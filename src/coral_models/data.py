"""Functions related to the data loading and processing"""

import os

from datasets import DatasetDict, load_dataset
from omegaconf import DictConfig


def load_data(cfg: DictConfig) -> DatasetDict:
    """Load an audio dataset.

    Args:
        cfg (DictConfig):
            The Hydra configuration object.

    Returns:
        DatasetDict:
            The audio dataset.

    Raises:
        ValueError:
            If the dataset is not supported.
    """
    # Load dataset from the Hugging Face Hub. The HUGGINGFACE_HUB_TOKEN is only used
    # during CI - normally it is expected that the user is logged in to the Hugging
    # Face Hub using the `huggingface-cli login` command.
    dataset = load_dataset(
        path=cfg.dataset.id,
        name=cfg.dataset.subset,
        use_auth_token=os.getenv("HUGGINGFACE_HUB_TOKEN"),
    )
    assert isinstance(dataset, DatasetDict)

    # Only include the train, validation and test splits of the dataset, and rename
    # these splits to the default split names.
    return DatasetDict(
        dict(
            train=dataset[cfg.dataset.train_name],
            val=dataset[cfg.dataset.val_name],
            test=dataset[cfg.dataset.test_name],
        )
    )