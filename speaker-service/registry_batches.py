"""Speaker-naming batch operations mixed into ``registry.Registry``.

A batch is the durable "these unknown clusters still need naming" record for one
meeting. The HTTP/Web-UI handoff reads it and resolves it; the registry only
stores state.
"""

from __future__ import annotations

import sqlite3

from registry_models import (
    BatchItem,
    BatchState,
    MeetingNotFoundError,
    SpeakerBatch,
)
from registry_support import batch_from_row, batch_item_from_row, last_id


class BatchOps:
    """Speaker batches and their items."""

    _conn: sqlite3.Connection

    def create_speaker_batch(self, meeting_id: int) -> int:
        if self.get_meeting(meeting_id) is None:
            raise MeetingNotFoundError(meeting_id)
        existing = self.open_batch_for_meeting(meeting_id)
        if existing is not None:
            return existing.id
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO speaker_batches(meeting_id) VALUES (?)", (meeting_id,)
            )
        return last_id(cursor)

    def add_batch_item(
        self,
        batch_id: int,
        cluster_id: int,
        *,
        suggested_speaker_id: int | None = None,
        similarity: float | None = None,
    ) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO batch_items(batch_id, cluster_id,"
                " suggested_speaker_id, similarity) VALUES (?, ?, ?, ?)",
                (batch_id, cluster_id, suggested_speaker_id, similarity),
            )
        return last_id(cursor)

    def get_speaker_batch(self, batch_id: int) -> SpeakerBatch | None:
        row = self._conn.execute(
            "SELECT id, meeting_id, state, created_at, resolved_at"
            " FROM speaker_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        return batch_from_row(row) if row is not None else None

    def batch_items(self, batch_id: int) -> list[BatchItem]:
        rows = self._conn.execute(
            "SELECT id, batch_id, cluster_id, suggested_speaker_id, similarity,"
            " resolution, resolved_speaker_id, resolved_at"
            " FROM batch_items WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [batch_item_from_row(row) for row in rows]

    def open_batch_for_meeting(self, meeting_id: int) -> SpeakerBatch | None:
        row = self._conn.execute(
            "SELECT id, meeting_id, state, created_at, resolved_at"
            " FROM speaker_batches WHERE meeting_id = ? AND state = 'open'"
            " ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        return batch_from_row(row) if row is not None else None

    def mark_batch_item(
        self,
        batch_id: int,
        cluster_id: int,
        resolution: str,
        resolved_speaker_id: int | None = None,
    ) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE batch_items SET resolution = ?, resolved_speaker_id = ?,"
                " resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " WHERE batch_id = ? AND cluster_id = ?",
                (resolution, resolved_speaker_id, batch_id, cluster_id),
            )
        return cursor.rowcount > 0

    def close_batch(
        self, batch_id: int, state: BatchState = BatchState.RESOLVED
    ) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE speaker_batches SET state = ?,"
                " resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (state.value, batch_id),
            )
        return cursor.rowcount > 0
