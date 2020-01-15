# Copyright (C) PROWLER.io 2020 - All Rights Reserved
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential

"""
Experiments running bayesian_benchmarks's regression task with Deep GPs.
"""

import tqdm
import gpflow
import tensorflow as tf
from gpflow.mean_functions import Zero
from gpflow.kernels import SquaredExponential
from gpflow.likelihoods import Gaussian
from gpflow.utilities import set_trainable, print_summary
from gpflux2.models import DeepGP
from gpflux2.layers import GPLayer, LikelihoodLayer
from gpflux2.helpers import construct_basic_kernel, construct_basic_inducing_variables

import numpy as np

from pprint import pprint
from scipy.cluster.vq import kmeans2


# TENSORBOARD_NAME = "/mnt/vincent/uci2/bayesbench_tensorboards/"
# TENSORBOARD_NAME = "test/"

def init_inducing_points(X, num):
    if X.shape[0] > num:
        return kmeans2(X, num, minit='points')[0]
    else:
        return np.concatenate([X, np.random.randn(num - X.shape[0], X.shape[1])], 0)


def build_deep_gp(input_dim, num_inducing):
    layer_dims = [input_dim, input_dim, 1]

    def kernel_factory(dim, variance=1.0):
        return SquaredExponential(lengthscale=float(dim)**0.5)

    # Pass in kernels, specificy output dim (shared hyperparams/variables)
    l1_kernel = construct_basic_kernel(
        kernels=kernel_factory(layer_dims[0]),
        output_dim=layer_dims[1],
        share_hyperparams=True,
    )
    l1_inducing = construct_basic_inducing_variables(
        num_inducing=num_inducing,
        input_dim=layer_dims[0],
        share_variables=True,
    )

    l2_kernel = construct_basic_kernel(
        kernels=kernel_factory(layer_dims[1]),
        output_dim=layer_dims[2],
        share_hyperparams=True,
    )
    l2_inducing = construct_basic_inducing_variables(
        num_inducing=num_inducing,
        input_dim=layer_dims[1],
        share_variables=True,
    )

    # Assemble at the end
    gp_layers = [
        GPLayer(l1_kernel, l1_inducing),
        GPLayer(l2_kernel, l2_inducing, mean_function=Zero(), use_samples=False),
    ]
    return DeepGP(gp_layers, likelihood_layer=LikelihoodLayer(Gaussian()))


tf.keras.backend.set_floatx("float64")


