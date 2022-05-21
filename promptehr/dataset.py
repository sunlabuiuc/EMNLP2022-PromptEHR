import pdb
import os
import json
import random
from collections import defaultdict
from typing import Any, Callable, Dict, List, NewType, Optional, Tuple, Union

import numpy as np
import torch
from torch import nn
from torch.utils.data import Dataset
from transformers import BartTokenizer
from transformers.data.data_collator import InputDataClass

from . import constants

class MimicTrainDataset(Dataset):
    '''mode: for spliting datasets
    '''
    def __init__(self, data_dir:str='./MIMIC-III/processed', mode: str='10k') -> None:
        '''train-5k, 10k, 20k, all
        '''
        self.is_training = True
        # load piece of training data for experiments
        if mode != 'all':
            merge_file = os.path.join(data_dir, f'./MIMIC-III-Merge-train-{mode}.jsonl')
        else:
            merge_file = os.path.join(data_dir, f'./MIMIC-III-Merge-train.jsonl')

        samples = []
        with open(merge_file, 'r', encoding='utf-8') as f:
            for line in f.readlines():
                samples.append(json.loads(line.strip()))
        self.samples = samples

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        sample = self.samples[index]
        return sample

class MimicDataset(Dataset):
    def __init__(self, data_dir:str='./MIMIC-III/processed', mode: str='train') -> None:
        assert mode in ['train', 'test', 'val']
        if mode == 'train': self.is_training = True
        else: self.is_training = False
        # load data
        merge_file = os.path.join(data_dir, f'./MIMIC-III-Merge-{mode}.jsonl')
        samples = []
        with open(merge_file, 'r', encoding='utf-8') as f:
            for line in f.readlines():
                samples.append(json.loads(line.strip()))
        self.samples = samples

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        sample = self.samples[index]
        return sample

