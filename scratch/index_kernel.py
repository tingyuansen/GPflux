import os
import gpflow
import gpflow.training.monitor as mon
import gpflux
import numpy as np
import tensorflow as tf

from sacred import Experiment
from observations import mnist

from utils import get_error_cb, calc_multiclass_error, calc_binary_error

SUCCESS = 0
NAME = "mnist_new"
ex = Experiment(NAME)


@ex.config
def config():
    dataset = "full"
    # number of inducing points
    M = 500
    # adam learning rate
    adam_lr = 0.01
    # training iterations
    iterations = int(2e3)
    # patch size
    patch_size = [5, 5]
    # path to save results
    basepath = "/mnt/vincent/"
    # minibatch size
    minibatch_size = 100

    base_kern = "RBF"

    # init patches
    init_patches = "patches-unique" # 'patches', 'random'

    restore = False

    # print hz
    hz = 10
    hz_slow = 500


@ex.capture
def data(basepath, dataset, normalize=True):

    path = os.path.join(basepath, "data")
    data_dict = {"01": mnist, "full": mnist}
    data_func = data_dict[dataset]

    (X, Y), (Xs, Ys) = data_func(path)
    Y = Y.astype(int)
    Ys = Ys.astype(int)
    Y = Y.reshape(-1, 1)
    Ys = Ys.reshape(-1, 1)
    alpha = 255. if normalize else 1.

    if dataset == "01":
        def filter_01(X, Y):
            lbls01 = np.logical_or(Y == 0, Y == 1).flatten()
            return X[lbls01, :], Y[lbls01, :]

        X, Y = filter_01(X, Y)
        Xs, Ys = filter_01(Xs, Ys)
        return X/alpha, Y, Xs/alpha, Ys
    else:
        return X/alpha, Y, Xs/alpha, Ys



@ex.capture
def experiment_name(adam_lr, M, minibatch_size, dataset,
                    base_kern, init_patches, patch_size):
    args = np.array(
        [
            dataset,
            "init_patches", init_patches,
            "kern", base_kern,
            "adam", adam_lr,
            "M", M,
            "minibatch_size", minibatch_size,
            "patch", patch_size[0],
        ])
    return "_".join(args.astype(str))


@ex.capture
def restore_session(session, restore, basepath):
    model_path = os.path.join(basepath, NAME, experiment_name())
    if restore and os.path.isdir(model_path):
        mon.restore_session(session, model_path)
        print("Model restored")


@gpflow.defer_build()
@ex.capture
def setup_model(X, Y, minibatch_size, patch_size, M, dataset, base_kern,
                init_patches, basepath, restore):


    if dataset == "01":
        like = gpflow.likelihoods.Bernoulli()
        num_filters = 1
    else:
        like = gpflow.likelihoods.SoftMax(10)
        num_filters = 10

    H = int(X.shape[1]**.5)

    if init_patches == "random":
        patches = gpflux.init.NormalInitializer()
    else:
        unique = init_patches == "patches-unique"
        patches = gpflux.init.PatchSamplerInitializer(X[:100],
                                                      width=H, height=H,
                                                      unique=unique)

    layer0 = gpflux.layers.PoolingIndexedConvLayer(
                            [H, H], M, patch_size,
                            num_filters=num_filters,
                            patches_initializer=patches)

    # init kernel
    layer0.kern.index_kernel.variance = 25.0
    layer0.kern.conv_kernel.basekern.variance = 25.0
    layer0.kern.conv_kernel.basekern.lengthscales = 1.2
    layer0.kern.index_kernel.lengthscales = 3.0

    # break symmetry in variational parameters
    layer0.q_sqrt = layer0.q_sqrt.read_value()
    layer0.q_mu = np.random.randn(*(layer0.q_mu.read_value().shape))

    model = gpflux.DeepGP(X, Y,
                          layers=[layer0],
                          likelihood=like,
                          batch_size=minibatch_size,
                          name="my_deep_gp")
    return model

