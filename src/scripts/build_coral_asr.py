"""Script that builds and uploads the CoRal speech recognition dataset from the raw data.

Usage:
    python src/scripts/build_coral_asr.py \
        [--audio-dir directory/containing/the/audio/subdirectories] \
        [--metadata-database-path path/to/the/sqlite/database] \
        [--hub-id organisation/dataset-id]
"""

import logging
import shutil
import sqlite3
import tarfile
from pathlib import Path
from time import sleep

import click
from datasets import (
    Audio,
    Dataset,
    DatasetDict,
    disable_progress_bar,
    enable_progress_bar,
)
from joblib import Parallel, delayed
from tqdm.auto import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s ⋅ %(name)s ⋅ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("build_coral_asr")


VALIDATION_SET_SPEAKER_IDS: list[str] = list()


TEST_SET_SPEAKER_IDS: list[str] = list()


@click.command()
@click.option(
    "--audio-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, readable=True),
    default="/Volumes/CoRal/_new_structure/raw",
    show_default=True,
    help="Path to the directory containing the raw audio files.",
)
@click.option(
    "--metadata-database-path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True),
    default="/Volumes/CoRal/_new_structure/raw/CoRal_public.db",
    show_default=True,
    help="Path to the SQLite database containing the metadata.",
)
@click.option(
    "--hub-id",
    type=str,
    default="alexandrainst/coral",
    show_default=True,
    help="Identifier of the Hugging Face Hub repository.",
)
def main(
    audio_dir: Path | str, metadata_database_path: Path | str, hub_id: str
) -> None:
    """Build and upload the CoRal speech recognition dataset."""
    metadata_database_path = Path(metadata_database_path)
    read_aloud_dir = Path(audio_dir) / "recordings"
    conversation_dir = Path(audio_dir) / "conversations"

    logger.info("Copying the raw files to the current working directory...")
    temp_read_aloud_dir = copy_audio_directory_to_cwd(audio_dir=read_aloud_dir)
    temp_conversation_dir = copy_audio_directory_to_cwd(audio_dir=conversation_dir)
    temp_metadata_database_path = Path.cwd() / metadata_database_path.name
    shutil.copy(src=metadata_database_path, dst=temp_metadata_database_path)

    logger.info("Building the CoRal read-aloud speech recognition dataset...")
    read_aloud_dataset = build_read_aloud_dataset(
        metadata_database_path=temp_metadata_database_path,
        audio_dir=temp_read_aloud_dir,
    )

    logger.info("Building the CoRal conversation speech recognition dataset...")
    conversation_dataset = build_conversation_dataset(
        metadata_database_path=temp_metadata_database_path,
        audio_dir=temp_conversation_dir,
    )

    logger.info("Deleting the temporary raw files in the current working directory...")
    temp_metadata_database_path.unlink()
    shutil.rmtree(path=temp_read_aloud_dir)
    shutil.rmtree(path=temp_conversation_dir)

    logger.info("Splitting the datasets into train, validation and test sets...")
    read_aloud_dataset = split_dataset(dataset=read_aloud_dataset)
    conversation_dataset = split_dataset(dataset=conversation_dataset)

    logger.info(f"Uploading the datasets to {hub_id!r} on the Hugging Face Hub...")
    upload_dataset(
        read_aloud_dataset=read_aloud_dataset,
        conversation_dataset=conversation_dataset,
        hub_id=hub_id,
    )

    logger.info(f"All done! See the datasets at https://hf.co/datasets/{hub_id}.")


##########################################
##### Building the read-aloud subset #####
##########################################


