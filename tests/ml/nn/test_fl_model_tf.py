#!/usr/bin/env python3
# *_* coding: utf-8 *_*

"""module docstring - short summary

If the description is long, the first line should be a short summary that makes sense on its own,
separated from the rest by a newline

"""

import functools
import os
import tempfile

import numpy as np
import tensorflow as tf

from secretflow.data.ndarray import load
from secretflow.ml.nn import FLModel
from secretflow.preprocessing.encoder import OneHotEncoder
from secretflow.security.aggregation import PlainAggregator
from secretflow.security.compare import PlainComparator
from secretflow.utils.simulation.datasets import load_iris, load_mnist
from secretflow.security.privacy import DPStrategyFL, GaussianModelDP
from secretflow.device import reveal

from tests.basecase import DeviceTestCase

_temp_dir = tempfile.mkdtemp()

NUM_CLASSES = 10
INPUT_SHAPE = (28, 28, 1)


# model define for mlp
def create_nn_model(input_dim, output_dim, nodes, n=1, name='model'):
    def create_model():
        from tensorflow import keras
        from tensorflow.keras import layers

        # Create model
        model = keras.Sequential(name=name)
        for i in range(n):
            model.add(layers.Dense(nodes, input_dim=input_dim, activation='relu'))
        model.add(layers.Dense(output_dim, activation='softmax'))

        # Compile model
        model.compile(
            loss='categorical_crossentropy', optimizer='adam', metrics=["accuracy"]
        )
        return model

    return create_model


# model define for cnn
def create_conv_model(input_shape, num_classes, name='model'):
    def create_model():
        from tensorflow import keras
        from tensorflow.keras import layers

        # Create model
        model = keras.Sequential(
            [
                keras.Input(shape=input_shape),
                layers.Conv2D(32, kernel_size=(3, 3), activation="relu"),
                layers.MaxPooling2D(pool_size=(2, 2)),
                layers.Conv2D(64, kernel_size=(3, 3), activation="relu"),
                layers.MaxPooling2D(pool_size=(2, 2)),
                layers.Flatten(),
                layers.Dropout(0.5),
                layers.Dense(num_classes, activation="softmax"),
            ]
        )
        # Compile model
        model.compile(
            loss='categorical_crossentropy', optimizer='adam', metrics=["accuracy"]
        )
        return model

    return create_model


class TestFedModelDF(DeviceTestCase):
    def keras_model_for_iris(self):
        """unittest ignore"""
        aggregator = PlainAggregator(self.carol)
        comparator = PlainComparator(self.carol)
        hdf = load_iris(
            parts=[self.alice, self.bob],
            aggregator=aggregator,
            comparator=comparator,
        )

        label = hdf['class']
        # do preprocess
        encoder = OneHotEncoder()
        label = encoder.fit_transform(label)

        data = hdf.drop(columns='class', inplace=False)
        data = data.fillna(data.mean(numeric_only=True).to_dict())

        # prepare model
        n_features = 4
        n_classes = 3
        model = create_nn_model(n_features, n_classes, 8, 3)

        device_list = [self.alice, self.bob]
        fed_model = FLModel(
            device_list=device_list,
            model=model,
            aggregator=aggregator,
            sampler="batch",
        )
        fed_model.fit(data, label, epochs=5, batch_size=16, aggregate_freq=3)
        global_metric, _ = fed_model.evaluate(data, label, batch_size=16)
        print(global_metric)
        self.assertGreater(global_metric[1].result().numpy(), 0.7)


class TestFedModelCSV(DeviceTestCase):
    def test_keras_model(self):
        aggregator = PlainAggregator(self.carol)
        train_data = load_iris(parts=[self.alice, self.bob], aggregator=aggregator)
        _, alice_path = tempfile.mkstemp()
        _, bob_path = tempfile.mkstemp()
        train_path = {self.alice: alice_path, self.bob: bob_path}
        train_data.to_csv(train_path, index=False)

        valid_path = train_path

        def label_decoder(x, label, num_class):
            # How to deal with label column
            vocab = ["Iris-setosa", "Iris-versicolor", "Iris-virginica"]
            layer = tf.keras.layers.StringLookup(vocabulary=vocab)
            label = layer(label)
            one_hot_label = tf.one_hot(label, depth=num_class, axis=-1)
            return x, one_hot_label

        # prepare model
        n_features = 4
        n_classes = 3
        onehot_func = functools.partial(label_decoder, num_class=n_classes)
        model = create_nn_model(n_features, n_classes, 8, 3)
        device_list = [self.alice, self.bob]
        fed_model = FLModel(device_list=device_list, model=model, aggregator=aggregator)

        fed_model.fit(
            train_path,
            "class",
            epochs=2,
            validation_data=valid_path,
            validation_freq=1,
            label_decoder=onehot_func,
            batch_size=32,
            aggregate_freq=2,
        )
        global_metric, _ = fed_model.evaluate(
            valid_path,
            "class",
            label_decoder=onehot_func,
            batch_size=16,
        )
        print(global_metric[1].result().numpy())


