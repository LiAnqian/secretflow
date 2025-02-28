#!/usr/bin/env python3
# *_* coding: utf-8 *_*

"""module docstring - short summary

If the description is long, the first line should be a short summary that makes sense on its own,
separated from the rest by a newline

"""
import os
import tempfile

import numpy as np

from secretflow.data.ndarray import load
from secretflow.ml.nn import SLModel
from secretflow.security.privacy import DPStrategy, GaussianEmbeddingDP, LabelDP
from tests.basecase import DeviceTestCase
from secretflow.utils.simulation.datasets import load_mnist
from secretflow.device import reveal
from secretflow.utils.compressor import RandomSparse, TopkSparse

_temp_dir = tempfile.mkdtemp()

NUM_CLASSES = 10
INPUT_SHAPE = (28, 28, 1)


def create_base_model(input_dim, output_dim, output_num, name='base_model'):
    # Create model
    def create_model():
        from tensorflow import keras

        inputs = keras.Input(shape=input_dim)
        conv = keras.layers.Conv2D(filters=2, kernel_size=(3, 3))(inputs)
        pooling = keras.layers.MaxPooling2D()(conv)
        flatten = keras.layers.Flatten()(pooling)
        dropout = keras.layers.Dropout(0.5)(flatten)
        output_layers = [
            keras.layers.Dense(output_dim)(dropout) for _ in range(output_num)
        ]

        model = keras.Model(inputs, output_layers)

        # Compile model
        model.compile(
            loss='categorical_crossentropy', optimizer='adam', metrics=["accuracy"]
        )
        return model  # need wrap

    return create_model


def create_fuse_model(input_dim, output_dim, party_nums, input_num, name='fuse_model'):
    def create_model():
        from tensorflow import keras
        from tensorflow.keras import layers

        # input
        input_layers = []
        for i in range(party_nums * input_num):
            input_layers.append(
                keras.Input(
                    input_dim,
                )
            )
        # user define hidden process logic
        merged_layer = layers.concatenate(input_layers)

        fuse_layer = layers.Dense(64, activation='relu')(merged_layer)
        output = layers.Dense(output_dim, activation='softmax')(fuse_layer)
        # Create model
        model = keras.Model(inputs=input_layers, outputs=output)
        # Compile model
        model.compile(
            loss=['categorical_crossentropy'],
            optimizer='adam',
            metrics=["accuracy"],
        )
        return model

    return create_model


num_classes = 10
input_shape = (28, 28, 1)
hidden_size = 64
train_batch_size = 128


