# Copyright (C) PROWLER.io 2019 - All Rights Reserved
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential

import itertools
from typing import List, Optional, Tuple, Type, Union

import tensorflow as tf

import gpflow
from gpflow.base import Module, TensorType

from gpflux.layers import LayerWithObservations, LikelihoodLayer
from gpflux.sampling.sample import Sample


class DeepGP(Module):
    """
    This class combines a sequential function model f(x) = fₙ(⋯ (f₂(f₁(x))))
    and a likelihood p(y|f). Layers may depend on both inputs x and targets y
    during training by inheriting from `LayerWithObservations`; those will
    be passed the argument `observations=[inputs, targets]`.

    Note that this class is not a `tf.keras.Model` subclass itself; to access
    Keras features, create a `Model` instance by calling :meth:`as_training_model`
    or :meth:`as_prediction_model` depending on the use-case; see their method
    doc strings for details.
    """

    def __init__(
        self,
        f_layers: List[tf.keras.layers.Layer],
        likelihood: Union[LikelihoodLayer, gpflow.likelihoods.Likelihood],
        *,
        input_dim: Optional[int] = None,
        target_dim: Optional[int] = None,
        default_model_class: Type[tf.keras.Model] = tf.keras.Model,
        num_data: Optional[int] = None,
    ):
        """
        :param f_layers: the layers [f₁, f₂, …, fₙ] describing the latent
            function f(x) = fₙ(⋯ (f₂(f₁(x)))).
        :param likelihood: the layer for the likelihood p(y|f); if this is a
            GPflow likelihood, will be wrapped in a LikelihoodLayer, or a
            LikelihoodLayer can be provided explicitly.
        :param input_dim: input dimensionality
        :param target_dim: target dimensionality
        :param default_model_class: `model_class` default for
            :meth:`as_training_model` and :meth:`as_prediction_model`
        :param num_data: number of data points (used by :meth:`elbo` to obtain
            correct scaling)
        """
        self.inputs = tf.keras.Input((input_dim,), name="inputs")
        self.targets = tf.keras.Input((target_dim,), name="targets")
        self.f_layers = f_layers
        if isinstance(likelihood, gpflow.likelihoods.Likelihood):
            self.likelihood_layer = LikelihoodLayer(likelihood)
        else:
            self.likelihood_layer = likelihood
        self.default_model_class = default_model_class
        self.num_data = self._validate_num_data(f_layers, num_data)

    @staticmethod
    def _validate_num_data(
        f_layers: List[tf.keras.layers.Layer], num_data: Optional[int] = None
    ) -> int:
        """
        Checks that the `num_data` attributes of all layers in `f_layers` are
        consistent with each other and with the (optional) `num_data` argument.
        :return: the validated number of data points
        """
        for i, layer in enumerate(f_layers):
            layer_num_data = getattr(layer, "num_data", None)
            if num_data is None:
                num_data = layer_num_data
            else:
                if layer_num_data is not None and num_data != layer_num_data:
                    raise ValueError(
                        f"f_layers[{i}].num_data is inconsistent with num_data={num_data}"
                    )
        if num_data is None:
            raise ValueError("Could not determine num_data; please provide explicitly")
        return num_data

    def _evaluate_deep_gp(
        self,
        inputs: TensorType,
        targets: Optional[TensorType],
        training: Optional[bool] = None,
    ) -> tf.Tensor:
        """
        Evaluates f(x) = fₙ(⋯ (f₂(f₁(x)))) on the `inputs`.

        Layers that inherit from `LayerWithObservations` will be passed an
        `observations` argument which is `[inputs, targets]` or `None`
        depending on whether `targets` contains a value or `None`.
        """
        features = inputs

        # NOTE: we cannot rely on the `training` flag here, as the correct
        # symbolic graph needs to be constructed at "build" time (before either
        # fit() or predict() get called).
        if targets is not None:
            observations = [inputs, targets]
        else:
            # TODO would it be better to simply pass [inputs, None] in this case?
            observations = None

        for layer in self.f_layers:
            if isinstance(layer, LayerWithObservations):
                features = layer(features, observations=observations, training=training)
            else:
                features = layer(features, training=training)
        return features

    def _evaluate_likelihood(
        self,
        f_outputs: TensorType,
        targets: Optional[TensorType],
        training: Optional[bool] = None,
    ) -> tf.Tensor:
        """
        Calls the `likelihood_layer` on `f_outputs`, which adds the
        corresponding layer loss when training.
        """
        return self.likelihood_layer(f_outputs, targets=targets, training=training)

    def call(
        self,
        inputs: TensorType,
        targets: Optional[TensorType] = None,
        training: Optional[bool] = None,
    ) -> tf.Tensor:
        f_outputs = self._evaluate_deep_gp(inputs, targets=targets, training=training)
        y_outputs = self._evaluate_likelihood(f_outputs, targets=targets, training=training)
        return y_outputs

    def predict_f(self, inputs: TensorType) -> Tuple[tf.Tensor, tf.Tensor]:
        """
        Returns mean and variance (not scale!) of f for compatibility with GPflow models.

        NOTE: Does not support `full_cov` or `full_output_cov`.
        """
        f_distribution = self._evaluate_deep_gp(inputs, targets=None)
        return f_distribution.loc, f_distribution.scale.diag ** 2

    def elbo(self, data: Tuple[TensorType, TensorType]) -> tf.Tensor:
        """
        Returns ELBO (not per-datapoint loss!) for compatibility with GPflow models.
        """
        X, Y = data
        _ = self.call(X, Y, training=True)
        all_losses = [
            loss
            for layer in itertools.chain(self.f_layers, [self.likelihood_layer])
            for loss in layer.losses
        ]
        return -tf.reduce_sum(all_losses) * self.num_data

    def _get_model_class(self, model_class: Optional[Type[tf.keras.Model]]) -> Type[tf.keras.Model]:
        if model_class is not None:
            return model_class
        else:
            return self.default_model_class

    def as_training_model(
        self, model_class: Optional[Type[tf.keras.Model]] = None
    ) -> tf.keras.Model:
        """
        Constructs a `tf.keras.Model` instance that requires both `inputs` and
        `targets` to be provided to its call. This is required for training the
        model, as the `likelihood_layer` (and `LayerWithObservations` instances
        such as `LatentVariableLayer`s, if present) needs to be passed the
        `targets`. When compiling the returned model, do NOT provide any
        additional losses.

        Train with
        ```
        model.compile(optimizer)  # do NOT pass a loss here
        model.fit({"inputs": X, "targets": Y}, ...)
        ```

        See https://keras.io/examples/keras_recipes/endpoint_layer_pattern/ for
        more details on this pattern.

        :param model_class: A class/constructor that has the same semantics as
            `tf.keras.Model.__init__`, accepting a list of inputs and an output.
            E.g., `tf.keras.Model` itself or `gpflux.optimization.NatGradModel`,
            but not `tf.keras.models.Sequential`.
        """
        model_class = self._get_model_class(model_class)
        outputs = self.call(self.inputs, self.targets)
        return model_class([self.inputs, self.targets], outputs)

    def as_prediction_model(
        self, model_class: Optional[Type[tf.keras.Model]] = None
    ) -> tf.keras.Model:
        """
        Constructs a `tf.keras.Model` instance that only requires `inputs`,
        which simplifies predictions.  Note that the returned model will not
        support training; for that, use `as_training_model`.

        Predict with
        ```
        model.predict(Xtest, ...)
        ```

        :param model_class: A class/constructor that has the same semantics as
            `tf.keras.Model.__init__`, accepting an input and an output.
            E.g., `tf.keras.Model` itself or `gpflux.optimization.NatGradModel`,
            but not `tf.keras.models.Sequential`.
        """
        model_class = self._get_model_class(model_class)
        outputs = self.call(self.inputs)
        return model_class(self.inputs, outputs)


def sample_dgp(model: DeepGP) -> Sample:  # TODO: should this be part of a [Vanilla]DeepGP class?
    function_draws = [layer.sample() for layer in model.f_layers]
    # TODO: error check that all layers implement .sample()?

    class ChainedSample(Sample):
        """Chains samples from consecutive layers."""

        def __call__(self, X: TensorType) -> tf.Tensor:
            for f in function_draws:
                X = f(X)
            return X

    return ChainedSample()
