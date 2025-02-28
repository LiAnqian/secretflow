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


from pathlib import Path
from abc import ABC, abstractmethod
from typing import Callable, Optional

import numpy as np
import torch
import torchmetrics

from secretflow.ml.nn.fl.backend.torch.sampler import sampler_data
from secretflow.ml.nn.fl.backend.torch.utils import TorchModel
from secretflow.ml.nn.fl.metrics import Default, Mean, Precision, Recall
from secretflow.utils.io import rows_count

# Torch model on worker side


class BaseTorchModel(ABC):
    def __init__(self, builder_base: Callable[[], TorchModel]):
        assert builder_base is not None, "Builder_base cannot be none"
        self.model = (
            builder_base.model_fn() if builder_base.model_fn is not None else None
        )
        self.loss = builder_base.loss_fn() if builder_base.loss_fn is not None else None
        self.optimizer = (
            builder_base.optim_fn(self.model.parameters())
            if builder_base.optim_fn is not None
            else None
        )
        self.metrics = (
            [m() for m in builder_base.metrics]
            if builder_base.metrics is not None
            else None
        )
        self.train_data_loader = None
        self.eval_data_loader = None
        self.callbacks = None
        self.logs = None
        self.epoch_logs = None
        self.training_logs = None
        self.history = {}
        self.train_set = None
        self.eval_set = None

    def build_dataset_from_csv(
        self,
        csv_file_path: str,
        label: str,
        sampling_rate=None,
        shuffle=False,
        random_seed=1234,
        na_value="?",
        repeat_count=1,
        sample_length=0,
        buffer_size=None,
        ignore_errors=True,
        prefetch_buffer_size=None,
        stage="train",
        label_decoder=None,
    ):
        """build torch.dataloader

        Args:
            csv_file_path: Dict of csv file path
            label: label column name
            sampling_rate: Sampling rate of a batch
            shuffle: A bool that indicates whether the input should be shuffled
            random_seed: Randomization seed to use for shuffling.
            na_value: Additional string to recognize as NA/NaN.
            repeat_count: num of repeats
            sample_length: num of sample length
            buffer_size: shuffle size
            ignore_errors: if `True`, ignores errors with CSV file parsing,
            prefetch_buffer_size: An int specifying the number of feature batches to prefetch for performance improvement.
            stage: the stage of the datset
            label_decoder: callable function for label preprocess
        """
        raise Exception("CSV incremental loader is not supported yet")

    def build_dataset(
        self,
        x: np.ndarray,
        y: Optional[np.ndarray] = None,
        s_w: Optional[np.ndarray] = None,
        sampling_rate=None,
        buffer_size=None,
        shuffle=False,
        random_seed=1234,
        repeat_count=1,
        sampler_method="batch",
        stage="train",
    ):
        """build torch.dataloader

        Args:
            x: feature, FedNdArray or HDataFrame
            y: label, FedNdArray or HDataFrame
            s_w: sample weight of this dataset
            sampling_rate: Sampling rate of a batch
            buffer_size: shuffle size
            shuffle: A bool that indicates whether the input should be shuffled
            random_seed: Prg seed for shuffling
            repeat_count: num of repeats
            sampler: method of sampler
        """
        if x is None or len(x.shape) == 0:
            raise Exception("Data 'x' cannot be None")

        assert sampling_rate is not None, "Sampling rate cannot be None"
        if x is not None and y is not None:
            assert (
                x.shape[0] == y.shape[0]
            ), "The samples of feature is different with label"

        data_set = sampler_data(
            sampler_method,
            x,
            y,
            s_w,
            sampling_rate,
            buffer_size,
            shuffle,
            repeat_count,
            random_seed,
        )
        if stage == "train":
            self.train_set = data_set
        elif stage == "eval":
            self.eval_set = data_set
        else:
            raise Exception(f"Illegal argument stage={stage}")

    def get_rows_count(self, filename):
        return int(rows_count(filename=filename)) - 1  # except header line

    def get_weights(self):
        return self.model.get_weights(return_numpy=True)

    def set_weights(self, weights):
        """set weights of client model"""
        self.model.update_weights(weights)

    def set_validation_metrics(self, global_metrics):
        self.epoch_logs.update(global_metrics)

    def wrap_local_metrics(self):
        # TODO: use pytorch to rewrite
        wraped_metrics = []
        for m in self.metrics:
            if isinstance(m, (torchmetrics.Accuracy)):
                tp, fp, tn, fn = m._get_final_stats()
                name = m._get_name().lower()
                correct = float((tp + tn).numpy().sum())
                total = float((tp + tn + fp + fn).numpy().sum())

                wraped_metrics.append(Mean(name, correct, total))

            elif isinstance(m, torchmetrics.Precision):
                tp, fp, tn, fn = m._get_final_stats()
                threshold = m.threshold

                wraped_metrics.append(
                    Precision(
                        m._get_name().lower(),
                        [threshold],
                        [float(tp.numpy().sum())],
                        [float(fp.numpy().sum())],
                    )
                )
            elif isinstance(m, torchmetrics.Recall):
                tp, fp, tn, fn = m._get_final_stats()
                threshold = m.threshold
                wraped_metrics.append(
                    Recall(
                        m._get_name().lower(),
                        [threshold],
                        tp.numpy().sum(),
                        fn.numpy().sum(),
                    )
                )
            else:
                # only do naive aggregate
                metrics_value = m.compute()
                wraped_metrics.append(
                    Default(
                        name=m._get_name().lower(),
                        total=metrics_value,
                        count=1,
                    )
                )

        return wraped_metrics

    def evaluate(self, evaluate_steps=0):
        assert evaluate_steps > 0, "Evaluate_steps must greater than 0"
        assert self.model is not None, "Model cannot be none, please give model define"
        assert (
            len(self.metrics) > 0
        ), "Metric cannot be none, please give metric by 'TorchModel'"
        self.model.eval()

        # reset all metrics
        self.eval_iter = iter(self.eval_set)
        for m in self.metrics:
            m.reset()
        with torch.no_grad():
            for _ in range(evaluate_steps):
                iter_data = next(self.eval_iter)
                if len(iter_data) == 2:
                    x, y = iter_data
                    s_w = None
                elif len(iter_data) == 3:
                    x, y, s_w = iter_data
                x = x.float()
                # Step 1: forward pass
                y_pred = self.model(x)

                # Step 2: update metrics
                if len(y.shape) == 1:
                    y_t = y
                else:
                    if y.shape[-1] == 1:
                        y_t = torch.squeeze(y, -1).long()
                    else:
                        y_t = y.argmax(dim=-1)
                for m in self.metrics:
                    m.update(y_pred, y_t)
            result = {}
            self.transform_metrics(result, stage="eval")
        return self.wrap_local_metrics()

    def predict(
        self,
        predict_steps=0,
    ):
        assert (
            self.model is not None
        ), "Please do training first or provide a trained model"
        pred_result = []
        self.eval_iter = iter(self.eval_set)
        assert self.eval_iter is not None, "self.eval_set must be initialized"
        for _ in range(predict_steps):
            iter_data = next(self.eval_iter)
            x = iter_data[0]
            y_pred = self.model(x)
            pred_result.extend(y_pred)
        return pred_result

    def init_training(self, callbacks, epochs=1, steps=0, verbose=0):
        assert self.model is not None, "model cannot be none, please give model define"
        if callbacks is not None:
            raise Exception("Callback is not supported yet")

    def on_train_begin(self):
        self.training_logs = {}
        self.epoch = []

    def on_epoch_begin(self, epoch):
        self._current_epoch = epoch
        self.epoch_logs = {}
        for m in self.metrics:
            m.reset()
        if self.train_set is not None:
            self.train_iter = iter(self.train_set)
        if self.eval_set is not None:
            self.eval_iter = iter(self.eval_set)

    def on_epoch_end(self, epoch):
        self.epoch.append(epoch)

        for k, v in self.epoch_logs.items():
            self.history.setdefault(k, []).append(v)
        self.training_logs = self.epoch_logs

        return self.epoch_logs

    def transform_metrics(self, logs, stage="train"):
        for m in self.metrics:
            result = m.compute()
            logs[f'{stage}_{m._get_name().lower()}'] = result
        return logs

    def on_train_end(self):
        return self.history

    def get_stop_training(self):
        return False  # is not supported

    @abstractmethod
    def train_step(self, weights, cur_steps, train_steps, **kwargs):
        pass

    def save_model(self, model_path: str):
        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        assert model_path is not None, "model path cannot be empty"
        torch.save(self.model, model_path)

    def load_model(self, model_path: str):
        assert model_path is not None, "model path cannot be empty"
        self.model = torch.load(model_path)