class TestSLModelTensorflow(DeviceTestCase):
    def keras_model_with_mnist(
        self, base_model_dict, device_y, model_fuse, data, label, **kwargs
    ):
        # kwargs parsing
        dp_strategy_dict = kwargs.get('dp_strategy_dict', None)
        compressor = kwargs.get('compressor', None)

        party_shape = data.partition_shape()
        alice_length = party_shape[self.alice][0]
        bob_length = party_shape[self.bob][0]

        sl_model = SLModel(
            base_model_dict=base_model_dict,
            device_y=device_y,
            model_fuse=model_fuse,
            dp_strategy_dict=dp_strategy_dict,
            compressor=compressor,
        )

        history = sl_model.fit(
            data,
            label,
            validation_data=(data, label),
            epochs=2,
            batch_size=train_batch_size,
            shuffle=True,
            random_seed=1234,
        )
        global_metric = sl_model.evaluate(
            data,
            label,
            batch_size=128,
            random_seed=1234,
        )
        alice_arr = self.alice(lambda: np.zeros(alice_length))()
        bob_arr = self.bob(lambda: np.zeros(bob_length))()
        sample_weights = load({self.alice: alice_arr, self.bob: bob_arr})
        zero_metric = sl_model.evaluate(
            data,
            label,
            sample_weight=sample_weights,
            batch_size=128,
            random_seed=1234,
        )
        # test history
        self.assertAlmostEqual(
            global_metric['accuracy'], history['val_accuracy'][-1], 1
        )
        self.assertEquals(zero_metric['loss'], 0.0)
        self.assertGreater(global_metric['accuracy'], 0.8)
        result = sl_model.predict(data, batch_size=128, verbose=1)
        reveal_result = []
        for rt in result:
            reveal_result.extend(reveal(rt))
        self.assertEquals(len(reveal_result), alice_length)
        base_model_path = os.path.join(_temp_dir, "base_model")
        fuse_model_path = os.path.join(_temp_dir, "fuse_model")
        sl_model.save_model(
            base_model_path=base_model_path,
            fuse_model_path=fuse_model_path,
            is_test=True,
        )
        self.assertIsNotNone(os.path.exists(base_model_path))
        self.assertIsNotNone(os.path.exists(fuse_model_path))

        sl_model_load = SLModel(
            base_model_dict=base_model_dict,
            device_y=device_y,
            model_fuse=model_fuse,
            dp_strategy_dict=dp_strategy_dict,
            compressor=compressor,
        )
        sl_model_load.load_model(
            base_model_path=base_model_path,
            fuse_model_path=fuse_model_path,
            is_test=True,
        )
        reload_metric = sl_model_load.evaluate(
            data,
            label,
            batch_size=128,
            random_seed=1234,
        )
        self.assertAlmostEqual(global_metric['accuracy'], reload_metric['accuracy'], 1)
        self.assertAlmostEqual(global_metric['loss'], reload_metric['loss'], 1)

    def test_single_output_model(self):
        num_samples = 10000
        (x_train, y_train), (_, _) = load_mnist(
            parts={
                self.alice: (0, num_samples),
                self.bob: (0, num_samples),
            },
            normalized_x=True,
            categorical_y=True,
        )
        # prepare model
        num_classes = 10

        input_shape = (28, 28, 1)
        # keras model
        base_model = create_base_model(input_shape, 64, output_num=1)
        base_model_dict = {
            self.alice: base_model,
            self.bob: base_model,
        }
        fuse_model = create_fuse_model(
            input_dim=hidden_size,
            input_num=1,
            party_nums=len(base_model_dict),
            output_dim=num_classes,
        )

        # Define DP operations
        gaussian_embedding_dp = GaussianEmbeddingDP(
            noise_multiplier=0.5,
            l2_norm_clip=1.0,
            batch_size=train_batch_size,
            num_samples=num_samples,
            is_secure_generator=False,
        )
        dp_strategy_alice = DPStrategy(embedding_dp=gaussian_embedding_dp)
        label_dp = LabelDP(eps=64.0)
        dp_strategy_bob = DPStrategy(label_dp=label_dp)
        dp_strategy_dict = {self.alice: dp_strategy_alice, self.bob: dp_strategy_bob}
        dp_spent_step_freq = 10

        print("test dp strategy")
        self.keras_model_with_mnist(
            data=x_train,
            label=y_train,
            base_model_dict=base_model_dict,
            model_fuse=fuse_model,
            device_y=self.bob,
            dp_strategy_dict=dp_strategy_dict,
            dp_spent_step_freq=dp_spent_step_freq,
            acc=0.82,
        )

        # test compressor
        print("test TopkSparse")
        top_k_compressor = TopkSparse(0.5)
        self.keras_model_with_mnist(
            data=x_train,
            label=y_train,
            base_model_dict=base_model_dict,
            model_fuse=fuse_model,
            device_y=self.bob,
            compressor=top_k_compressor,
        )
        print("test RandomSparse")
        random_sparse = RandomSparse(0.5)
        self.keras_model_with_mnist(
            data=x_train,
            label=y_train,
            base_model_dict=base_model_dict,
            model_fuse=fuse_model,
            device_y=self.bob,
            compressor=random_sparse,
        )

    def test_multi_output_model(self):
        (x_train, y_train), (_, _) = load_mnist(
            parts={
                self.alice: (0, 10000),
                self.bob: (0, 10000),
            },
            normalized_x=True,
            categorical_y=True,
        )
        # prepare model
        num_classes = 10
        input_shape = (28, 28, 1)
        basenet_output = 2

        # keras model
        base_model = create_base_model(input_shape, 64, output_num=basenet_output)
        base_model_dict = {
            self.alice: base_model,
            self.bob: base_model,
        }
        fuse_model = create_fuse_model(
            input_dim=hidden_size,
            input_num=basenet_output,
            party_nums=len(base_model_dict),
            output_dim=num_classes,
        )

        self.keras_model_with_mnist(
            data=x_train,
            label=y_train,
            base_model_dict=base_model_dict,
            model_fuse=fuse_model,
            device_y=self.bob,
        )