class BayesBench_DeepGP:
    """
    We wrap our Deep GP model in a RegressionModel class, to comply with
    bayesian_benchmarks' interface. This means we need to implement:
    - fit
    - predict
    - sample
    """
    class Config:
        NATGRAD = True
        # NATGRAD = False
        ADAM_LR = 0.01
        GAMMA = 0.1
        VAR = 0.01
        FIX_VAR = False
        M = 100
        MAXITER = int(10e3)
        MINIBATCH = 1000
        #TB_NAME = TENSORBOARD_NAME +\
        #          name +\
        #          "_dgp_var_{}_{}_nat_{}_M_{}".\
        #            format(VAR, FIX_VAR, NATGRAD, M)

    def __init__(self, is_test=False, seed=0):
        self.is_test = is_test
        if self.is_test:
            self.Config.M = 5
            self.Config.MAXITER = 500

    def fit(self, X, Y, Xt=None, Yt=None, name=None, Config=None):
        if Config is None:
            Config = self.Config

        num_data = X.shape[0]
        assert Y.shape[0] == num_data
        X_dim, Y_dim = X.shape[1], Y.shape[1]
        assert Y_dim == 1

        if num_data <= Config.MINIBATCH:
            Config.MINIBATCH = None

        self.Xt = Xt
        self.Yt = Yt

        print("Configuration")
        pprint(vars(Config))

        # build model
        model = build_deep_gp(X_dim, Config.M)

        Z = init_inducing_points(X, Config.M)
        model.gp_layers[0].inducing_variable.inducing_variable_shared.Z.assign(Z.copy())
        model.gp_layers[0].kernel.kernel.variance.assign(0.1)
        model.gp_layers[0].q_sqrt.assign(1e-5 * model.gp_layers[0].q_sqrt.read_value())
        model.gp_layers[1].inducing_variable.inducing_variable_shared.Z.assign(Z.copy())
        model.likelihood_layer.likelihood.variance.assign(Config.VAR)
        set_trainable(model.likelihood_layer.likelihood, not Config.FIX_VAR)

        print_summary(model)

        self.model = model
        # self.beta = 1.5

        # minimize
        self._optimize(X, Y, Config)

    def predict(self, X):
        # The last GP layer has use_samples=False, hence the DeepGP model will
        # pass a tuple to the likelihood layer, which will in turn call
        # predict_mean_and_var and return mean and variance.
        # TODO: it seems like this is too much magic under the hood
        return self.model(X)

    def sample(self, X, num_samples):
        m, v = self.predict(X)
        return m + np.random.randn(*m.shape) * np.sqrt(v)

    def log_pdf(self, Xt, Yt):
        return self.model.log_pdf(Xt, Yt)

    """
    def _create_monitor_tasks(self, file_writer, Config):

        model_tboard_task = mon.ModelToTensorBoardTask(file_writer, self.model)\
            .with_name('model_tboard')\
            .with_condition(mon.PeriodicIterationCondition(10))\
            .with_exit_condition(True)

        print_task = mon.PrintTimingsTask().with_name('print')\
            .with_condition(mon.PeriodicIterationCondition(10))\
            .with_exit_condition(True)

        hz = 200

        lml_tboard_task = mon.LmlToTensorBoardTask(file_writer, self.model,
                                                   display_progress=False)\
            .with_name('lml_tboard')\
            .with_condition(mon.PeriodicIterationCondition(hz))\
            .with_exit_condition(True)

        test_loglik_func = lambda *args, **kwargs: self.model.log_pdf(self.Xt, self.Yt)
        test_loglik_task = mon.ScalarFuncToTensorBoardTask(file_writer, test_loglik_func, "ttl")\
              .with_name('test_loglik')\
              .with_condition(mon.PeriodicIterationCondition(hz))\
              .with_exit_condition(True)

        kl_u_func = lambda *args, **kwargs: self.model.compute_KL_U_sum()
        kl_u_task = mon.ScalarFuncToTensorBoardTask(file_writer, kl_u_func, "KL_U")\
              .with_name('kl_u')\
              .with_condition(mon.PeriodicIterationCondition(hz))\
              .with_exit_condition(True)

        data_fit_func = lambda *args, **kwargs: self.model.compute_data_fit()
        data_fit_task = mon.ScalarFuncToTensorBoardTask(file_writer, data_fit_func, "data_fit")\
              .with_name('data_fit')\
              .with_condition(mon.PeriodicIterationCondition(hz))\
              .with_exit_condition(True)

        return [print_task, model_tboard_task, lml_tboard_task,
                  test_loglik_task, kl_u_task, data_fit_task]
    """

    def _optimize(self, X, Y, Config):

        num_data = X.shape[0]
        adam_opt = tf.optimizers.Adam(learning_rate=Config.ADAM_LR)

        data = (X, Y)

        @tf.function(autograph=False)
        def model_objective(batch):
            return - self.model.elbo(batch)

        if Config.MINIBATCH is not None:
            batch_size = np.minimum(Config.MINIBATCH, num_data)
            data_minibatch = tf.data.Dataset.from_tensor_slices(data) \
                .prefetch(num_data).repeat().shuffle(num_data) \
                .batch(batch_size)
            data_minibatch_it = iter(data_minibatch)

            def objective_closure() -> tf.Tensor:
                batch = next(data_minibatch_it)
                return model_objective(batch)

        else:
            def objective_closure() -> tf.Tensor:
                return model_objective(data)

        natgrad_step = None
        if Config.NATGRAD:
            var_params = [(self.model.gp_layers[-1].q_mu, self.model.gp_layers[-1].q_sqrt)]
            # stop Adam from optimizing variational parameters
            for param_list in var_params:
                for param in param_list:
                    set_trainable(param, False)

            natgrad_opt = gpflow.optimizers.NaturalGradient(gamma=Config.GAMMA)

            @tf.function
            def natgrad_step():
                natgrad_opt.minimize(objective_closure, var_params)

        @tf.function
        def adam_step():
            adam_opt.minimize(objective_closure, self.model.trainable_weights)

        print("Before optimization:", self.model.elbo(data))
        tq = tqdm.tqdm(range(Config.MAXITER))
        for i in tq:
            if natgrad_step is not None:
                natgrad_step()
            adam_step()
            if i % 60 == 0:
                tq.set_postfix_str(f"objective: {objective_closure()}")

        print("After optimization:", self.model.elbo(data))


if __name__ == "__main__":
    # from: https://github.com/Prowler-io/bayesian_benchmarks
    from bayesian_benchmarks.tasks.regression import run as run_regression
    from bayesian_benchmarks.tasks.regression import parse_args

    run_regression(parse_args(),
                   is_test=True,
                   model=BayesBench_DeepGP(is_test=False))
