from turtle import forward
from typing import Optional, Union

from opendelta.utils.signature import get_arg_names, get_arg_names_inside_func
from opendelta.utils.name_based_addressing import *
from opendelta.basemodel import DeltaBase
from transformers.models.t5 import T5ForConditionalGeneration
import loralib as lora
import torch.nn as nn
from opendelta import BaseDeltaConfig
import math

class LowRankLinear(nn.Module):
    #  ------------------------------------------------------------------------------------------
    #  Copyright (c) Microsoft Corporation. All rights reserved.
    #  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
    #  ------------------------------------------------------------------------------------------
    #  copy from loralib and do some refactor
    def __init__(self,
        in_features,
        out_features,
        weight,
        r=8, 
        lora_alpha=16,
        lora_dropout=0.0,
    ):
        super().__init__()
        self.r = r
        self.lora_alpha = lora_alpha
        self.lora_dropout = lora_dropout
        self.lin = nn.Linear(in_features, out_features) #
        if lora_dropout > 0.:
            self.lora_dropout = nn.Dropout(p=lora_dropout)
        else:
            self.lora_dropout = lambda x: x
        if r > 0:
            self.lora_A = nn.Parameter(weight.new_zeros((r, in_features)))
            self.lora_B = nn.Parameter(weight.new_zeros((out_features, r)))
            self.scaling = self.lora_alpha / self.r
            self.lin.reset_parameters() #
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
            nn.init.zeros_(self.lora_B)

    def forward(self, x):
        return (self.lora_dropout(x) @ self.lora_A.T @ self.lora_B.T) * self.scaling


class LoraConfig(BaseDeltaConfig):
    r"""
    This is the configuration class to store the configuration of a :py:class:`~LoraModel`

    """
    def __init__(
        self, 
        lora_r=8,
        lora_alpha=16,
        lora_dropout=0.0,
        **kwargs
    ): 
        super().__init__(**kwargs)
        arg_names = get_arg_names_inside_func(self.__init__)
        for arg_name in arg_names:
            if not hasattr(self, arg_name): # the arg has not been registered in parent config
                setattr(self, arg_name, locals()[arg_name])


class LoraModel(DeltaBase):
    r""" The implementation of `LoRA: Low-Rank Adaptation of Large Language Models <https://arxiv.org/abs/2106.09685>`_ .
    Thanks for their `loralib <https://github.com/microsoft/LoRA/tree/main/loralib>`_.
    
    .. note::
        In our implementation, we did not use loralib.linear to replace the linear layer of the backbone model.
        Instead, we insert a parallel module into the backbone.
        In other words, we treat :math:`(W + A^TB) X` as :math:`WX+ A^TBX`, and insert the :math:`A^TBX` as a parallel insertion module. 
        If you want to use the original implementation, please refer to `lora_old.py`

    class attributes:
        - default_modified_modules = ['attn.q', 'attn.v'] According to the paper, they modify q and v matrix in the
        attention layer. However, other linears can also be modified, and may lead to better performance. 
        
        .. note::
            modified_modules should point to linear layer. We currently don't support broadcast to all linears in 
            a module's child modules.

        - delta_type = "lora"


    Args:
        backbone_model (:obj:`transformers.PretrainedModels`): The backbone model to be modified. 
        lora_r (:obj:`int`, *optional*): the rank of the lora parameters. The smaller lora_r is , the fewer parameters lora has.
        lora_alpha (:obj:`int`, *optional*): A hyper-parameter to control the init scale of loralib.linear .
        lora_dropout (:obj:`float`, *optional*): The dropout rate in lora.linear.
        modified_modules (:obj:`List[str]`): For prefix tuning, the it must refer to an attention layer (Currently, only
                        the implemented ones)
        unfrozen_modules (:obj:`List[str]`, *optional*, default to :obj:`None`): The modules that should be unfrozen
                         together with the prefix parameters.
        common_structure (:obj:`bool`): whether using name-based addressing with a common structure mapping.

    """

    config_class = LoraConfig
    delta_type = "lora"
    default_modified_modules = ['attn.q', 'attn.v']
    def __init__(self,
                 backbone_model: nn.Module, 
                 lora_r=8,
                 lora_alpha=16,
                 lora_dropout=0.0,
                 modified_modules: Optional[bool] = None,
                 unfrozen_modules: Optional[bool] = None,
                 common_structure: Optional[bool] = None,
                 interactive_modify: Optional[Union[bool, int]] = False,
                 ):
        DeltaBase.__init__(self, 
                           backbone_model, 
                           modified_modules=modified_modules,
                           unfrozen_modules=unfrozen_modules,
                           common_structure=common_structure,
                           interactive_modify=interactive_modify,
                           )
        arg_names = get_arg_names_inside_func(self.__init__)
        for arg_name in arg_names:
            if not hasattr(self, arg_name): # not registered in parent class
                setattr(self, arg_name, locals()[arg_name])

        self.delta_modules = nn.ModuleList()

        self.add_all_delta_to_backbone(self.backbone_model,
                                   self.modified_modules,
                                   )
    
    
    def update_module(self, module: nn.Module, key: str):
        parent_ref, child_name, child_ref = self.find_module(module, key)
        parallel_module = self.new_module_like(child_module=child_ref)
        self.insert_parallel_module(child_ref, delta_module=parallel_module, delta_name="lora")
        
    def _pseudo_data_to_instantiate(self, module):
        # no need to pass pseudo input, so overwrite it
        pass

    def new_module_like(self, child_module):
        if isinstance(child_module, nn.Linear):
            in_features, out_features = child_module.in_features, child_module.out_features
            new_module = LowRankLinear(in_features = in_features, 
                                     out_features = out_features, 
                                     weight = child_module.weight,
                                     r=self.lora_r, 
                                     lora_alpha=self.lora_alpha,
                                     lora_dropout=self.lora_dropout)
            self.delta_modules.append(new_module)  
        else:
            raise NotImplementedError
        return new_module