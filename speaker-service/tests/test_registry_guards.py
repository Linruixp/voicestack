"""Guard tests: the registry rejects anything not safely storable or deletable.

Given: an enrolled speaker and an empty registry.
When: a bare vector, a wrong-dimension vector or a vector without a model
revision is stored, a voiceprint is attached to a missing speaker, or a
missing speaker is deleted.
Then: invalid input fails loudly with a typed error and the store stays clean,
while deleting a missing speaker is a harmless no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from registry import (
    InvalidEmbeddingError,
    SpeakerNotFoundError,
    open_registry,
)

DIM = 256
MODEL_ID = "pyannote/wespeaker-voxceleb-resnet34-LM"
REVISION = "837717ddb9ff5507820346191109dc79c958d614"


@dataclass(frozen=True, slots=True)
class FakeEmbedding:
    vector: np.ndarray
    model_id: str = MODEL_ID
    revision: str = REVISION
    dim: int = DIM


def basis(index: int) -> np.ndarray:
    unit = np.zeros(DIM, dtype=np.float32)
    unit[index] = 1.0
    return unit


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def test_deleting_unknown_speaker_is_a_noop(db_path: Path) -> None:
    # Given: an empty registry
    with open_registry(db_path) as registry:
        # When/Then: deleting a missing speaker reports no deletion, no exception
        assert registry.delete_speaker(999) is False


def test_embedding_without_provenance_or_wrong_dim_is_rejected(
    db_path: Path,
) -> None:
    # Given: an enrolled speaker
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("Alice")
        # When/Then: a raw vector (no model id/revision) is rejected outright
        with pytest.raises(InvalidEmbeddingError):
            registry.add_voiceprint(speaker_id, basis(1))
        # And: a vector whose dimension is not the pinned 256-d is rejected
        with pytest.raises(InvalidEmbeddingError):
            registry.add_voiceprint(
                speaker_id, FakeEmbedding(vector=np.zeros(128, dtype=np.float32))
            )
        # And: an empty model revision is rejected
        with pytest.raises(InvalidEmbeddingError):
            registry.add_voiceprint(
                speaker_id, FakeEmbedding(vector=basis(1), revision="")
            )
        # And: nothing was stored
        assert registry.voiceprints_for_speaker(speaker_id) == []


def test_voiceprint_for_missing_speaker_is_rejected(db_path: Path) -> None:
    # Given: an empty registry
    with open_registry(db_path) as registry:
        # When/Then: attaching a voiceprint to a missing speaker fails loudly
        with pytest.raises(SpeakerNotFoundError):
            registry.add_voiceprint(42, FakeEmbedding(vector=basis(1)))
