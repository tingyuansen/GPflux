# Copyright (C) PROWLER.io 2018 - All Rights Reserved
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential


import gpflow
import numpy as np
import tensorflow as tf

from scipy.stats import norm
from functools import reduce

from typing import Optional, List

from gpflow import settings
from gpflow.decors import params_as_tensors, autoflow
from gpflow.likelihoods import Gaussian
from gpflow.models.model import Model
from gpflow.params.dataholders import Minibatch, DataHolder

from ..layers.latent_variable_layer import LatentVariableLayer, LatentVarMode


class DeepGP(Model):
    """
    Implementation of a Deep Gaussian process, following the specification of:

    @inproceedings{salimbeni2017doubly,
        title={Doubly Stochastic Variational Inference for Deep Gaussian Processes},
        author={Salimbeni, Hugh and Deisenroth, Marc},
        booktitle={NIPS},
        year={2017}
    }
    """
    def __init__(self,
                 X: np.ndarray,
                 Y: np.ndarray,
                 layers: List, *,
                 likelihood: Optional[gpflow.likelihoods.Likelihood] = None,
                 batch_size: Optional[int] = None,
                 name: Optional[str] = None):
        """
        :param X: np.ndarray, N x Dx
        :param Y: np.ndarray, N x Dy
        :param layers: list
            List of `layers.BaseLayer` instances, e.g. PerceptronLayer, ConvLayer, GPLayer, ...
        :param likelihood: gpflow.likelihoods.Likelihood object
            Analytic expressions exists for the Gaussian case.
        :param batch_size: int
        """
        Model.__init__(self, name=name)

        assert X.ndim == 2
        assert Y.ndim == 2

        self.num_data = X.shape[0]
        self.layers = gpflow.ParamList(layers)
        self.likelihood = Gaussian() if likelihood is None else likelihood

        if (batch_size is not None) and (batch_size > 0) and (batch_size < X.shape[0]):
            self.X = Minibatch(X, batch_size=batch_size, seed=0)
            self.Y = Minibatch(Y, batch_size=batch_size, seed=0)
            self.scale = self.num_data / batch_size
        else:
            self.X = DataHolder(X)
            self.Y = DataHolder(Y)
            self.scale = 1.0

    def _get_Ws_iter(self, latent_var_mode: LatentVarMode, Ws=None) -> iter:
        i = 0
        for layer in self.layers:
            if latent_var_mode == LatentVarMode.GIVEN and isinstance(layer, LatentVariableLayer):

                # passing some fixed Ws, which are packed to a single tensor for ease of use with autoflow
                assert isinstance(Ws, tf.Tensor)
                d = layer.latent_dim
                yield Ws[:, i:(i+d)]
                i += d
            else:
                yield None

    @params_as_tensors
    def _build_decoder(self, Z, full_cov=False, full_output_cov=False,
                       Ws=None, latent_var_mode=LatentVarMode.POSTERIOR):
        """
        :param Z: N x W
        """
        Z = tf.cast(Z, dtype=settings.float_type)

        Ws_iter = self._get_Ws_iter(latent_var_mode, Ws)  # iter, returning either None or slices from Ws

        for layer, W in zip(self.layers[:-1], Ws_iter):
            Z = layer.propagate(Z,
                                sampling=True,
                                W=W,
                                latent_var_mode=latent_var_mode,
                                full_output_cov=full_output_cov,
                                full_cov=full_cov)

        return self.layers[-1].propagate(Z,
                                         sampling=False,
                                         W=next(Ws_iter),
                                         latent_var_mode=latent_var_mode,
                                         full_output_cov=full_output_cov,
                                         full_cov=full_cov)  #f_mean, f_var

    @params_as_tensors
    def _build_likelihood(self):
        f_mean, f_var = self._build_decoder(self.X)  # N x P, N x P
        self.E_log_prob = tf.reduce_sum(self.likelihood.variational_expectations(f_mean, f_var, self.Y))

        self.KL_U_layers = reduce(tf.add, (l.KL() for l in self.layers))

        ELBO = self.E_log_prob * self.scale - self.KL_U_layers
        return tf.cast(ELBO, settings.float_type)

    def _predict_f(self, X):
        mean, variance = self._build_decoder(X, latent_var_mode=LatentVarMode.PRIOR)  # N x P, N x P
        return mean, variance

    @params_as_tensors
    @autoflow([settings.float_type, [None, None]])
    def predict_y(self, X):
        mean, var = self._predict_f(X)
        return self.likelihood.predict_mean_and_var(mean, var)

    @autoflow([settings.float_type, [None, None]])
    def predict_f(self, X):
        return self._predict_f(X)

    @autoflow([settings.float_type, [None, None]], [settings.float_type, [None, None]])
    def predict_f_with_Ws(self, X, Ws):
        return self._build_decoder(X, Ws=Ws, latent_var_mode=LatentVarMode.GIVEN)

    @autoflow([settings.float_type, [None, None]], [settings.float_type, [None, None]])
    def predict_f_with_Ws_full_output_cov(self, X, Ws):
        return self._build_decoder(X, Ws=Ws, full_output_cov=True, latent_var_mode=LatentVarMode.GIVEN)

    @autoflow([settings.float_type, [None, None]], [settings.float_type, [None, None]])
    def predict_f_with_Ws_full_cov(self, X, Ws):
        return self._build_decoder(X, Ws=Ws, full_cov=True, latent_var_mode=LatentVarMode.GIVEN)

    @autoflow()
    def compute_KL_U(self):
        return self.KL_U_layers

    @autoflow()
    def compute_data_fit(self):
        return self.E_log_prob * self.scale

    def log_pdf(self, X, Y):
        m, v = self.predict_y(X)
        l = norm.logpdf(Y, loc=m, scale=v**0.5)
        return np.average(l)

    def describe(self):
        """ High-level description of the model """
        desc = self.__class__.__name__
        desc += "\nLayers"
        desc += "\n------\n"
        desc += "\n".join(l.describe() for l in self.layers)
        desc += "\nlikelihood: " + self.likelihood.__class__.__name__
        return desc

