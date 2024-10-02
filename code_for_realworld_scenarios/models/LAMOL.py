import numpy as np
import logging
import torch
import os
import pickle
import transformers
from typing import List
from datasets import Dataset
from torch.utils.data import ConcatDataset, DataLoader

from utils.metric import ResultSummary
from utils.backbone import get_backbone
from utils.optimizer import get_optimizer
from utils.dataloader import get_dataloader, preprocess_function_train_generative_LAMOL
from utils.buffer import get_buffer
from utils.evaluation import evaluate_sent_level_acc_with_generation
from utils.datatypes import STR2BOOL
from models.Base import BaseLearner

logger = logging.getLogger()

def get_LAMOL_params(parser):
    '''
        The parameters of model LAMOL
    '''

    parser.add_argument("--LAMOL_lambda", type=float, default=0.25, help="The weight of the generation target in LAMOL")
    parser.add_argument("--LAMOL_gamma", type=float, default=0.20, help="The ratio of psesudo old samples w.r.t the training data of new task.")
    parser.add_argument("--LAMOL_topk", type=int, default=20, help="The top-k sampling for generating psesudo old samples.")
    parser.add_argument("--LAMOL_use_task_specific_gen_token", type=STR2BOOL, default=False, help="If using task-specific generation token for generating psesudo old samples.")
    parser.add_argument("--LAMOL_use_eos_as_gen_token", type=STR2BOOL, default=False, help="If using EOS token for generating psesudo old samples.")
    parser.add_argument("--LAMOL_use_ans_token", type=STR2BOOL, default=True, help="If using ANS token for generating psesudo old samples.")
    parser.add_argument("--LAMOL_ans_split_token", type=str, default=None, help="If LAMOL_use_ans_token is False, model will use LAMOL_ans_split_token to split questions and answers for pseudo samples.")


