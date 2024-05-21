import torch
from torch4keras.trainer import Trainer
from bert4torch.models import BaseModel, build_transformer_model
import copy
from typing import Literal, Optional, Dict, Union
from contextlib import contextmanager, nullcontext
import warnings
import inspect
from bert4torch.snippets import is_peft_available, disable_dropout_in_model, peft_module_casting_to_bf16, DottableDict
if is_peft_available():
    from peft import PeftModel, get_peft_model, prepare_model_for_kbit_training


class DPOTrainer(Trainer):
    '''使用dpo算法进行人类偏好对齐

    :param model: 待训练模型
    :param ref_model: 参考模型
    '''
    def __init__(
        self, 
        model: Optional[Union[BaseModel, str]], 
        ref_model:BaseModel=None,
        args: Optional[DottableDict] = None,
        model_init_kwargs: Optional[Dict] = None,
        ref_model_init_kwargs: Optional[Dict] = None,
        model_adapter_name: Optional[str] = None,
        ref_adapter_name: Optional[str] = None,
        peft_config: Optional[Dict] = None,
        disable_dropout: bool = True,
        force_use_ref_model: bool = False,
        ):
        super().__init__()

        if model_init_kwargs is None:
            model_init_kwargs = {}
        elif not isinstance(model, str):
            raise ValueError("You passed model_kwargs to the DPOTrainer. But your model is already instantiated.")

        if ref_model_init_kwargs is None:
            ref_model_init_kwargs = {}
        elif not isinstance(ref_model, str):
            raise ValueError(
                "You passed ref_model_kwargs to the DPOTrainer. But your ref_model is already instantiated."
            )

        if isinstance(model, str):
            warnings.warn(
                "You passed a model_id to the DPOTrainer. This will automatically create an "
                "`build_transformer_model` or a `PeftModel` (if you passed a `peft_config`) for you."
            )
            model = build_transformer_model(checkpoint_path=model, **model_init_kwargs).to(self.device)

        if isinstance(ref_model, str):
            warnings.warn(
                "You passed a ref model_id to the DPOTrainer. This will automatically create an `build_transformer_model`"
            )
            ref_model = build_transformer_model(checkpoint_path=ref_model, **ref_model_init_kwargs)

        self._peft_has_been_casted_to_bf16 = False

        if not is_peft_available() and peft_config is not None:
            raise ValueError(
                "PEFT is not installed and you passed a `peft_config` in the trainer's kwargs, please install it to use the PEFT models"
            )
        elif is_peft_available() and peft_config is not None:
            # if model is a peft model and we have a peft_config, we merge and unload it first
            if isinstance(model, PeftModel):
                model = model.merge_and_unload()

            if ref_model is not None and not force_use_ref_model:
                raise ValueError(
                    "You passed both a ref_model and a peft_config. For training PEFT adapters with DPO there is no need to pass a reference"
                    " model. Please pass `ref_model=None` in case you want to train PEFT adapters, or pass a ref_model with `force_use_ref_model=True` in DPOTrainer's init."
                    " if you want to use a different ref_model."
                )

            if getattr(model, "is_loaded_in_8bit", False) or getattr(model, "is_loaded_in_4bit", False):
                _support_gc_kwargs = hasattr(
                    args, "gradient_checkpointing_kwargs"
                ) and "gradient_checkpointing_kwargs" in list(
                    inspect.signature(prepare_model_for_kbit_training).parameters
                )

                prepare_model_kwargs = {"use_gradient_checkpointing": args.gradient_checkpointing}

                if _support_gc_kwargs:
                    prepare_model_kwargs["gradient_checkpointing_kwargs"] = args.gradient_checkpointing_kwargs

                model = prepare_model_for_kbit_training(model, **prepare_model_kwargs)
            elif getattr(args, "gradient_checkpointing", False):
                # For backward compatibility with older versions of transformers
                if hasattr(model, "enable_input_require_grads"):
                    model.enable_input_require_grads()
                else:

                    def make_inputs_require_grad(module, input, output):
                        output.requires_grad_(True)

                    model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

            # get peft model with the given config
            model = get_peft_model(model, peft_config)
            if getattr(args, 'bf16', False) and getattr(model, "is_loaded_in_4bit", False):
                peft_module_casting_to_bf16(model)
                # If args.bf16 we need to explicitly call `generate` with torch amp autocast context manager
                self._peft_has_been_casted_to_bf16 = True

        # For models that use gradient_checkpointing, we need to attach a hook that enables input
        # to explicitly have `requires_grad=True`, otherwise training will either silently
        # fail or completely fail.
        elif getattr(args, "gradient_checkpointing", False):
            # For backward compatibility with older versions of transformers
            if hasattr(model, "enable_input_require_grads"):
                model.enable_input_require_grads()
            else:

                def make_inputs_require_grad(module, input, output):
                    output.requires_grad_(True)

                model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

        self.is_peft_model = is_peft_available() and isinstance(model, PeftModel)
        self.model_adapter_name = model_adapter_name
        self.ref_adapter_name = ref_adapter_name

        self.model = model
        self.model.print_trainable_parameters()
        if ref_model:
            self.ref_model = ref_model
            for p in self.ref_model.parameters():
                p.requires_grad = False
            self.ref_model.print_trainable_parameters()
            self.ref_model.eval()
        else:
            self.ref_model = None

        if disable_dropout:
            disable_dropout_in_model(model)
            if self.ref_model is not None:
                disable_dropout_in_model(self.ref_model)
    
    def _forward(self, *inputs, **input_kwargs):
        '''修改父类的_forward来获取输出'''
        policy_logits = self._argparse_forward(self.model, *inputs, **input_kwargs).to(torch.float32)
        with torch.no_grad():
            if self.ref_model is None:
                with self.null_ref_context():
                    reference_logits = self._argparse_forward(self.model, *inputs, **input_kwargs).to(torch.float32)
            else:
                reference_logits = self._argparse_forward(self.ref_model, *inputs, **input_kwargs).to(torch.float32)
        
        return policy_logits, reference_logits

    @contextmanager
    def null_ref_context(self):
        """Context manager for handling null reference model (that is, peft adapter manipulation)."""
        with self.unwrap_model().disable_adapter() if self.is_peft_model and not self.ref_adapter_name else nullcontext():
            if self.ref_adapter_name:
                self.model.set_adapter(self.ref_adapter_name)
            yield
            if self.ref_adapter_name:
                self.model.set_adapter(self.model_adapter_name or "default")

    def unwrap_model(self):
        '''返回nn.Module模块
        '''
        return self.model