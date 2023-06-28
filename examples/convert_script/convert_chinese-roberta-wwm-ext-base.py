# 转换哈工大的chinese-roberta-wwm-ext-base权重
# 权重链接：https://huggingface.co/hfl/chinese-roberta-wwm-ext
# 不转换也没关系，只是在with_mlm=True的时候，会报[WARNIMG] cls.predictions.decoder.bias not found in pretrain models

import torch

state_dict = torch.load('G:/pretrain_ckpt/roberta/[hit_torch_base]--chinese-roberta-wwm-ext-base/pytorch_model.bin')
state_dict['cls.predictions.decoder.bias'] = state_dict['cls.predictions.bias']
torch.save(state_dict, 'G:/pretrain_ckpt/roberta/[hit_torch_base]--chinese-roberta-wwm-ext-base/bert4torch_pytorch_model.bin')