def build_read_aloud_dataset(metadata_database_path: Path, audio_dir: Path) -> Dataset:
    """Build the CoRal read-aloud dataset.

    Args:
        metadata_database_path:
            Path to the SQLite database containing the metadata.
        audio_dir:
            Path to the directory containing the audio files.

    Returns:
        The CoRal read-aloud dataset.
    """
    # Get the number of samples in the SQLite database. We don't do any merges here to
    # save some time. That means that the count will be an upper bound rather than a
    # precise number of samples, but we deal with that when we actually fetch the data
    count_query = "SELECT COUNT(*) FROM Recordings;"
    with sqlite3.connect(database=metadata_database_path) as connection:
        cursor = connection.cursor()
        cursor.execute(count_query)
        num_metadata_samples = cursor.fetchone()[0]
    logger.info(f"There are {num_metadata_samples:,} samples in the SQLite database.")

    # Set up which features to fetch from the SQLite database. We exclude the ID
    # features since they need to be handled separately
    non_id_features = [
        "datetime_start",
        "datetime_end",
        "text",
        "location",
        "location_roomdim",
        "noise_level",
        "noise_type",
        "source_url",
        "age",
        "gender",
        "dialect",
        "language_native",
        "language_spoken",
        "country_birth",
        "zipcode_birth",
        "zip_school",
        "education",
        "occupation",
        "validated",
    ]
    non_id_features_str = ",\n".join(non_id_features)

    selection_query = f"""
        SELECT
            Recordings.id_recording,
            Sentences.id_sentence,
            Speakers.id_speaker,
            Recordings.id_validator,
            {non_id_features_str}
        FROM
            Recordings
            INNER JOIN Sentences ON Recordings.id_sentence = Sentences.id_sentence
            INNER JOIN Speakers ON Recordings.id_speaker = Speakers.id_speaker
    """

    # Open the database connection and fetch the data
    logger.info("Fetching the metadata from the SQLite database...")
    with sqlite3.connect(database=metadata_database_path) as connection:
        cursor = connection.cursor()
        cursor.execute(selection_query)
        rows = list(map(list, cursor.fetchall()))

    # Get a list of all the audio file paths. We need this since the audio files lie in
    # subdirectories of the main audio directory
    audio_subdirs = list(audio_dir.iterdir())
    with Parallel(n_jobs=-1, backend="threading") as parallel:
        all_audio_path_lists = parallel(
            delayed(list_audio_files)(subdir)
            for subdir in tqdm(audio_subdirs, desc="Collecting audio file paths")
        )
    all_audio_paths = {
        path.stem: path
        for path_list in all_audio_path_lists
        for path in path_list or []
    }

    # Match the audio files to the metadata, to ensure that there is a 1-to-1
    # correspondence between them
    logger.info("Matching the audio files to the metadata...")
    recording_ids: list[str] = [row[0] for row in rows]
    matched_audio_paths = [
        all_audio_paths.get(recording_id) for recording_id in recording_ids
    ]
    rows = [
        row + [str(audio_path)]
        for row, audio_path in zip(rows, matched_audio_paths)
        if audio_path is not None
    ]

    # Build the dataset from the metadata and the audio files. This embeds all the audio
    # files into the dataset as parquet files
    dataset = Dataset.from_dict(
        mapping={
            "id_recording": [row[0] for row in rows],
            "id_sentence": [row[1] for row in rows],
            "id_speaker": [row[2] for row in rows],
            "id_validator": [row[3] for row in rows],
            **{
                feature: [row[i] for row in rows]
                for i, feature in enumerate(non_id_features, start=4)
            },
            "audio": [row[-1] for row in rows],
        }
    )
    dataset = dataset.cast_column("audio", Audio())
    return dataset


def list_audio_files(audio_dir: Path, max_attempts: int = 10) -> list[Path]:
    """List all the audio files in the given directory.

    Args:
        audio_dir:
            The directory containing the audio files.
        max_attempts (optional):
            The maximum number of attempts to list the audio files. Defaults to 10.

    Returns:
        A list of paths to the audio files.

    Raises:
        OSError:
            If the audio files cannot be listed.
    """
    for _ in range(max_attempts):
        try:
            return list(audio_dir.glob("*.wav"))
        except OSError:
            sleep(1)
    else:
        raise OSError(f"Failed to list the audio files in {audio_dir!r}.")


############################################
##### Building the conversation subset #####
############################################


# TODO: Implement this function
def build_conversation_dataset(
    metadata_database_path: Path, audio_dir: Path
) -> Dataset:
    """Build the CoRal conversation dataset.

    Args:
        metadata_database_path:
            Path to the SQLite database containing the metadata.
        audio_dir:
            Path to the directory containing the audio files.

    Returns:
        The CoRal conversation dataset.
    """
    dataset = Dataset.from_dict({})
    return dataset


#####################################
##### Splitting of the datasets #####
#####################################


def split_dataset(dataset: Dataset) -> DatasetDict | None:
    """Split a dataset into train, validation and test sets.

    Args:
        dataset:
            The dataset to split.

    Returns:
        The split dataset, or None if no training samples are found.

    Raises:
        ValueError:
            If no training samples are found.
    """
    if len(dataset) == 0:
        return None

    with no_progress_bar():
        train_dataset = dataset.filter(function=examples_belong_to_train, batched=True)
    splits = dict(train=train_dataset)

    with no_progress_bar():
        val_dataset = dataset.filter(function=examples_belong_to_val, batched=True)
    if len(val_dataset) > 0:
        splits["val"] = val_dataset

    with no_progress_bar():
        test_dataset = dataset.filter(function=examples_belong_to_test, batched=True)
    if len(test_dataset) > 0:
        splits["test"] = test_dataset

    return DatasetDict(splits)


def examples_belong_to_train(examples: dict[str, list]) -> list[bool]:
    """Check if each example belongs to the training set.

    Args:
        examples:
            A batch of examples.

    Returns:
        A list of booleans indicating whether each example belongs to the training
        set.
    """
    return [
        speaker_id not in VALIDATION_SET_SPEAKER_IDS + TEST_SET_SPEAKER_IDS
        for speaker_id in examples["id_speaker"]
    ]