@ex.capture
def setup_monitor_tasks(Xs, Ys, model, optimizer,
                        hz, hz_slow, basepath, dataset, adam_lr):

    tb_path = os.path.join(basepath, NAME, "tensorboards", experiment_name())
    model_path = os.path.join(basepath, NAME, experiment_name())
    fw = mon.LogdirWriter(tb_path)

    # print_error = mon.CallbackTask(error_cb)\
        # .with_name('error')\
        # .with_condition(mon.PeriodicIterationCondition(hz))\
        # .with_exit_condition(True)
    tasks = []

    if adam_lr == "decay":
        def lr(*args, **kwargs):
            sess = model.enquire_session()
            return sess.run(optimizer._optimizer._lr)

        tasks += [\
              mon.ScalarFuncToTensorBoardTask(fw, lr, "lr")\
              .with_name('lr')\
              .with_condition(mon.PeriodicIterationCondition(hz))\
              .with_exit_condition(True)\
              .with_flush_immediately(True)]

    tasks += [\
        mon.CheckpointTask(model_path)\
        .with_name('saver')\
        .with_condition(mon.PeriodicIterationCondition(hz))]

    tasks += [\
        mon.ModelToTensorBoardTask(fw, model)\
        .with_name('model_tboard')\
        .with_condition(mon.PeriodicIterationCondition(hz))\
        .with_exit_condition(True)\
        .with_flush_immediately(True)]

    tasks += [\
        mon.PrintTimingsTask().with_name('print')\
        .with_condition(mon.PeriodicIterationCondition(hz))\
        .with_exit_condition(True)]

    error_func = calc_binary_error if dataset == "01" \
                    else calc_multiclass_error

    f1 = get_error_cb(model, Xs, Ys, error_func)
    tasks += [\
          mon.ScalarFuncToTensorBoardTask(fw, f1, "error")\
          .with_name('error')\
          .with_condition(mon.PeriodicIterationCondition(hz))\
          .with_exit_condition(True)\
          .with_flush_immediately(True)]

    f2 = get_error_cb(model, Xs, Ys, error_func, full=True)
    tasks += [\
          mon.ScalarFuncToTensorBoardTask(fw, f2, "error_full")\
          .with_name('error_full')\
          .with_condition(mon.PeriodicIterationCondition(hz_slow))\
          .with_exit_condition(True)\
          .with_flush_immediately(True)]

    print("# tasks:", len(tasks))
    return tasks


@ex.capture
def setup_optimizer(model, global_step, adam_lr):

    if adam_lr == "decay":
        print("decaying lr")
        lr = tf.train.exponential_decay(learning_rate=0.01,
                                        global_step=global_step,
                                        decay_steps=500,
                                        decay_rate=.785,
                                        staircase=True)
    else:
        lr = adam_lr

    return gpflow.train.AdamOptimizer(lr)

@ex.capture
def run(model, session, global_step, monitor_tasks, optimizer, iterations):

    monitor = mon.Monitor(monitor_tasks, session, global_step, print_summary=True)

    with monitor:
        optimizer.minimize(model,
                           step_callback=monitor,
                           maxiter=iterations,
                           global_step=global_step)
    return model.compute_log_likelihood()


def _save(model, filename):
    gpflow.Saver().save(filename, model)
    print("model saved")


@ex.capture
def finish(X, Y, Xs, Ys, model, dataset, basepath):
    print(model)
    error_func = calc_binary_error if dataset == "01" \
                    else calc_multiclass_error
    error_func = get_error_cb(model, Xs, Ys, error_func, full=True)
    print("error test", error_func())
    print("error train", error_func())

    fn = os.path.join(basepath, NAME) + "/" + experiment_name() + ".gpflow"
    _save(model, fn)


@ex.capture
def trace_run(model, sess, M, minibatch_size, adam_lr):

    name =  f"M_{M}_N_{minibatch_size}_pyfunc"
    from utils import trace

    with sess:
        like = model.likelihood_tensor
        trace(like, sess, "trace_likelihood_{}.json".format(name))

        adam_opt = gpflow.train.AdamOptimizer(learning_rate=0.01)
        adam_step = adam_opt.make_optimize_tensor(model, session=sess)
        trace(adam_step, sess, "trace_adam_{}.json".format(name))

@ex.automain
def main():
    X, Y, Xs, Ys = data()

    model = setup_model(X, Y)
    model.compile()
    sess = model.enquire_session()
    step = mon.create_global_step(sess)

    trace_run(model, sess)
    return 0

    restore_session(sess)

    print(model)
    print("X", np.min(X), np.max(X))
    print("before optimisation ll", model.compute_log_likelihood())

    optimizer = setup_optimizer(model, step)
    monitor_tasks = setup_monitor_tasks(Xs, Ys, model, optimizer)
    ll = run(model, sess, step,  monitor_tasks, optimizer)
    print("after optimisation ll", ll)

    finish(X, Y, Xs, Ys, model)

    return SUCCESS