class MimicDataCollator:
    '''Data collator for train/evaluate the EHR-BART model.
    '''
    __code_type_list__ = constants.CODE_TYPES
    __special_token_dict__ = constants.SPECIAL_TOKEN_DICT
    __del_or_rep__ = ['rep', 'del']

    def __init__(self, tokenizer, mlm_prob=0.15, lambda_poisson=3.0, del_prob=0.15, max_train_batch_size=16, mode='train'):
        '''mlm_prob: probability of masked tokens
        lambda_poisoon: span infilling parameters
        del_prob: probability of delete tokens
        max_train_batch_size: sample batch to avoid OOM, because for each patient we will generate a batch of series
        '''
        self.mlm_prob = mlm_prob
        self.tokenizer = tokenizer
        self.tokenizer.model_max_length = constants.model_max_length
        self.mlm_probability = mlm_prob
        self.lambda_poisson = lambda_poisson
        self.del_probability = del_prob
        self.max_train_batch_size = max_train_batch_size # sample batch to avoid OOM

        self.eval_code_type = None # remained for evaluation

        assert mode in ['train', 'val', 'test']
        if mode=='train': self.is_training=True
        else: self.is_training=False
        if mode=='test': self.is_testing=True
        else: self.is_testing=False

    def __call__(self, samples: List[InputDataClass]) -> Dict[str, Any]:
        if self.is_training:
            batch = self.call_train(samples)
        elif self.is_testing:
            batch = self.call_test(samples)
        else:
            batch = self.call_val(samples)
        # batch = self._ship_to_device(batch)
        return batch

    # def _ship_to_device(self, batch):
    #     for k,v in batch.items():
    #         if isinstance(v, torch.Tensor):
    #             batch[k] = v.cuda()
    #     return batch

    def call_train(self, samples: List[InputDataClass]) -> Dict[str, Any]:
        '''label mask should not be used during training.
        '''
        batch = defaultdict(list)

        # randomly pick one of code types for prediction, keep the same for this batch
        code_type = random.sample(self.__code_type_list__, 1)[0]
        batch['code_type'] = code_type

        for sample in samples:
            num_adm = len(sample[code_type])

            # accumulated during enumerating all admisions
            input_str_all = []
            label_str_all = []
            num_token_all = []

            # cope with too long labtest codes
            # start from the offset if the labtest is too long
            adm = 0
            while adm < num_adm:
                span_str_list = [] # input ids
                span_label_str_list = [] # label ids
                num_token_this_adm = 0

                # shuffle the code order
                code_list = list(sample.keys())
                random.shuffle(code_list)
                for code in sample.keys():
                    if code == 'pid': continue
                    span = sample[code][adm]

                    if len(span) == 0: continue

                    # restrict the num of tokens in each span
                    span = random.sample(span, min(20, len(span)))

                    span_str = self.__special_token_dict__[code][0] + ' '.join(span) + self.__special_token_dict__[code][1]
                    span_label_str_list.append(span_str)
                    num_token_this_adm += len(span) + 2

                    if code == code_type:
                        # do mask infilling / mask
                        infill_span, _, _ = self.mask_infill([span])
                        span_str = self.__special_token_dict__[code][0] + ' '.join(infill_span[0]) + self.__special_token_dict__[code][1]
                        span_str_list.append(span_str)
                    else:
                        if self.__del_or_rep__[random.randint(0,1)] == 'rep': rep_del_span = self.rep_token([span], code)
                        else: rep_del_span = self.del_token([span])
                        span_str = self.__special_token_dict__[code][0] + ' '.join(rep_del_span[0]) + self.__special_token_dict__[code][1]
                        span_str_list.append(span_str)

                span_str_list.append('</s>')
                span_label_str_list.append('</s>')
                num_token_this_adm += 1

                if adm == 0: # the first visit starts from bos token
                    span_str_list = ['<s>'] + span_str_list
                    span_label_str_list = ['<s>'] + span_label_str_list
                    num_token_this_adm += 1

                # build one sample in batch, concatenate all admissions before
                span_str_this_adm = ' '.join(span_str_list)
                span_label_str_this_adm = ' '.join(span_label_str_list)

                num_token_all, input_str_all, label_str_all = \
                    self._check_max_length(num_token_this_adm, num_token_all, input_str_all, label_str_all)

                inputs = self.tokenizer([' '.join(input_str_all) + span_str_this_adm, ' '.join(label_str_all) + span_label_str_this_adm],
                    padding='max_length', add_special_tokens=False, return_tensors='pt').input_ids

                batch['input_ids'].append(inputs[0].unsqueeze(0))
                batch['labels'].append(inputs[1].unsqueeze(0))

                input_str_all.append(span_str_this_adm)
                label_str_all.append(span_label_str_this_adm)
                num_token_all.append(num_token_this_adm)

                if num_adm > 1 and adm < num_adm-1:
                    # build next span predictio ntask
                    next_span = sample[code_type][adm+1]

                    if len(next_span) == 0:
                        adm +=1 # empty modality, try next admission
                        continue

                    # do shuffling
                    next_span = random.sample(next_span, len(next_span))

                    # do next span prediction
                    label_str = self.__special_token_dict__[code_type][0] + ' '.join(next_span) + self.__special_token_dict__[code_type][1]
                    input_str = self.__special_token_dict__[code_type][0] + '<mask>' + self.__special_token_dict__[code_type][1]

                    num_token_all, input_str_all, label_str_all = \
                        self._check_max_length(len(next_span)+2, num_token_all, input_str_all, label_str_all)

                    inputs = self.tokenizer([' '.join(input_str_all)+input_str, ' '.join(label_str_all)+label_str],
                        padding='max_length', add_special_tokens=False, return_tensors='pt').input_ids

                    batch['input_ids'].append(inputs[0].unsqueeze(0))
                    batch['labels'].append(inputs[1].unsqueeze(0))

                # go to next admission
                adm += 1

        # concatenate all the processed
        batch['input_ids'] = torch.cat(batch['input_ids'], 0)
        batch['labels'] = torch.cat(batch['labels'], 0)

        if batch['input_ids'].shape[0] > self.max_train_batch_size:
            sub_indices = np.random.choice(np.arange(len(batch['input_ids'])), self.max_train_batch_size, replace=False)
            batch['input_ids'] = batch['input_ids'][sub_indices]
            batch['labels'] = batch['labels'][sub_indices]

        return batch

    def call_val(self, samples: List[InputDataClass]) -> Dict[str, Any]:
        batch = defaultdict(list)
        eval_code_type = self.eval_code_type
        batch['code_type'] = eval_code_type

        for sample in samples:
            num_adm = len(sample['diagnosis'])

            # accumulated during enumerating all admisions
            input_str_all = []
            label_str_all = []
            label_mask_list_all = []
            num_token_all = []

            adm = 0
            while adm < num_adm:
                span_str_list = [] # input ids
                span_label_str_list = [] # label ids
                label_mask_list = [] # label mask used for evaluation (not used during training)
                num_token_this_adm = 0

                for code in sample.keys():
                    if code == 'pid': continue
                    span = sample[code][adm]

                    if len(span) == 0: continue
                    if len(span) > 20: span = random.sample(span, 20)

                    num_token_this_adm += len(span) + 2
                    span_str = self.__special_token_dict__[code][0] + ' '.join(span) + self.__special_token_dict__[code][1]
                    span_label_str_list.append(span_str)

                    if code == eval_code_type and num_adm == 1:
                        # do mask infilling / mask if there is only one admission of the patient
                        infill_span, label_span, label_mask_span = self.mask_infill([span])
                        span_str = self.__special_token_dict__[code][0] + ' '.join(infill_span[0]) + self.__special_token_dict__[code][1]
                        span_str_list.append(span_str)
                        label_mask_list += [0] + label_mask_span[0] + [0]

                    else:
                        # do not change anything for not targeted codes
                        span_str = self.__special_token_dict__[code][0] + ' '.join(span) + self.__special_token_dict__[code][1]
                        span_str_list.append(span_str)
                        label_mask_list += np.zeros(len(span)+2, dtype=int).tolist()

                span_str_list.append('</s>')
                span_label_str_list.append('</s>')
                num_token_this_adm += 1
                label_mask_list =  label_mask_list + [0]

                if adm == 0: # the first visit starts from bos token
                    span_str_list = ['<s>'] + span_str_list
                    span_label_str_list = ['<s>'] + span_label_str_list
                    label_mask_list = [0] + label_mask_list

                # build one sample in batch, concatenate all admissions before
                span_str_this_adm = ' '.join(span_str_list)
                span_label_str_this_adm = ' '.join(span_label_str_list)
                label_mask_this_adm = label_mask_list

                num_token_all, input_str_all, label_str_all, label_mask_list_all = \
                    self._check_max_length(num_token_this_adm, num_token_all, input_str_all, label_str_all, label_mask_list_all)

                if num_adm == 1:
                    # if there is only one admission for this patient
                    inputs = self.tokenizer([span_str_this_adm, span_label_str_this_adm],
                        padding='max_length', add_special_tokens=False, return_tensors='pt').input_ids
                    batch['input_ids'].append(inputs[0].unsqueeze(0))
                    batch['labels'].append(inputs[1].unsqueeze(0))
                    label_mask_ts = torch.tensor(self._pad_max_length(label_mask_this_adm)).long()
                    batch['label_mask'].append(label_mask_ts.unsqueeze(0))
                    break # no next span prediction for this

                input_str_all.append(span_str_this_adm)
                label_str_all.append(span_label_str_this_adm)
                label_mask_list_all.append(label_mask_this_adm)
                num_token_all.append(num_token_this_adm)

                if num_adm > 1 and adm < num_adm-1:
                    # build next span predictio ntask
                    next_span = sample[eval_code_type][adm+1]

                    if len(next_span) == 0:
                        adm += 1 # empty modality, try next admission
                        continue # empty modality

                    # do next span prediction
                    label_str = self.__special_token_dict__[eval_code_type][0] + ' '.join(next_span) + self.__special_token_dict__[eval_code_type][1]
                    input_str = self.__special_token_dict__[eval_code_type][0] + '<mask>' + self.__special_token_dict__[eval_code_type][1]

                    num_token_all, input_str_all, label_str_all, label_mask_list_all = \
                        self._check_max_length(len(next_span)+2, num_token_all, input_str_all, label_str_all, label_mask_list_all)

                    inputs = self.tokenizer([' '.join(input_str_all)+input_str, ' '.join(label_str_all)+label_str],
                        padding='max_length', add_special_tokens=False, return_tensors='pt').input_ids

                    label_mask = sum(label_mask_list_all,[]) + [0] + np.ones(len(next_span), dtype=int).tolist() + [0]
                    label_mask_ts = torch.tensor(self._pad_max_length(label_mask)).long()

                    batch['input_ids'].append(inputs[0].unsqueeze(0))
                    batch['labels'].append(inputs[1].unsqueeze(0))
                    batch['label_mask'].append(label_mask_ts.unsqueeze(0))

                # go to next admission
                adm += 1

        batch['input_ids'] = torch.cat(batch['input_ids'], 0)
        batch['labels'] = torch.cat(batch['labels'], 0)
        batch['label_mask'] = torch.cat(batch['label_mask'], 0)
        return batch

    def call_test(self, samples: List[InputDataClass]) -> Dict[str, Any]:
        '''separate longitudinal and latitudinal perplexity evaluation.
        '''
        batch = defaultdict(list)
        eval_code_type = self.eval_code_type
        eval_ppl_type = self.eval_ppl_type

        batch['code_type'] = eval_code_type

        for sample in samples:
            num_adm = len(sample['diagnosis'])

            if num_adm == 1 and eval_ppl_type == 'tpl': # cant evaluate tpl if there is only one admission
                continue

            # accumulated during enumerating all admisions
            input_str_all = []
            label_str_all = []
            label_mask_list_all = []
            num_token_all = []

            adm = 0
            while adm < num_adm:
                span_str_list = [] # input ids
                span_label_str_list = [] # label ids
                label_mask_list = [] # label mask used for evaluation (not used during training)
                num_token_this_adm = 0

                for code in sample.keys():
                    if code == 'pid': continue
                    span = sample[code][adm]

                    if len(span) == 0: continue

                    if len(span) > 20: span = random.sample(span, 20)

                    num_token_this_adm += len(span) + 2
                    span_str = self.__special_token_dict__[code][0] + ' '.join(span) + self.__special_token_dict__[code][1]
                    span_label_str_list.append(span_str)

                    if code == eval_code_type and eval_ppl_type == 'spl': # for spatial ppl evaluation
                        # do mask all codes inside this modality
                        span_str = self.__special_token_dict__[code][0] + '<mask>' + self.__special_token_dict__[code][1]
                        label_mask_span = np.ones(len(span), dtype=int).tolist()
                        label_mask_list += [0] + label_mask_span + [0]
                        span_str_list.append(span_str)

                    else:
                        # do not change anything for not targeted codes
                        span_str = self.__special_token_dict__[code][0] + ' '.join(span) + self.__special_token_dict__[code][1]
                        span_str_list.append(span_str)
                        label_mask_list += np.zeros(len(span)+2, dtype=int).tolist()

                span_str_list.append('</s>')
                span_label_str_list.append('</s>')
                num_token_this_adm += 1
                label_mask_list =  label_mask_list + [0]

                if adm == 0: # the first visit starts from bos token
                    span_str_list = ['<s>'] + span_str_list
                    span_label_str_list = ['<s>'] + span_label_str_list
                    label_mask_list = [0] + label_mask_list

                # build one sample in batch, concatenate all admissions before
                span_str_this_adm = ' '.join(span_str_list)
                span_label_str_this_adm = ' '.join(span_label_str_list)
                label_mask_this_adm = label_mask_list

                input_str_all.append(span_str_this_adm)
                label_str_all.append(span_label_str_this_adm)
                label_mask_list_all.append(label_mask_this_adm)
                num_token_all.append(num_token_this_adm)

                num_token_all, input_str_all, label_str_all, label_mask_list_all = \
                    self._check_max_length(num_token_this_adm, num_token_all, input_str_all, label_str_all, label_mask_list_all)

                if eval_ppl_type == 'spl': # only used when evaluating spatial ppl
                    # if there is only one admission for this patient
                    input_strs = label_str_all[:-1] + [input_str_all[-1]]
                    inputs = self.tokenizer([' '.join(input_strs), ' '.join(label_str_all)],
                        padding='max_length', add_special_tokens=False, return_tensors='pt').input_ids

                    label_mask_past = sum(label_mask_list_all[:-1],[])
                    label_mask = [0] * len(label_mask_past) + label_mask_list_all[-1]

                    batch['input_ids'].append(inputs[0].unsqueeze(0))
                    batch['labels'].append(inputs[1].unsqueeze(0))

                    label_mask_ts = torch.tensor(self._pad_max_length(label_mask)).long()
                    batch['label_mask'].append(label_mask_ts.unsqueeze(0))

                elif eval_ppl_type == 'tpl' and adm < num_adm-1: # do next span prediction
                    # build next span predictio ntask
                    next_span = sample[eval_code_type][adm+1]

                    if len(next_span) == 0:
                        adm += 1 # empty modality, try next admission
                        continue # empty modality

                    # do next span prediction
                    label_str = self.__special_token_dict__[eval_code_type][0] + ' '.join(next_span) + self.__special_token_dict__[eval_code_type][1]
                    input_str = self.__special_token_dict__[eval_code_type][0] + '<mask>' + self.__special_token_dict__[eval_code_type][1]

                    num_token_all, input_str_all, label_str_all, label_mask_list_all = \
                        self._check_max_length(len(next_span)+2, num_token_all, input_str_all, label_str_all, label_mask_list_all)

                    inputs = self.tokenizer([' '.join(label_str_all)+input_str, ' '.join(label_str_all)+label_str],
                        padding='max_length', add_special_tokens=False, return_tensors='pt').input_ids

                    label_mask = sum(label_mask_list_all,[]) + [0] + np.ones(len(next_span), dtype=int).tolist() + [0]
                    label_mask_ts = torch.tensor(self._pad_max_length(label_mask)).long()

                    batch['input_ids'].append(inputs[0].unsqueeze(0))
                    batch['labels'].append(inputs[1].unsqueeze(0))
                    batch['label_mask'].append(label_mask_ts.unsqueeze(0))

                # go to next admission
                adm += 1

        if len(batch['input_ids']) == 0: # num_adm > 1 not found
            return None

        batch['input_ids'] = torch.cat(batch['input_ids'], 0)
        batch['labels'] = torch.cat(batch['labels'], 0)
        batch['label_mask'] = torch.cat(batch['label_mask'], 0)
        return batch


    def mask_infill(self, spans:List=[['D_536','D_564']]):
        '''infill in a list of spans.
        '''
        num_adm = len(spans)
        mask_token = self.tokenizer.mask_token
        num_infill_tokens = np.random.poisson(self.lambda_poisson, num_adm)
        label_mask_list = []
        sample_list = []
        label_list = []
        for i, span in enumerate(spans):
            num_code = len(span)
            label_list.append(span)
            if num_code == 1: # only one token inside this span
                label_mask = [1] # 1 -> masked; 0 -> not masked
                sample = ['<mask>']
            else:
                sample = span
                num_infill = num_infill_tokens[i]
                num_infill = max(min(num_code-1, num_infill), 1) # at least mask one token
                label_mask = np.zeros(len(span), dtype=int)
                start_idx = np.random.randint(0, num_infill+1)
                sample = sample[:start_idx] + [mask_token] + sample[start_idx+num_infill:]
                label_mask[start_idx:start_idx+num_infill] = 1
                label_mask = label_mask.tolist()

            sample_list.append(sample)
            label_mask_list.append(label_mask)

        return sample_list, label_list, label_mask_list

    def del_token(self,spans:List=[['D_536', 'D_564']]):
        '''del token
        '''
        return_spans = []
        num_adm = len(spans)
        for i, span in enumerate(spans):
            # span = np.array(span).flatten().tolist()
            num_code = len(span)
            # deletion
            del_indices = np.random.binomial(np.ones(num_code, dtype=int), self.del_probability)
            sub_span = np.array(span)[~del_indices.astype(bool)].tolist()
            return_spans.append(sub_span)

        return return_spans

    def rep_token(self, spans:List=[['D_536', 'D_564']], code_type:str='diag'):
        '''replace token in this span.
        '''
        num_adm = len(spans)
        return_spans = []
        for i, span in enumerate(spans):
            # span = np.array(span).flatten().tolist()
            num_code = len(span)
            # replace
            rep_indices = np.random.binomial(np.ones(num_code, dtype=int), self.del_probability)
            rep_indices = rep_indices.astype(bool)
            random_words = np.random.randint(0, len(self.tokenizer.code_vocab[code_type]), num_code)
            random_words = self.tokenizer.code_vocab[code_type][random_words]
            rep_span = np.array(span).flatten()
            rep_span[rep_indices] = random_words[rep_indices]
            rep_span = rep_span.tolist()
            return_spans.append(rep_span)

        return return_spans

    def set_eval_code_type(self, code_type):
        print(f'evaluation for code {code_type}.')
        self.eval_code_type = code_type

    def set_eval_ppl_type(self, ppl_type):
        assert ppl_type in ['tpl', 'spl'] # temporal or spatial perplexity measure
        print(f'evaluation for {ppl_type} perplexity.')
        self.eval_ppl_type = ppl_type

    def _pad_max_length(self, x:List, fill:int=0):
        '''fill label mask
        '''
        max_length = self.tokenizer.model_max_length
        if len(x) < max_length:
            return x + [fill] * (max_length - len(x))

    def _check_max_length(self, num_token_this_span, num_token_all, input_str_all, label_str_all, label_mask_list_all=None):
        '''cut if it exceeds the model max length
        '''
        while sum(num_token_all) + num_token_this_span > self.tokenizer.model_max_length - 10:
            # print(len(num_token_all))
            # move the span list to the right if the length over passes the maximum length restriction
            num_token_all, input_str_all, label_str_all = num_token_all[1:], input_str_all[1:], label_str_all[1:]
            input_str_all[0] = '<s>' + input_str_all[0]
            label_str_all[0] = '<s>' + label_str_all[0]
            num_token_all[0] += 1
            if label_mask_list_all is not None:
                label_mask_list_all = label_mask_list_all[1:]
                label_mask_list_all[0] = [0] + label_mask_list_all[0]

        if label_mask_list_all is None:
            return num_token_all, input_str_all, label_str_all
        else:
            return num_token_all, input_str_all, label_str_all, label_mask_list_all