def examples_belong_to_val(examples: dict[str, list]) -> list[bool]:
    """Check if each example belongs to the validation set.

    Args:
        examples:
            A batch of examples.

    Returns:
        A list of booleans indicating whether each example belongs to the validation
        set.
    """
    return [
        speaker_id in VALIDATION_SET_SPEAKER_IDS
        for speaker_id in examples["id_speaker"]
    ]


def examples_belong_to_test(examples: dict[str, list]) -> list[bool]:
    """Check if each example belongs to the test set.

    Args:
        examples:
            A batch of examples.

    Returns:
        A list of booleans indicating whether each example belongs to the test set.
    """
    return [speaker_id in TEST_SET_SPEAKER_IDS for speaker_id in examples["id_speaker"]]


#####################################
##### Uploading of the datasets #####
#####################################


def upload_dataset(
    read_aloud_dataset: DatasetDict | None,
    conversation_dataset: DatasetDict | None,
    hub_id: str,
) -> None:
    """Upload the dataset to the Hugging Face Hub.

    Args:
        read_aloud_dataset:
            The read-aloud dataset, or None if no such dataset exists.
        conversation_dataset:
            The conversation dataset, or None if no such dataset exists.
        hub_id:
            Identifier of the Hugging Face Hub repository.
    """
    if read_aloud_dataset is not None:
        read_aloud_dataset.push_to_hub(
            repo_id=hub_id,
            config_name="read_aloud",
            private=True,
            max_shard_size="500MB",
            commit_message="Add the CoRal read-aloud dataset",
        )
    if conversation_dataset is not None:
        conversation_dataset.push_to_hub(
            repo_id=hub_id,
            config_name="conversation",
            private=True,
            max_shard_size="500MB",
            commit_message="Add the CoRal conversation dataset",
        )


#############################
##### Utility functions #####
#############################


def copy_audio_directory_to_cwd(audio_dir: Path) -> Path:
    """Copy audio files to the current working directory.

    Args:
        audio_dir:
            The directory containing the audio files.

    Returns:
        The new directory containing the audio files.
    """
    new_audio_dir = Path.cwd() / audio_dir.name
    new_audio_dir.mkdir(exist_ok=True)

    # Get list of subdirectories of the audio directory, or abort of none exist
    audio_subdirs = [path for path in audio_dir.iterdir() if path.is_dir()]
    if not audio_subdirs:
        return new_audio_dir

    # Compress all subdirectories that are not already compressed
    with Parallel(n_jobs=-1, backend="threading") as parallel:
        parallel(
            delayed(function=compress_dir)(directory=subdir)
            for subdir in tqdm(iterable=audio_subdirs, desc="Compressing audio files")
        )

    # Copy all the compressed audio files to the current working directory
    with Parallel(n_jobs=-1, backend="threading") as parallel:
        parallel(
            delayed(function=copy_tarfile)(
                tarfile=tarfile, destination_dir=new_audio_dir
            )
            for tarfile in tqdm(
                iterable=list(audio_dir.glob("*.tar")),
                desc="Copying compressed audio files",
            )
        )

    # Uncompress all the compressed audio files in the current working directory
    with Parallel(n_jobs=-1, backend="threading") as parallel:
        parallel(
            delayed(function=decompress_file)(
                compressed_file=compressed_subdir, destination_dir=new_audio_dir
            )
            for compressed_subdir in tqdm(
                iterable=list(new_audio_dir.glob("*.tar")),
                desc="Uncompressing audio files",
            )
        )

    return new_audio_dir


def compress_dir(directory: Path) -> Path:
    """Compress a directory using tar.

    Args:
        directory:
            The directory to compress.

    Returns:
        The path to the compressed file.
    """
    if not directory.with_suffix(".tar").exists():
        with tarfile.open(name=f"{str(directory)}.tar", mode="w") as tar:
            tar.add(name=directory, arcname=directory.name)
    return directory.with_suffix(".tar")


def copy_tarfile(tarfile: Path, destination_dir: Path) -> None:
    """Copy a tarfile to a destination directory.

    Args:
        tarfile:
            The tarfile to copy.
        destination_dir:
            The destination directory.
    """
    destination_path = destination_dir / tarfile.name
    decompressed_path = destination_dir / tarfile.stem
    if not destination_path.exists() and not decompressed_path.exists():
        shutil.copy(src=tarfile, dst=destination_dir)


def decompress_file(compressed_file: Path, destination_dir: Path) -> Path:
    """Decompress a file using tar.

    Args:
        compressed_file:
            The file to decompress.
        destination_dir:
            The directory to decompress the file into.

    Returns:
        The path to the decompressed directory.
    """
    if not (destination_dir / compressed_file.stem).exists():
        with tarfile.open(name=compressed_file, mode="r") as tar:
            tar.extractall(path=destination_dir)
    compressed_file.unlink()
    return destination_dir / compressed_file.stem


class no_progress_bar:
    """Context manager that disables the progress bar."""

    def __enter__(self):
        """Disable the progress bar."""
        disable_progress_bar()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Re-enable the progress bar."""
        enable_progress_bar()


if __name__ == "__main__":
    main()
