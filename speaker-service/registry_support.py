"""Internal marshalling between SQLite rows and registry value objects.

Kept out of ``registry`` so the per-aggregate operation modules stay within a
reviewable size; this module is not part of the public surface.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from registry_models import (
    BatchItem,
    BatchState,
    InvalidEmbeddingError,
    Meeting,
    RegistryError,
    Speaker,
    SpeakerBatch,
    SpeakerConsent,
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
        title=row["title"],
    )


def meeting_from_row(row: sqlite3.Row) -> Meeting:
    return Meeting(
        id=int(row["id"]),
        title=str(row["title"]),
        date=row["date"],
        audio_path=row["audio_path"],
        created_at=str(row["created_at"]),
        topic=row["topic"],
        location=row["location"],
        duration_s=row["duration_s"],
        summary_json=row["summary_json"],
        original_title=row["original_title"],
    )


def consent_from_row(row: sqlite3.Row) -> SpeakerConsent:
    return SpeakerConsent(
        id=int(row["id"]),
        speaker_id=int(row["speaker_id"]),
        granted_at=str(row["granted_at"]),
        purpose=str(row["purpose"]),
        retention_until=str(row["retention_until"]),
        source_batch_id=row["source_batch_id"],
        revoked_at=row["revoked_at"],
    )


def batch_from_row(row: sqlite3.Row) -> SpeakerBatch:
    return SpeakerBatch(
        id=int(row["id"]),
        meeting_id=int(row["meeting_id"]),
        state=BatchState(row["state"]),
        created_at=str(row["created_at"]),
        resolved_at=row["resolved_at"],
    )


def batch_item_from_row(row: sqlite3.Row) -> BatchItem:
    return BatchItem(
        id=int(row["id"]),
        batch_id=int(row["batch_id"]),
        cluster_id=int(row["cluster_id"]),
        suggested_speaker_id=row["suggested_speaker_id"],
        similarity=row["similarity"],
        resolution=row["resolution"],
        resolved_speaker_id=row["resolved_speaker_id"],
        resolved_at=row["resolved_at"],
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
