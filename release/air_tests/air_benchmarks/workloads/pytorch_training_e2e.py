import click
import time
import json
import os
import pandas as pd

from torchvision import transforms
from torchvision.models import resnet18
import torch
import torch.nn as nn
import torch.optim as optim

import ray
from ray.air.util.tensor_extensions.pandas import TensorArray
from ray.train.torch import TorchCheckpoint
from ray.data.preprocessors import BatchMapper
from ray import train
from ray.air import session
from ray.train.torch import TorchTrainer
from ray.data.datasource import ImageFolderDatasource
from ray.air.config import ScalingConfig


def preprocess_image_with_label(df: pd.DataFrame) -> pd.DataFrame:
    """
    User Pytorch code to transform user image. Note we still use TensorArray as
    intermediate format to hold images for now.
    """
    preprocess = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )
    df["image"] = TensorArray([preprocess(image.to_numpy()) for image in df["image"]])
    # Fix fixed synthetic value for perf benchmark purpose
    df["label"] = df["label"].map(lambda _: 1)
    return df


def train_loop_per_worker(config):
    raw_model = resnet18(pretrained=True)
    model = train.torch.prepare_model(raw_model)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(model.parameters(), lr=0.001, momentum=0.9)

    train_dataset_shard = session.get_dataset_shard("train")

    for epoch in range(config["num_epochs"]):
        running_loss = 0.0
        for i, data in enumerate(
            train_dataset_shard.iter_batches(
                batch_size=config["batch_size"], batch_format="numpy"
            )
        ):
            # get the inputs; data is a list of [inputs, labels]
            inputs = torch.as_tensor(data["image"], dtype=torch.float32).to(
                device="cuda"
            )
            labels = torch.as_tensor(data["label"], dtype=torch.int64).to(device="cuda")
            # zero the parameter gradients
            optimizer.zero_grad()

            # forward + backward + optimize
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()

            # print statistics
            running_loss += loss.item()
            if i % 2000 == 1999:  # print every 2000 mini-batches
                print(f"[{epoch + 1}, {i + 1:5d}] loss: {running_loss / 2000:.3f}")
                running_loss = 0.0

        session.report(
            dict(running_loss=running_loss),
            checkpoint=TorchCheckpoint.from_model(model),
        )


@click.command(help="Run Batch prediction on Pytorch ResNet models.")
@click.option("--data-size-gb", type=int, default=1)
@click.option("--num-epochs", type=int, default=2)
@click.option("--num-workers", type=int, default=1)
def main(data_size_gb: int, num_epochs=2, num_workers=1):
    data_url = f"s3://air-example-data-2/{data_size_gb}G-image-data-synthetic-raw"
    print(
        "Running Pytorch image model training with "
        f"{data_size_gb}GB data from {data_url}"
    )
    print(f"Training for {num_epochs} epochs with {num_workers} workers.")
    start = time.time()
    # Enable cross host NCCL for larger scale tests
    runtime_env = {"env_vars": {"NCCL_SOCKET_IFNAME": "ens3"}}
    ray.init(runtime_env=runtime_env)
    dataset = ray.data.read_datasource(ImageFolderDatasource(), paths=[data_url])

    preprocessor = BatchMapper(preprocess_image_with_label)

    trainer = TorchTrainer(
        train_loop_per_worker=train_loop_per_worker,
        train_loop_config={"batch_size": 64, "num_epochs": num_epochs},
        datasets={"train": dataset},
        preprocessor=preprocessor,
        scaling_config=ScalingConfig(num_workers=num_workers, use_gpu=True),
    )
    trainer.fit()

    total_time_s = round(time.time() - start, 2)

    # For structured output integration with internal tooling
    results = {"data_size_gb": data_size_gb, "num_epochs": num_epochs}
    results["perf_metrics"] = [
        {
            "perf_metric_name": "total_time_s",
            "perf_metric_value": total_time_s,
            "perf_metric_type": "LATENCY",
        },
        {
            "perf_metric_name": "throughout_MB_s",
            "perf_metric_value": round(
                num_epochs * data_size_gb * 1024 / total_time_s, 2
            ),
            "perf_metric_type": "THROUGHPUT",
        },
    ]

    test_output_json = os.environ.get("TEST_OUTPUT_JSON", "/tmp/release_test_out.json")
    with open(test_output_json, "wt") as f:
        json.dump(results, f)

    print(results)


if __name__ == "__main__":
    main()