class LAMOL(BaseLearner):
    '''
        LAMOL: Train a generative model with question answering and generation targets 
        and generate pseudo old sampels before learning each new task.
        The implementation is based on [this repository](https://github.com/jojotenya/LAMOL).

        - [LAMOL: LAnguage MOdeling for Lifelong Language Learning](https://openreview.net/forum?id=Skgxcn4YDS)
    '''
    def __init__(self, params, CL_dataset, accelerator): 
        super().__init__(params, CL_dataset, accelerator)

        assert params.classifier in ['None'], 'NotImplemented for classifier %s and model %s'%(params.classifier,'LAMOL')
        assert params.il_mode in ['IIL','CIL','TIL','CIT'], 'NotImplemented for il mode %s and model %s'%(params.il_mode,'LAMOL')
        assert params.classification_type == 'sentence-level', 'NotImplemented for classification type %s'%(params.classification_type)
        assert params.backbone_type == 'generative', 'NotImplemented for backbone type %s'%(params.backbone_type)
        assert not params.is_replay, 'NotImplemented for is_replay = %s'%(params.is_replay)

    # ================================= Initialization =======================================
    def build_metric(self):
        self.result_summary = ResultSummary(num_task=self.CL_dataset.continual_config['NUM_TASK'])
        if self.params.il_mode == 'IIL':
            self.result_summary_train = ResultSummary(num_task=self.CL_dataset.continual_config['NUM_TASK'])
        
    def build_backbone(self):
        self.model, self.tokenizer = get_backbone(self.params)

    def build_classifier(self):
        self.classifier_list = None

    def build_optimizer(self):
        self.optimizer = get_optimizer(self.params, self.model, self.classifier_list)

    def build_dataloader(self):
        self.train_loader_list, self.dev_loader_list, self.test_loader_list = get_dataloader(self.params, self.CL_dataset, self.tokenizer)
        # Adding Special Tokens such as __ans__, __gen__
        self.model.resize_token_embeddings(len(self.tokenizer))

    def build_buffer(self):
        self.buffer = None
    
    def accelerate_prepare(self):
        self.model, self.optimizer, *self.train_loader_list = self.accelerator.prepare(self.model, self.optimizer, *self.train_loader_list)
        self.dev_loader_list = self.accelerator.prepare(*self.dev_loader_list)
        self.test_loader_list = self.accelerator.prepare(*self.test_loader_list)
    # =============================================================================================

    # ================================= Task-Level Functions =======================================
    def begin_task(self, task_id):
        super().begin_task(task_id)

    def end_task(self, task_id):
        super().end_task(task_id)

    # ==============================================================================================

    # ================================= Epoch-Level Functions =======================================
    def train_epochs(self, task_id):
        '''
            Training the model with serveral epochs
        '''

        if task_id>0:
            train_dataset = self.train_loader_list[task_id].dataset
            pseudo_buf_dataset_list = self.generate_pseudo_buffer_samples(task_id=task_id,
                                                                    num_samples=int(len(train_dataset)*self.params.LAMOL_gamma))
            cur_train_loader = DataLoader(
                ConcatDataset((train_dataset,*pseudo_buf_dataset_list)),
                batch_size=self.params.batch_size,
                shuffle=True,
                drop_last=False
            )
            cur_train_loader = self.accelerator.prepare(cur_train_loader)
        else:
            cur_train_loader = self.train_loader_list[task_id]

        total_epochs = self.params.training_epochs

        for epoch_id in range(total_epochs):

            if self.accelerator.is_main_process:
                logger.info("------------------------ epoch %d ------------------------" %(epoch_id+1))

            self.begin_epoch(task_id, epoch_id)

            for lm_input in cur_train_loader:
                self.observe_batch(task_id, epoch_id, lm_input) 

            self.end_epoch(task_id, epoch_id)

    def begin_epoch(self, task_id, epoch_id):
        '''
            Start of each epoch
        '''
        # Avoid overwrite the result of the same global step
        if ((self.params.evaluate_interval>0) and (epoch_id>0 and epoch_id%self.params.evaluate_interval==0)) or \
            (self.params.is_evaluate_init and task_id==0 and epoch_id==0):
            self.evaluate_model(task_id=task_id)
        self.loss_list = []
        self.model.train()

    def observe_batch(self, task_id, epoch_id, lm_input):
        '''
            Observe a batch of data
        '''
        # Update step
        self.step += 1
        self.global_step += 1

        # For Distributed Data Parallel
        if hasattr(self.model,'module'):
            model = self.model.module
        else:
            model = self.model

        # Compute loss
        # Training with Causal Language Modeling Loss
            
        qa_loss = model(**{'input_ids':lm_input['input_ids_with_ans'], 
                                'attention_mask':lm_input['attention_mask_with_ans'],
                                'labels':lm_input['labels_with_ans']}).loss

        generation_loss = model(**{'input_ids':lm_input['input_ids_with_gen_ans'], 
                                'attention_mask':lm_input['attention_mask_with_gen_ans'],
                                'labels':lm_input['labels_with_gen_ans']}).loss

        total_loss = qa_loss + self.params.LAMOL_lambda*generation_loss

        # Backward
        model.train()
        self.optimizer.zero_grad()        
        self.accelerator.backward(total_loss)
        
        scalar_loss = total_loss.item()
        if not(np.isnan(scalar_loss)) or not(np.isinf(scalar_loss)):
            self.optimizer.step()
            self.loss_list.append(scalar_loss)

        # Print training information
        if self.params.info_per_steps and self.step%self.params.info_per_steps==0:
            mean_loss = np.mean(self.loss_list)
            if self.accelerator.is_main_process:
                logger.info("Epoch %d, Step %d: Total_loss=%.3f,"%(
                        epoch_id+1, self.step, mean_loss
                ))
            self.accelerator.log({'loss':mean_loss},step=self.global_step)

    def end_epoch(self, task_id, epoch_id):
        '''
            End of each epoch
        '''
        # Print training information
        if len(self.loss_list)>0:
            mean_loss = np.mean(self.loss_list)
            if self.accelerator.is_main_process:
                logger.info("Epoch %d, Step %d: Total_loss=%.3f"%(
                            epoch_id+1, self.step, mean_loss
                    ))
            self.accelerator.log({'loss':mean_loss},step=self.global_step)
            
        # For evaluation
        if (self.params.evaluate_interval>0) and epoch_id%self.params.evaluate_interval==0:
            il_mode = self.params.il_mode
            acc = self.evaluate_current_task(task_id, task_id, 'dev', il_mode)
            if self.accelerator.is_main_process:
                logger.info("Mode %s, Current Task %d, Epoch %d, Step %d: Dev_acc=%.3f" % (
                    il_mode, task_id, epoch_id+1, self.step, acc
                ))
            self.accelerator.log({'Dev_Acc_Task_%d'%(task_id):acc},step=self.global_step)
            dev_score = acc

            if dev_score > self.best_score:
                if self.accelerator.is_main_process:
                    logger.info("Find better model!!")

        # Saving GPU memory
        torch.cuda.empty_cache()
    # ===========================================================================================


    # ================== Evaluation, Logging, Saving and Loading Functions ======================
    def evaluate_current_task(self,
                                eval_task_id: int, 
                                cur_task_id: int, 
                                phase: str,
                                il_mode: str,
                                return_dict: bool=False) -> dict:
        '''
            Evaluate the model on the current task

            Params: 
                - eval_task_id: the id of the task to be evaluated, 
                this information should NOT be provided to the CIL model during inference!
                - cur_task_id: the id recording how many tasks the model has learned,
                this information can be provided to the CIL model during inference.
                - phase: 'train','dev'or'test'
                - il_mode: 'CIL', 'TIL', 'CIT'

            Return:
                - acc: CIL accuracy (%) or 'TIL': TIL accuracy (%)
        '''

        assert phase in ['train','test','dev']
        if phase=='train':
            data_loader = self.train_loader_list
        elif phase=='dev':
            data_loader = self.dev_loader_list
        else:
            data_loader = self.test_loader_list

        # For Distributed Data Parallel
        if hasattr(self.model,'module'):
            model = self.model.module
        else:
            model = self.model

        if self.classifier_list is None:
            
            acc = evaluate_sent_level_acc_with_generation(
                model=model,
                eval_data_loader=data_loader[eval_task_id],
                tokenizer=self.tokenizer,
                accelerator=self.accelerator,
                params=self.params,
                idx2label=self.CL_dataset.continual_config.get('idx2label',None)
            )
            # NOTE: When not using classifier, the SEQ model does not need (benefit from) task identity 

            return  acc

        else:
            raise NotImplementedError()
    # ===========================================================================================

    # ======================== Other Model-Specific Functions ===================================
    def generate_pseudo_buffer_samples(self, task_id: int, num_samples: int) -> List[Dataset]:
        '''
            Generate pseudo old samples with generative models

            Args:
                - task_id: the current task id
                - num_samples: the number of samples to be generated

            Return:
                pseudo_dataset_list
        '''

        # For Distributed Data Parallel
        if hasattr(self.model,'module'):
            model = self.model.module
        else:
            model = self.model

        input_column = 'input'
        target_column = 'target'
        if self.params.LAMOL_use_ans_token:
            ans_token = '__ans__'
        else:
            ans_token = ''
        eos_token = self.tokenizer.eos_token
        num_task = self.CL_dataset.continual_config['NUM_TASK']

        pseudo_dataset_list = []
        
        generate_batch_size = 8

        # To ignore the following debug information when use <|endoftext|> as the generation token: 
        # "A decoder-only architecture is being used, but right-padding was detected! For correct generation results, please set `padding_side='left'` when initializing the tokenizer."
        transformers.utils.logging.set_verbosity_error()

        with torch.no_grad():
        
            for t_id in range(task_id):

                if self.params.il_mode == 'IIL':
                    pesudo_samples_dict = {
                        'input': [], 'target': [], 'label_idx_cil': [], 'label_idx_til': [],
                        'instance_id': [], 'concept_id': [], 'relation_id': [], 
                    }
                else:
                    if self.params.classifier == 'None':
                        pesudo_samples_dict = {
                            'input': [], 'target': [], # 'label_idx_cil': [], 'label_idx_til': []
                        }
                    else:
                        pesudo_samples_dict = {
                            'input': [], 'target': [], 'label_idx_cil': [], 'label_idx_til': []
                        }

                cnt_num_samples = num_samples//task_id

                if self.params.LAMOL_use_eos_as_gen_token:
                    gen_token = self.tokenizer.eos_token
                else:
                    gen_token = '__%d__'%(t_id) if self.params.LAMOL_use_task_specific_gen_token else '__gen__'

                while cnt_num_samples > 0:
                        
                    generate_num = generate_batch_size if cnt_num_samples>=generate_batch_size else cnt_num_samples

                    lm_input = self.tokenizer([gen_token for _ in range(generate_num)],
                                                return_tensors='pt')
                    lm_input = {k:v.to(model.device) for k,v in lm_input.items()}
                    
                    max_input_len = np.max([len(lm_input['input_ids'][i]) for i in range(generate_num)])

                    generate_ids_all = model.generate(**lm_input, 
                                            max_new_tokens=self.params.max_seq_length-max_input_len, 
                                            pad_token_id=self.tokenizer.eos_token_id,
                                            do_sample=True,
                                            top_k=self.params.LAMOL_topk,
                                            ) 
                    generate_ids = generate_ids_all[:,max_input_len:].contiguous()
                    generated_samples = self.tokenizer.batch_decode(generate_ids)

                    for _one_sample in generated_samples:
                        if self.params.LAMOL_use_ans_token:
                            if _one_sample.count('__ans__')!=1:
                                continue
                            _question, _answer = _one_sample.split('__ans__')
                        else:
                            assert self.params.LAMOL_ans_split_token is not None, \
                                "params.LAMOL_ans_split_token should be specified if params.LAMOL_use_ans_token is False"
                            if _one_sample.count(self.params.LAMOL_ans_split_token)!=1:
                                continue
                            else:
                                _question, _answer = _one_sample.split(self.params.LAMOL_ans_split_token)
                                _question += self.params.LAMOL_ans_split_token
                        _answer = _answer.replace(self.tokenizer.eos_token,'')
                        pesudo_samples_dict['input'].append(_question)
                        pesudo_samples_dict['target'].append(_answer)
                        if self.params.classifier != 'None':
                            pesudo_samples_dict['label_idx_cil'].append(-1)
                            pesudo_samples_dict['label_idx_til'].append(-1)
                        if self.params.il_mode == 'IIL':
                            pesudo_samples_dict['instance_id'].append(-1)
                            pesudo_samples_dict['concept_id'].append(-1)
                            pesudo_samples_dict['relation_id'].append(-1)
                        
                        
                    cnt_num_samples -= generate_num
            
                if len(pesudo_samples_dict['input'])==0:
                    logger.error('No pseudo samples are generated in the correct format for task %d!'%(t_id+1))
                    continue
                with open(os.path.join(self.params.dump_path,f'Pseudo_Dataset_Train_{task_id}_Task_{t_id}.pkl'),'wb') as f:
                    pickle.dump(pesudo_samples_dict,f)
                pseudo_dataset = Dataset.from_dict(pesudo_samples_dict)
                pseudo_dataset = pseudo_dataset.map(preprocess_function_train_generative_LAMOL, 
                                                    batched=True, 
                                                    desc='Generate pseudo samples for task %d'%(t_id+1), 
                                                    batch_size=1000,
                                                    fn_kwargs={
                                                        'params':self.params,
                                                        'tokenizer':self.tokenizer,
                                                        'num_task':num_task,
                                                        'task_id':t_id,
                                                        'input_column':input_column,
                                                        'target_column':target_column,
                                                        'ans_token':ans_token,
                                                        'eos_token':eos_token,
                                                        'gen_token':gen_token,
                                                    })
                if self.params.il_mode == 'IIL':
                    pseudo_dataset.set_format(type='torch', columns=['input_ids','attention_mask','label_idx_cil','label_idx_til',
                                                                    'input_ids_with_ans', 'attention_mask_with_ans', 'labels_with_ans', 
                                                                    'input_ids_with_gen_ans', 'attention_mask_with_gen_ans', 'labels_with_gen_ans',
                                                                    'target', 'instance_id', 'concept_id', 'relation_id'])
                else:
                    if self.params.classifier == 'None':
                        pseudo_dataset.set_format(type='torch', columns=['input_ids','attention_mask', 'target', # 'label_idx_cil','label_idx_til',
                                                                        'input_ids_with_ans', 'attention_mask_with_ans', 'labels_with_ans', 
                                                                        'input_ids_with_gen_ans', 'attention_mask_with_gen_ans', 'labels_with_gen_ans'])
                    else:
                        pseudo_dataset.set_format(type='torch', columns=['input_ids','attention_mask','label_idx_cil','label_idx_til',
                                                                        'input_ids_with_ans', 'attention_mask_with_ans', 'labels_with_ans', 
                                                                        'input_ids_with_gen_ans', 'attention_mask_with_gen_ans', 'labels_with_gen_ans'])

                pseudo_dataset_list.append(pseudo_dataset)

        transformers.utils.logging.set_verbosity_warning()

        return pseudo_dataset_list

    # ===========================================================================================