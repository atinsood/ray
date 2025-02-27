import io
import logging
import os
import time
from contextlib import redirect_stderr
from unittest.mock import patch

import pytest

import ray
from ray import tune
from ray.data.preprocessor import Preprocessor
from ray.train import base_trainer
from ray.train.data_parallel_trainer import DataParallelTrainer
from ray.train.gbdt_trainer import GBDTTrainer
from ray.train.trainer import BaseTrainer
from ray.air.config import ScalingConfig
from ray.util.placement_group import get_current_placement_group

logger = logging.getLogger(__name__)


@pytest.fixture
def ray_start_4_cpus():
    address_info = ray.init(num_cpus=4)
    yield address_info
    # The code after the yield will run as teardown code.
    ray.shutdown()


class DummyPreprocessor(Preprocessor):
    def __init__(self):
        self.fit_counter = 0

    def fit(self, ds):
        self.fit_counter += 1

    def transform(self, ds):
        return ds.map(lambda x: x + 1)


class DummyTrainer(BaseTrainer):
    _scaling_config_allowed_keys = BaseTrainer._scaling_config_allowed_keys + [
        "num_workers",
        "use_gpu",
        "resources_per_worker",
        "placement_strategy",
    ]

    def __init__(self, train_loop, custom_arg=None, **kwargs):
        self.custom_arg = custom_arg
        self.train_loop = train_loop
        super().__init__(**kwargs)

    def training_loop(self) -> None:
        self.train_loop(self)


class DummyGBDTTrainer(GBDTTrainer):
    _dmatrix_cls: type = None
    _ray_params_cls: type = None
    _tune_callback_report_cls: type = None
    _tune_callback_checkpoint_cls: type = None
    _init_model_arg_name: str = None


def test_trainer_fit(ray_start_4_cpus):
    def training_loop(self):
        tune.report(my_metric=1)

    trainer = DummyTrainer(train_loop=training_loop)
    result = trainer.fit()
    assert result.metrics["my_metric"] == 1


def test_preprocess_datasets(ray_start_4_cpus):
    def training_loop(self):
        assert self.datasets["my_dataset"].take() == [2, 3, 4]

    datasets = {"my_dataset": ray.data.from_items([1, 2, 3])}
    trainer = DummyTrainer(
        training_loop, datasets=datasets, preprocessor=DummyPreprocessor()
    )
    trainer.fit()


def test_resources(ray_start_4_cpus):
    def check_cpus(self):
        assert ray.available_resources()["CPU"] == 2

    assert ray.available_resources()["CPU"] == 4
    trainer = DummyTrainer(
        check_cpus, scaling_config=ScalingConfig(trainer_resources={"CPU": 2})
    )
    trainer.fit()


@pytest.mark.parametrize("gen_dataset", [True, False])
def test_preprocess_fit_on_train(ray_start_4_cpus, gen_dataset):
    def training_loop(self):
        # Fit was only called once.
        assert self.preprocessor.fit_counter == 1
        # Datasets should all be transformed.
        assert self.datasets["train"].take() == [2, 3, 4]
        assert self.datasets["my_dataset"].take() == [2, 3, 4]

    if gen_dataset:
        datasets = {
            "train": lambda: ray.data.from_items([1, 2, 3]),
            "my_dataset": lambda: ray.data.from_items([1, 2, 3]),
        }
    else:
        datasets = {
            "train": ray.data.from_items([1, 2, 3]),
            "my_dataset": ray.data.from_items([1, 2, 3]),
        }
    trainer = DummyTrainer(
        training_loop, datasets=datasets, preprocessor=DummyPreprocessor()
    )
    trainer.fit()


def test_preprocessor_already_fitted(ray_start_4_cpus):
    def training_loop(self):
        # Make sure fit is not called if preprocessor is already fit.
        assert self.preprocessor.fit_counter == 1
        # Datasets should all be transformed.
        assert self.datasets["train"].take() == [2, 3, 4]
        assert self.datasets["my_dataset"].take() == [2, 3, 4]

    datasets = {
        "train": ray.data.from_items([1, 2, 3]),
        "my_dataset": ray.data.from_items([1, 2, 3]),
    }
    preprocessor = DummyPreprocessor()
    preprocessor.fit(ray.data.from_items([1]))
    trainer = DummyTrainer(
        training_loop, datasets=datasets, preprocessor=DummyPreprocessor()
    )
    trainer.fit()


