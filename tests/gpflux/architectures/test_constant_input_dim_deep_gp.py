import numpy as np
import pytest
import tensorflow as tf

from gpflux.architectures import Config, build_constant_input_dim_deep_gp
from gpflux.helpers import make_dataclass_from_class

tf.keras.backend.set_floatx("float64")


class DemoConfig:
    num_inducing = 7
    inner_layer_qsqrt_factor = 1e-3
    between_layer_noise_variance = 1e-3
    likelihood_noise_variance = 1e-2
    white = True


@pytest.mark.parametrize("input_dim", [7])
@pytest.mark.parametrize("num_layers", [3])
def test_smoke_build_constant_input_dim_deep_gp(input_dim, num_layers):
    config = make_dataclass_from_class(Config, DemoConfig)
    X = np.random.randn(13, input_dim)
    Y = np.random.randn(13, 1)
    dgp = build_constant_input_dim_deep_gp(X, num_layers, config)
    model_train = dgp.as_training_model()
    model_train.compile("Adam")
    model_train.fit((X, Y), epochs=1)
    model_test = dgp.as_prediction_model()
    _ = model_test(X)
