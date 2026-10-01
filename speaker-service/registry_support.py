"""Internal marshalling between SQLite rows and registry value objects.

Kept out of ``registry`` so the per-aggregate operation modules stay within a
reviewable size; this module is not part of the public surface.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from registry_models import (
    InvalidEmbeddingError,
    Meeting,
    RegistryError,
    Speaker,
    StoredVoiceprint,
    VersionedVector,
)
from registry_schema import VOICEPRINT_DIM


def last_id(cursor: sqlite3.Cursor) -> int:
    row_id = cursor.lastrowid
    if row_id is None:
        raise RegistryError("insert did not produce a row id")
    return int(row_id)


def store_vector(embedding: VersionedVector) -> np.ndarray:
    if isinstance(embedding, np.ndarray):
        raise InvalidEmbeddingError(
            "a bare vector cannot be stored: carry model_id, revision and dim"
        )
    vector = np.asarray(embedding.vector, dtype=np.float32).reshape(-1)
    if vector.shape != (VOICEPRINT_DIM,) or embedding.dim != VOICEPRINT_DIM:
        raise InvalidEmbeddingError(
            f"embedding must be {VOICEPRINT_DIM}-d, got shape {vector.shape}"
            f" and dim {embedding.dim}"
        )
    if not embedding.model_id or not embedding.revision:
        raise InvalidEmbeddingError("embedding is missing model_id or revision")
    return vector.astype("<f4", copy=False)


def query_vector(embedding: VersionedVector | np.ndarray) -> np.ndarray:
    vector = embedding if isinstance(embedding, np.ndarray) else embedding.vector
    normalized = np.asarray(vector, dtype=np.float32).reshape(-1)
    if normalized.shape != (VOICEPRINT_DIM,):
        raise InvalidEmbeddingError(
            f"query vector must be {VOICEPRINT_DIM}-d, got {normalized.shape}"
        )
    return normalized.astype("<f4", copy=False)


def speaker_from_row(row: sqlite3.Row) -> Speaker:
    return Speaker(
        id=int(row["id"]),
        name=str(row["name"]),
        organization=row["organization"],
        notes=row["notes"],
        created_at=str(row["created_at"]),
    )


def meeting_from_row(row: sqlite3.Row) -> Meeting:
    return Meeting(
        id=int(row["id"]),
        title=str(row["title"]),
        date=row["date"],
        audio_path=row["audio_path"],
        created_at=str(row["created_at"]),
    )


def voiceprint_from_row(row: sqlite3.Row) -> StoredVoiceprint:
    return StoredVoiceprint(
        id=int(row["id"]),
        speaker_id=int(row["speaker_id"]),
        vector=np.frombuffer(row["embedding"], dtype="<f4").copy(),
        model_id=str(row["model_id"]),
        revision=str(row["revision"]),
        dim=int(row["dim"]),
        created_at=str(row["created_at"]),
    )
