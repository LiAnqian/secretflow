#!/usr/bin/env python3
# *_* coding: utf-8 *_*

# Copyright 2022 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import copy
from typing import Callable, Tuple

import numpy as np
from secretflow.device import PYUObject, proxy
from secretflow.ml.nn.fl.backend.torch.fl_base import BaseTorchModel
from secretflow.ml.nn.fl.backend.torch.utils import TorchModel
from secretflow.ml.nn.fl.sparse import STCSparse
from secretflow.ml.nn.fl.strategy_dispatcher import register_strategy


class FedSTC(BaseTorchModel):
    """
    FedSTC: Sparse Ternary Compression (STC), a new compression framework that is speciﬁcally
    designed to meet the requirements of the Federated Learning environment. STC applies both
    sparsity and binarization in both upstream (client --> server) and downstream (server -->
    client) communication.
    """

    def __init__(self, builder_base: Callable[[], TorchModel]):
        super().__init__(builder_base)
        self._res = []

    def train_step(
        self,
        updates: np.ndarray,
        cur_steps: int,
        train_steps: int,
        **kwargs,
    ) -> Tuple[np.ndarray, int]:
        """Accept ps model params,then do local train

        Args:
            updates: global updates from params server
            cur_steps: current train step
            train_steps: local training steps
            kwargs: strategy-specific parameters
                sparsity: SparsityParameters,the ratio of masked elements, default is 0.0
        Returns:
            Parameters after local training
        """
        assert self.model is not None, "Model cannot be none, please give model define"
        dp_strategy = kwargs.get('dp_strategy', None)
        # prepare for the STC compression
        sparsity = kwargs.get('sparsity', 0.0)
        compressor = STCSparse(sparse_rate=sparsity)
        # update current weights
        if updates is not None:
            weights = [np.add(w, u) for w, u in zip(self.model_weights, updates)]
            self.model.update_weights(weights)
        num_sample = 0
        logs = {}
        # store current weights for residual computing
        self.model_weights = self.model.get_weights(return_numpy=True)

        for _ in range(train_steps):
            self.optimizer.zero_grad()
            iter_data = next(self.train_iter)
            if len(iter_data) == 2:
                x, y = iter_data
                s_w = None
            elif len(iter_data) == 3:
                x, y, s_w = iter_data

            num_sample += x.shape[0]
            y_t = y.argmax(dim=-1)
            y_pred = self.model(x)

            # do back propagation
            loss = self.loss(y_pred, y)
            loss.backward()
            self.optimizer.step()
            for m in self.metrics:
                m.update(y_pred, y_t)
        loss = loss.item()
        logs['train-loss'] = loss

        self.logs = self.transform_metrics(logs)
        self.epoch_logs = copy.deepcopy(self.logs)

        # do STC compression
        if self._res:
            client_updates = [
                np.add(np.subtract(new_w, old_w), res_u)
                for new_w, old_w, res_u in zip(
                    self.model.get_weights(return_numpy=True),
                    self.model_weights,
                    self._res,
                )
            ]
        else:
            # initial training res is zero
            client_updates = [
                np.subtract(new_w, old_w)
                for new_w, old_w in zip(
                    self.model.get_weights(return_numpy=True), self.model_weights
                )
            ]
        # DP operation
        if dp_strategy is not None:
            if dp_strategy.model_gdp is not None:
                client_updates_tensor = dp_strategy.model_gdp(client_updates)
                client_updates = [
                    client_updates_tensor[i] for i in range(len(client_updates))
                ]
        # do sparsity + binarization
        sparse_client_updates = compressor(client_updates)
        # compute new residual
        self._res = [
            np.subtract(dense_u, sparse_u)
            for dense_u, sparse_u in zip(client_updates, sparse_client_updates)
        ]
        self.model.update_weights(self.model_weights)
        return sparse_client_updates, num_sample


@register_strategy(strategy_name='fed_stc', backend='torch')
@proxy(PYUObject)
class PYUFedSTC(FedSTC):
    pass
