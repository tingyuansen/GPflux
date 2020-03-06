"""
Vincent Dutordoir
13/07/2018

Experiments running bayesian benchmark regression's
task with deep conditional latent gp models.
"""

import gpflow
import gpflux
import numpy as np

import gpflow.training.monitor as mon

from pprint import pprint
from scipy.cluster.vq import kmeans2

# from: https://github.com/hughsalimbeni/bayesian_benchmarks
from bayesian_benchmarks.tasks.regression import run as run_regression
from bayesian_benchmarks.tasks.regression import argument_parser
from gpflow.training import AdamOptimizer


TENSORBOARD_NAME = "./test/"


def init_inducing_points(X, num):
    if X.shape[0] > num:
        return kmeans2(X, num, minit="points")[0]
    else:
        return np.concatenate([X, np.random.randn(num - X.shape[0], X.shape[1])], 0)


class BayesBench_ConditionalLatentDeepGP:
    """
    We wrap our Deep GP model in a RegressionModel class, to comply with
    bayesian_benchmarks' interface. This means we need to implement:
    - fit
    - predict
    - sample
    """

    def __init__(self, is_test=False, seed=0):
        self.is_test = is_test

    def fit(self, X, Y, Xt=None, Yt=None, name=""):
        self.Xt = Xt
        self.Yt = Yt

        class Config:
            NATGRAD = True
            LATENT_DIM = 1
            X_dim, Y_dim = X.shape[1], Y.shape[1]
            D_in = X_dim + LATENT_DIM
            OUTPUT_DIMS = [D_in, Y.shape[1]]
            ADAM_LR = 0.01
            GAMMA = 0.1
            VAR = 0.01
            FIX_VAR = False
            if self.is_test:
                M = 5
                MAXITER = 500
            else:
                M = 100
                MAXITER = int(10e3)
            MB = 1000 if X.shape[0] > 1000 else None
            TB_NAME = (
                TENSORBOARD_NAME
                + name
                + f"_ldgp_var_{VAR}_{FIX_VAR}_nat_{NATGRAD}_M_{M}"
            )

        print("Configuration")
        pprint(vars(Config))

        # Encder
        encoder = gpflux.GPflowEncoder(
            Config.X_dim + Config.Y_dim, Config.LATENT_DIM, [50, 50]
        )

        # Layer 1
        Z1 = init_inducing_points(X, Config.M)
        Z1 = np.concatenate([Z1, np.random.randn(Z1.shape[0], Config.LATENT_DIM)], 1)
        feat1 = gpflow.features.InducingPoints(Z1)
        kern1 = gpflow.kernels.RBF(
            Config.D_in, lengthscales=Config.D_in ** 0.5, variance=0.1
        )
        mean1 = gpflow.mean_functions.Identity(Config.D_in)
        layer1 = gpflux.layers.GPLayer(
            kern1, feat1, Config.OUTPUT_DIMS[0], mean_function=mean1
        )
        layer1.q_sqrt = 1e-5 * layer1.q_sqrt.read_value()

        # Layer 2
        Z2 = Z1.copy()
        feat2 = gpflow.features.InducingPoints(Z2)
        kern2 = gpflow.kernels.RBF(
            Config.D_in, lengthscales=Config.D_in ** 0.5, variance=0.1
        )
        mean2 = None  # gpflow.mean_functions.Linear(np.random.randn(Config.D_in, Config.Y_dim))
        layer2 = gpflux.layers.GPLayer(
            kern2, feat2, Config.OUTPUT_DIMS[0], mean_function=mean2
        )

        # build model
        self.model = gpflux.ConditionalLatentDeepGP(
            X, Y, encoder, layers=[layer1, layer2], batch_size=Config.MB
        )
        self.model.likelihood.variance = Config.VAR
        self.model.likelihood.set_trainable(not Config.FIX_VAR)

        # self.beta = 1.5

        # minimize
        self._optimize(Config)

    def predict(self, X):
        return self.model.predict_y(X)

    def sample(self, X, num_samples):
        m, v = self.model.predict_y(X)
        return m + np.random.randn(*m.shape) * np.sqrt(v)

    def log_pdf(self, X, Y):
        return self.model.log_pdf(X, Y)

    def avg_log_pdf(self, X, Y):
        return np.average(self.model.log_pdf(X, Y))

    def _create_monitor_tasks(self, file_writer, Config):

        model_tboard_task = (
            mon.ModelToTensorBoardTask(file_writer, self.model)
            .with_name("model_tboard")
            .with_condition(mon.PeriodicIterationCondition(10))
            .with_exit_condition(True)
        )

        print_task = (
            mon.PrintTimingsTask()
            .with_name("print")
            .with_condition(mon.PeriodicIterationCondition(10))
            .with_exit_condition(True)
        )

        hz = 200

        lml_tboard_task = (
            mon.LmlToTensorBoardTask(file_writer, self.model, display_progress=False)
            .with_name("lml_tboard")
            .with_condition(mon.PeriodicIterationCondition(hz))
            .with_exit_condition(True)
        )

        def test_loglik_func(*args, **kwargs):
            return self.avg_log_pdf(self.Xt, self.Yt)

        test_loglik_task = (
            mon.ScalarFuncToTensorBoardTask(file_writer, test_loglik_func, "ttl")
            .with_name("test_loglik")
            .with_condition(mon.PeriodicIterationCondition(hz))
            .with_exit_condition(True)
        )

        def kl_u_func(*args, **kwargs):
            return self.model.compute_KL_U_sum()

        kl_u_task = (
            mon.ScalarFuncToTensorBoardTask(file_writer, kl_u_func, "KL_U")
            .with_name("kl_u")
            .with_condition(mon.PeriodicIterationCondition(hz))
            .with_exit_condition(True)
        )

        def kl_z_func(*args, **kwargs):
            return self.model.compute_KL_Z()

        kl_z_task = (
            mon.ScalarFuncToTensorBoardTask(file_writer, kl_z_func, "KL_Z")
            .with_name("kl_z")
            .with_condition(mon.PeriodicIterationCondition(hz))
            .with_exit_condition(True)
        )

        def data_fit_func(*args, **kwargs):
            return self.model.compute_data_fit()

        data_fit_task = (
            mon.ScalarFuncToTensorBoardTask(file_writer, data_fit_func, "data_fit")
            .with_name("data_fit")
            .with_condition(mon.PeriodicIterationCondition(hz))
            .with_exit_condition(True)
        )

        return [
            print_task,
            model_tboard_task,
            lml_tboard_task,
            test_loglik_task,
            kl_u_task,
            kl_z_task,
            data_fit_task,
        ]

    def _optimize(self, Config):

        session = self.model.enquire_session()
        global_step = mon.create_global_step(session)
        file_writer = mon.LogdirWriter(Config.TB_NAME)
        monitor_tasks = self._create_monitor_tasks(file_writer, Config)

        # Adam
        adam_opt = AdamOptimizer(learning_rate=Config.ADAM_LR)
        adam_step = adam_opt.make_optimize_action(self.model, global_step=global_step)

        natgrad_step = None
        if Config.NATGRAD:
            var_params = [self.model.layers[-1].q_mu, self.model.layers[-1].q_sqrt]
            _ = [param.set_trainable(False) for param in var_params]
            nat_grad_opt = gpflow.train.NatGradOptimizer(Config.GAMMA)
            natgrad_step = nat_grad_opt.make_optimize_action(
                self.model, var_list=[(var_params[0], var_params[1])]
            )

        print("Before optimization:", self.model.compute_log_likelihood())
        with mon.Monitor(
            monitor_tasks, session, global_step, print_summary=True
        ) as monitor:
            for i in range(Config.MAXITER):
                if natgrad_step is not None:
                    natgrad_step()
                adam_step()
                monitor(i)

        print("After optimization:", self.model.compute_log_likelihood())


if __name__ == "__main__":

    cmd_line_arguments = argument_parser()

    run_regression(
        cmd_line_arguments.parse_args(),
        is_test=False,
        write_to_database=False,
        verbose=True,
        Model=BayesBench_ConditionalLatentDeepGP,
    )

    # model = BayesBench_ConditionalLatentDeepGP(is_test=True)
    # func = lambda x: np.sin(x) + np.random.randn(x.shape[0], 1) * 0.001
    # X = np.linspace(0, 1, 20).reshape(-1, 1)
    # Xs = np.random.rand(3).reshape(-1, 1)
    # Y = func(X)
    # Ys = func(Xs)
    # model.fit(X, Y, Xs, Ys)
    # gpflux_model = model.model

    # elbo = gpflux_model.compute_log_likelihood()
    # kl_z = gpflux_model.compute_KL_Z()
    # kl_u = gpflux_model.compute_KL_U()
    # data_fit = gpflux_model.compute_data_fit()

    # print("elbo", elbo)
    # print("kl_z", kl_z)
    # print("kl_u", kl_u)
    # print("data", data_fit)
    # print("sum", data_fit - kl_z - kl_u)
