"""Speaker and voiceprint operations mixed into ``registry.Registry``.

Voiceprints are stored with their exact ``{model_id, revision, dim}``
provenance and indexed by sqlite-vec for cosine KNN, so a vector from another
model can never be silently matched. ``delete_speaker`` implements the
documented ON DELETE policy across aggregates.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from registry_models import (
    ClusterState,
    Match,
    RegistryError,
    Speaker,
    SpeakerNotFoundError,
    StoredVoiceprint,
    VersionedVector,
)
from registry_support import (
    last_id,
    query_vector,
    speaker_from_row,
    store_vector,
    voiceprint_from_row,
)


class SpeakerOps:
    """Speakers and their versioned voiceprints."""

    _conn: sqlite3.Connection

    def add_speaker(
        self, name: str, organization: str | None = None, notes: str | None = None
    ) -> int:
        clean = name.strip()
        if not clean:
            raise RegistryError("speaker name must not be empty")
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO speakers(name, organization, notes) VALUES (?, ?, ?)",
                (clean, organization, notes),
            )
        return last_id(cursor)

    def get_speaker(self, speaker_id: int) -> Speaker | None:
        row = self._conn.execute(
            "SELECT id, name, organization, notes, created_at"
            " FROM speakers WHERE id = ?",
            (speaker_id,),
        ).fetchone()
        return speaker_from_row(row) if row is not None else None

    def list_speakers(self) -> list[Speaker]:
        rows = self._conn.execute(
            "SELECT id, name, organization, notes, created_at FROM speakers ORDER BY id"
        ).fetchall()
        return [speaker_from_row(row) for row in rows]

    def rename_speaker(self, speaker_id: int, name: str) -> bool:
        clean = name.strip()
        if not clean:
            raise RegistryError("speaker name must not be empty")
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE speakers SET name = ? WHERE id = ?", (clean, speaker_id)
            )
        return cursor.rowcount > 0

    def delete_speaker(self, speaker_id: int) -> bool:
        """Apply the documented ON DELETE policy; True if a speaker was removed."""
        with self._conn:
            cluster_ids = [
                int(row["cluster_id"])
                for row in self._conn.execute(
                    "SELECT cluster_id FROM meeting_speakers WHERE speaker_id = ?",
                    (speaker_id,),
                ).fetchall()
            ]
            if cluster_ids:
                placeholders = ", ".join("?" * len(cluster_ids))
                self._conn.execute(
                    "UPDATE clusters SET state = ?, label = NULL"
                    f" WHERE id IN ({placeholders})",
                    (ClusterState.UNKNOWN.value, *cluster_ids),
                )
            # Explicit first so the vec0 purge trigger always fires; the FK
            # CASCADE would otherwise do it implicitly.
            self._conn.execute(
                "DELETE FROM voiceprints WHERE speaker_id = ?", (speaker_id,)
            )
            cursor = self._conn.execute(
                "DELETE FROM speakers WHERE id = ?", (speaker_id,)
            )
        return cursor.rowcount > 0

    def add_voiceprint(self, speaker_id: int, embedding: VersionedVector) -> int:
        """Store a vector WITH its pinned-model provenance (never a bare array)."""
        vector = store_vector(embedding)
        if self.get_speaker(speaker_id) is None:
            raise SpeakerNotFoundError(speaker_id)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO voiceprints(speaker_id, embedding, model_id,"
                " revision, dim) VALUES (?, ?, ?, ?, ?)",
                (
                    speaker_id,
                    vector.tobytes(),
                    embedding.model_id,
                    embedding.revision,
                    embedding.dim,
                ),
            )
        return last_id(cursor)

    def voiceprints_for_speaker(self, speaker_id: int) -> list[StoredVoiceprint]:
        rows = self._conn.execute(
            "SELECT id, speaker_id, embedding, model_id, revision, dim, created_at"
            " FROM voiceprints WHERE speaker_id = ? ORDER BY id",
            (speaker_id,),
        ).fetchall()
        return [voiceprint_from_row(row) for row in rows]

    def nearest_voiceprints(
        self, embedding: VersionedVector | np.ndarray, k: int = 5
    ) -> list[Match]:
        """Cosine KNN (sqlite-vec): smallest ``distance = 1 - similarity`` first."""
        if k < 1:
            raise RegistryError("k must be at least 1")
        payload = query_vector(embedding).tobytes()
        rows = self._conn.execute(
            "SELECT vp.speaker_id, vp.id, vp.model_id, vp.revision, vp.dim,"
            " v.distance FROM voiceprints_vec v"
            " JOIN voiceprints vp ON vp.id = v.voiceprint_id"
            " WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (payload, k),
        ).fetchall()
        return [
            Match(
                speaker_id=int(row["speaker_id"]),
                voiceprint_id=int(row["id"]),
                model_id=str(row["model_id"]),
                revision=str(row["revision"]),
                dim=int(row["dim"]),
                distance=float(row["distance"]),
            )
            for row in rows
        ]