def test_arg_override(ray_start_4_cpus):
    def check_override(self):
        assert self.scaling_config.num_workers == 1
        # Should do deep update.
        assert not self.custom_arg["outer"]["inner"]
        assert self.custom_arg["outer"]["fixed"] == 1
        # Should merge with base config.
        assert self.preprocessor.original

        pg = get_current_placement_group()
        assert len(pg.bundle_specs) == 2  # 1 trainer, 1 worker

    preprocessor = DummyPreprocessor()
    preprocessor.original = True
    scale_config = ScalingConfig(num_workers=4)
    trainer = DummyTrainer(
        check_override,
        custom_arg={"outer": {"inner": True, "fixed": 1}},
        preprocessor=preprocessor,
        scaling_config=scale_config,
    )

    new_config = {
        "custom_arg": {"outer": {"inner": False}},
        "scaling_config": ScalingConfig(num_workers=1),
    }

    tune.run(trainer.as_trainable(), config=new_config)


def test_reserved_cpus(ray_start_4_cpus):
    def train_loop(self):
        ray.data.range(10).show()

    # Will deadlock without reserved CPU fraction.
    scale_config = ScalingConfig(num_workers=1, _max_cpu_fraction_per_node=0.9)
    trainer = DummyTrainer(
        train_loop,
        scaling_config=scale_config,
    )
    tune.run(trainer.as_trainable(), num_samples=4)


# TODO(ekl/sang) this currently fails.
#    # Check we don't deadlock with too low of a fraction either.
#    scale_config = ScalingConfig(num_workers=1, _max_cpu_fraction_per_node=0.01)
#    trainer = DummyTrainer(
#        train_loop,
#        scaling_config=scale_config,
#    )
#    tune.run(trainer.as_trainable(), num_samples=4)


def test_reserved_cpu_warnings(ray_start_4_cpus):
    def train_loop(self):
        pass

    class MockLogger:
        def __init__(self):
            self.warnings = []

        def warning(self, msg):
            self.warnings.append(msg)

        def info(self, msg):
            print(msg)

    try:
        old = base_trainer.logger
        base_trainer.logger = MockLogger()

        # Fraction correctly specified.
        DummyTrainer(
            train_loop,
            scaling_config=ScalingConfig(num_workers=1, _max_cpu_fraction_per_node=0.9),
            datasets={"train": ray.data.range(10)},
        )
        assert not base_trainer.logger.warnings

        # No datasets, no fraction.
        DummyTrainer(
            train_loop,
            scaling_config=ScalingConfig(num_workers=1),
        )
        assert not base_trainer.logger.warnings

        # Should warn.
        DummyTrainer(
            train_loop,
            scaling_config=ScalingConfig(num_workers=1),
            datasets={"train": ray.data.range(10)},
        )
        assert len(base_trainer.logger.warnings) == 1, base_trainer.logger.warnings
        assert "_max_cpu_fraction_per_node" in base_trainer.logger.warnings[0]
    finally:
        base_trainer.logger = old


def test_setup(ray_start_4_cpus):
    def check_setup(self):
        assert self._has_setup

    class DummyTrainerWithSetup(DummyTrainer):
        def setup(self):
            self._has_setup = True

    trainer = DummyTrainerWithSetup(check_setup)
    trainer.fit()


def test_fail(ray_start_4_cpus):
    def fail(self):
        raise ValueError

    trainer = DummyTrainer(fail)
    with pytest.raises(ValueError):
        trainer.fit()


@patch.dict(os.environ, {"RAY_LOG_TO_STDERR": "1"})
def _is_trainable_name_overriden(trainer: BaseTrainer):
    trainable = trainer.as_trainable()
    output = io.StringIO()

    def say(self):
        logger.warning("say")

    trainable.say = say
    with redirect_stderr(output):
        remote_trainable = ray.remote(trainable)
        remote_actor = remote_trainable.remote()
        ray.get(remote_actor.say.remote())
        time.sleep(1)  # make sure logging gets caught
    output = output.getvalue()
    print(output)
    assert trainable().__repr__() in output


def test_trainable_name_is_overriden_data_parallel_trainer(ray_start_4_cpus):
    trainer = DataParallelTrainer(
        lambda x: x, scaling_config=ScalingConfig(num_workers=1)
    )

    _is_trainable_name_overriden(trainer)


def test_trainable_name_is_overriden_gbdt_trainer(ray_start_4_cpus):
    trainer = DummyGBDTTrainer(
        params={},
        label_column="__values__",
        datasets={"train": ray.data.from_items([1, 2, 3])},
        scaling_config=ScalingConfig(num_workers=1),
    )

    _is_trainable_name_overriden(trainer)


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main(["-v", "-x", __file__]))
