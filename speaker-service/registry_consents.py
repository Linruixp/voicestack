"""Consent records mixed into ``registry.Registry``.

A consent row ties a stored voiceprint to a purpose and a retention term, so the
directory can show (and the operator can audit) why a biometric is held and for
how long. ``delete_speaker`` cascades these rows away with the speaker.
"""

from __future__ import annotations

import sqlite3

from registry_models import SpeakerConsent, SpeakerNotFoundError
from registry_support import consent_from_row, last_id

_COLUMNS = (
    "id, speaker_id, granted_at, purpose, retention_until, source_batch_id, revoked_at"
)


class ConsentOps:
    """Consent records for stored voiceprints."""

    _conn: sqlite3.Connection

    def add_consent(
        self,
        speaker_id: int,
        *,
        purpose: str,
        retention_until: str,
        source_batch_id: int | None = None,
    ) -> int:
        if self.get_speaker(speaker_id) is None:
            raise SpeakerNotFoundError(speaker_id)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO enroll_consents(speaker_id, purpose,"
                " retention_until, source_batch_id) VALUES (?, ?, ?, ?)",
                (speaker_id, purpose, retention_until, source_batch_id),
            )
        return last_id(cursor)

    def consents_for_speaker(self, speaker_id: int) -> list[SpeakerConsent]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM enroll_consents WHERE speaker_id = ? ORDER BY id",
            (speaker_id,),
        ).fetchall()
        return [consent_from_row(row) for row in rows]

    def latest_consent_for_speaker(self, speaker_id: int) -> SpeakerConsent | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM enroll_consents WHERE speaker_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (speaker_id,),
        ).fetchone()
        return consent_from_row(row) if row is not None else None

    def revoke_consent(self, consent_id: int) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE enroll_consents SET revoked_at ="
                " strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (consent_id,),
            )
        return cursor.rowcount > 0
