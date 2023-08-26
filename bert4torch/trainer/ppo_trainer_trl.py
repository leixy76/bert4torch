'''修改trl包的PPOTrainer, 正在修改中
'''
import torch
from torch import nn
from torch4keras.trainer import Trainer
from torch4keras.snippets import log_warn
from bert4torch.models import BaseModel
try:
    import trl
    trl.trainer.ppo_trainer.SUPPORTED_ARCHITECTURES = (BaseModel, )
    from trl.trainer import PPOTrainer
except:
    PPOTrainer = object


class PPOTrainerTrl(PPOTrainer, Trainer):
    def __init__(self, *args, generation=None, generation_kwargs=None, reward_model=None, reward_tokenizer=None, **kwargs):
        if PPOTrainer == object:
            raise ValueError('Please install trl by running `pip install trl`')
        Trainer.__init__(self)
        PPOTrainer.__init__(self, *args, **kwargs)
        self.generation = generation
        self.generation.process_choice = None  # 训练的时候已经传入的是token_ids，因此这里不tokenize了
        self.reward_model = reward_model
        self.reward_tokenizer = reward_tokenizer
        self.generation_kwargs = generation_kwargs or {}
        self.reward_baseline = kwargs.pop('reward_baseline', 0)
        self.grad_accumulation_steps = self.config.gradient_accumulation_steps
            
    def train_step(self, train_X, train_y):
        device = self.model.device
        if isinstance(train_X, (tuple, list)):
            question_tensors, query = train_X[0], train_X[1]
        elif isinstance(train_X, dict):
            question_tensors, query = train_X["input_ids"], train_X["query"]
        else:
            raise ValueError('Args `train_X` format illegel')
        
        # actor生成得到推理结果
        responses = []
        self.generation.decoder.with_lm = True
        response_tensors = self.generation.batch_generate(question_tensors, **self.generation_kwargs)
        self.generation.decoder.with_lm = False
        for response_tensor in response_tensors:
            r = self.tokenizer.decode(response_tensor, skip_special_tokens=True)
            responses.append(r)

        # Compute reward score
        score_outputs = [self.get_reward_model_output(self.reward_model, self.reward_tokenizer, q, r, device)
                         for q, r in zip(query, responses)]
        rewards = self.calculate_rewards(score_outputs, self.reward_baseline)

        # Run PPO step
        self.question_tensors = [i for i in question_tensors]
        self.response_tensors = response_tensors
        self.rewards = rewards
        self.ppo_step = True
        stats = self.step()
        return None, torch.tensor(stats['ppo/loss/total']), stats

    def step(self):
        if self.ppo_step:
            self.ppo_step = False
            return PPOTrainer.step(self, self.question_tensors, self.response_tensors, self.rewards)
        

    @staticmethod
    def calculate_rewards(reward_score_outputs, reward_baseline=0):
        """
        Calculate the reward for a given score output.
        :param reward_score_outputs: 
        :param reward_baseline: 
        :return: 
        """
        rewards = []
        for score in reward_score_outputs:
            if isinstance(score, torch.Tensor) and score.numel() == 1:
                reward_value = score.item() - reward_baseline
                rewards.append(torch.tensor(reward_value))
            else:
                # Use the average of the tensor elements as `score` is multiple elements
                reward_value = torch.mean(score).item() - reward_baseline
                rewards.append(torch.tensor(reward_value))
        return rewards
    
    @staticmethod
    def get_reward_model_output(reward_model, reward_tokenizer, question, answer, device):
        """
        Get the reward score for a given question and answer pair.
        """
        inputs = reward_tokenizer(question, answer, return_tensors='pt').to(device)
        score = reward_model(**inputs).logits[0].cpu().detach()
        return score

    def unwrap_model(self):
        '''返回nn.Module模块'''
        unwrap_model = self.accelerator.unwrap_model(self.model)
        if isinstance(unwrap_model, nn.Module): return unwrap_model
        return unwrap_model.module if hasattr(unwrap_model, 'module') else unwrap_model
