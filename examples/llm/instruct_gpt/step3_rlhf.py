# -*- coding: utf-8 -*-
"""
rlhf
正在调试中
"""

from glob import glob
import torch
from torch import nn
from bert4torch.snippets import DottableDict, ListDataset, sequence_padding
from bert4torch.models import BaseModel, build_transformer_model
from bert4torch.generation import SeqGeneration
from bert4torch.callbacks import Callback, Logger
from bert4torch.trainer import PPOTrainerTrl
from trl import PPOConfig, set_seed
from utils import get_model_config
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import json


# 基本参数
args = DottableDict()
args.steps_per_epoch = None
args.epochs = 1
args.data_path = 'E:/Github/MedicalGPT/data/finetune/**/*.jsonl'
args.device = "cuda" if torch.cuda.is_available() else "cpu"
args.use_fast_tokenizer = False
args.seed = 1234
args.max_source_length = 256
args.max_target_length = 256
args.reward_model_name_or_path = "E:/pretrain_ckpt/deberta/[OpenAssistant]--reward-model-deberta-v3-large-v2"
args.load_in_8bit = False
args.max_steps = 100
args.learning_rate = 1e-5
args.batch_size = 8
args.gradient_accumulation_steps = 1
args.target_kl = 0.1
args.init_kl_coef = 0.2
args.adap_kl_ctrl = True
args.trust_remote_code = True

set_seed(args.seed)

# 模型配置
args.model_type, args.model_name_or_path, args.config_path, args.checkpoint_path = get_model_config('bloom')

# =============== 定义tokenizer ==================
if args.model_type == 'bloom':
    args.use_fast_tokenizer = True
# Load tokenizer
tokenizer_kwargs = {
    "use_fast": args.use_fast_tokenizer,
    "trust_remote_code": args.trust_remote_code,
}
tokenizer  = AutoTokenizer.from_pretrained(args.model_name_or_path, **tokenizer_kwargs)
# Required for llama
if args.model_type == "llama" and tokenizer.pad_token is None:
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})
pad_token_id = tokenizer.pad_token_id or -100

# =============== 定义Dataset ==================
max_source_length = args.max_source_length
max_target_length = args.max_target_length

PROMPT_TEMPLATE = (
    "Below is an instruction that describes a task. "
    "Write a response that appropriately completes the request.\n\n"
    "### Instruction:\n{instruction}\n\n### Response: "
)

def preprocess_function(examples):
    new_examples = []
    for conversation in examples:
        for message in conversation:
            instruction = message['value']
            input = message['from']
            if input:
                instruction = instruction + "\n" + input
            source = PROMPT_TEMPLATE.format_map({"instruction": instruction})
            tokenized_question = tokenizer(source, truncation=True, max_length=max_source_length)["input_ids"]
            new_examples.append((source, tokenized_question))
    return new_examples

# 加载数据集
class MyDataset(ListDataset):
    @staticmethod
    def load_data(filenames):
        """加载数据，并尽量分为不超过maxlen的句子
        """
        D = []
        for filename in filenames:
            with open(filename, encoding='utf-8') as f:
                for l in f:
                    D.append(json.loads(l)['conversations'])
        return preprocess_function(D)

def collate_fn(batch):
    batch_token_ids, batch_queries = [], []
    for query, token_ids in batch:
        batch_token_ids.append(token_ids)
        batch_queries.append(query)

    batch_token_ids = torch.tensor(sequence_padding(batch_token_ids, value=pad_token_id, mode='pre'), dtype=torch.long, device=args.device)
    return {'input_ids': batch_token_ids, 'query': batch_queries}, None

train_dataset = MyDataset(glob(args.data_path, recursive=True))

# ============= 定义 model =============
class Model(BaseModel):
    def __init__(self, *arg, **kwargs):
        super().__init__(*arg, **kwargs)
        self.module = build_transformer_model(config_path=args.config_path, checkpoint_path=args.checkpoint_path, model=args.model_type, 
                                                pad_token_id=pad_token_id, with_lm=False)
        self.score = nn.Linear(self.module.config['hidden_size'], 1)
    
    def forward(self, input_ids):
        logit = self.score(self.module(input_ids))
        return logit
model = Model().to(args.device)

# ============= 定义reward model =============
reward_model = AutoModelForSequenceClassification.from_pretrained(
    args.reward_model_name_or_path,
    # config=config,
    load_in_8bit=args.load_in_8bit,
    trust_remote_code=args.trust_remote_code,
)
reward_model = reward_model.to(args.device)
reward_tokenizer = AutoTokenizer.from_pretrained(args.reward_model_name_or_path, **tokenizer_kwargs)

config = PPOConfig(
    steps=args.max_steps,
    model_name=args.model_name_or_path,
    learning_rate=args.learning_rate,
    batch_size=args.batch_size,
    gradient_accumulation_steps=args.gradient_accumulation_steps,
    optimize_cuda_cache=True,
    target_kl=args.target_kl,
    seed=args.seed,
    init_kl_coef=args.init_kl_coef,
    adap_kl_ctrl=args.adap_kl_ctrl
)

# ============= generation =============
generation_kwargs = {
    "temperature": 1.0,
    "repetition_penalty": 1.0,
    "topp": 1.0,
}
tokenizer_config = {'skip_special_tokens': True, 'add_special_tokens': False}
generation = SeqGeneration(model, tokenizer, start_id=None, end_id=tokenizer.eos_token_id, mode='random_sample', tokenizer_config=tokenizer_config,
                        maxlen=max_target_length, default_rtype='logits', use_states=True)


# ============= PPOTrainer =============
trainer = PPOTrainerTrl(
    config,
    model,
    ref_model=None,
    tokenizer=tokenizer,
    dataset=train_dataset,
    data_collator=collate_fn,
    generation=generation,
    generation_kwargs=generation_kwargs
)

trainer.compile(loss=None, optimizer=None)

if __name__ == "__main__":
    logger = Logger('./log_reward.log')
    trainer.fit(trainer.dataloader, steps_per_epoch=args.steps_per_epoch, epochs=args.epochs, callbacks=[logger])