class TestFedModelTensorflow(DeviceTestCase):
    def keras_model_with_mnist(self, model, data, label, strategy, backend, **kwargs):
        aggregator = PlainAggregator(self.carol)
        party_shape = data.partition_shape()
        alice_length = party_shape[self.alice][0]
        bob_length = party_shape[self.bob][0]

        alice_arr = self.alice(lambda: np.zeros(alice_length))()
        bob_arr = self.bob(lambda: np.zeros(bob_length))()
        sample_weights = load({self.alice: alice_arr, self.bob: bob_arr})

        # spcify params
        sampler_method = kwargs.get('sampler_method', "batch")
        dp_spent_step_freq = kwargs.get('dp_spent_step_freq', None)
        device_list = [self.alice, self.bob]

        fed_model = FLModel(
            server=self.carol,
            device_list=device_list,
            model=model,
            aggregator=aggregator,
            backend=backend,
            strategy=strategy,
            **kwargs,
        )
        random_seed = 1524
        history = fed_model.fit(
            data,
            label,
            validation_data=(data, label),
            epochs=5,
            batch_size=128,
            aggregate_freq=2,
            sampler_method=sampler_method,
            random_seed=random_seed,
            dp_spent_step_freq=dp_spent_step_freq,
        )
        global_metric, _ = fed_model.evaluate(
            data,
            label,
            batch_size=128,
            sampler_method=sampler_method,
            random_seed=random_seed,
        )
        self.assertEquals(
            global_metric[1].result().numpy(),
            history.global_history['val_accuracy'][-1],
        )
        self.assertGreater(global_metric[1].result().numpy(), 0.8)
        zero_metric, _ = fed_model.evaluate(
            data,
            label,
            sample_weight=sample_weights,
            batch_size=128,
            sampler_method=sampler_method,
            random_seed=random_seed,
        )
        # test sample_weight validation
        self.assertEquals(zero_metric[0].result(), 0.0)
        result = fed_model.predict(data, batch_size=128, random_seed=random_seed)
        self.assertEquals(len(reveal(result[self.alice])), alice_length)

        model_path_test = os.path.join(_temp_dir, "base_model")
        fed_model.save_model(model_path=model_path_test, is_test=True)
        model_path_dict = {
            self.alice: os.path.join(_temp_dir, "alice_model"),
            self.bob: os.path.join(_temp_dir, "bob_model"),
        }
        fed_model.save_model(model_path=model_path_dict, is_test=False)

        # test load model
        fed_model.load_model(model_path=model_path_test, is_test=True)
        fed_model.load_model(model_path=model_path_dict, is_test=False)
        reload_metric, _ = fed_model.evaluate(
            data,
            label,
            batch_size=128,
            sampler_method=sampler_method,
            random_seed=random_seed,
        )
        np.testing.assert_equal(
            [m.result().numpy() for m in global_metric],
            [m.result().numpy() for m in reload_metric],
        )

    def test_keras_model(self):
        (_, _), (mnist_data, mnist_label) = load_mnist(
            parts={self.alice: 0.4, self.bob: 0.6},
            normalized_x=True,
            categorical_y=True,
        )
        # prepare model
        num_classes = 10

        input_shape = (28, 28, 1)
        # keras model
        model = create_conv_model(input_shape, num_classes)

        # test fed avg w
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_avg_w",
            backend="tensorflow",
        )
        # test fed avg w with possion sampler
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_avg_w",
            backend="tensorflow",
            sampler_method='possion',
        )
        # test fed avg g
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_avg_g",
            backend="tensorflow",
        )
        # test fed avg u
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_avg_u",
            backend="tensorflow",
        )
        # test fed prox
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_prox",
            backend="tensorflow",
            mu=0.1,
        )
        # test fed stc
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_stc",
            backend="tensorflow",
            sparsity=0.9,
        )
        # test fed scr
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_scr",
            backend="tensorflow",
        )

        # Define DP operations
        gaussian_model_gdp = GaussianModelDP(
            noise_multiplier=0.001,
            l2_norm_clip=0.1,
            num_clients=2,
            is_secure_generator=False,
        )
        dp_strategy_fl = DPStrategyFL(model_gdp=gaussian_model_gdp)
        dp_spent_step_freq = 10
        self.keras_model_with_mnist(
            data=mnist_data,
            label=mnist_label,
            model=model,
            strategy="fed_avg_g",
            dp_strategy=dp_strategy_fl,
            dp_spent_step_freq=dp_spent_step_freq,
            backend="tensorflow",
        )
