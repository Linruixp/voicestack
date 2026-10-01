"""Meeting, cluster, segment, link and job operations for ``registry.Registry``."""

from __future__ import annotations

import sqlite3

from registry_models import (
    Cluster,
    ClusterLink,
    ClusterState,
    Job,
    JobState,
    Meeting,
    MeetingNotFoundError,
    MeetingSpeakerLink,
    NewSegment,
    Segment,
)
from registry_support import last_id, meeting_from_row


class MeetingOps:
    """Meetings and everything produced by processing one."""

    _conn: sqlite3.Connection

    def create_meeting(
        self, title: str, date: str | None = None, audio_path: str | None = None
    ) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO meetings(title, date, audio_path) VALUES (?, ?, ?)",
                (title, date, audio_path),
            )
        return last_id(cursor)

    def get_meeting(self, meeting_id: int) -> Meeting | None:
        row = self._conn.execute(
            "SELECT id, title, date, audio_path, created_at FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        return meeting_from_row(row) if row is not None else None

    def list_meetings(self) -> list[Meeting]:
        rows = self._conn.execute(
            "SELECT id, title, date, audio_path, created_at FROM meetings ORDER BY id"
        ).fetchall()
        return [meeting_from_row(row) for row in rows]

    def delete_meeting(self, meeting_id: int) -> bool:
        """Remove a meeting and everything derived from it.

        Clusters, speaker links, segments and jobs are deleted by the schema's
        ``ON DELETE CASCADE``; used to roll back a failed pre-transcription
        attempt so no orphan rows remain.
        """
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM meetings WHERE id = ?", (meeting_id,)
            )
        return cursor.rowcount > 0

    def add_cluster(
        self,
        meeting_id: int,
        label: str | None = None,
        state: ClusterState = ClusterState.UNKNOWN,
    ) -> int:
        self._require_meeting(meeting_id)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO clusters(meeting_id, label, state) VALUES (?, ?, ?)",
                (meeting_id, label, state.value),
            )
        return last_id(cursor)

    def get_cluster(self, cluster_id: int) -> Cluster | None:
        """One cluster by its globally unique id, or ``None``."""
        row = self._conn.execute(
            "SELECT id, meeting_id, label, state, created_at FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        if row is None:
            return None
        return Cluster(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            label=row["label"],
            state=ClusterState(row["state"]),
            created_at=str(row["created_at"]),
        )

    def clusters_for_meeting(self, meeting_id: int) -> list[Cluster]:
        rows = self._conn.execute(
            "SELECT id, meeting_id, label, state, created_at"
            " FROM clusters WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
        return [
            Cluster(
                id=int(row["id"]),
                meeting_id=int(row["meeting_id"]),
                label=row["label"],
                state=ClusterState(row["state"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def update_cluster(
        self, cluster_id: int, state: ClusterState, label: str | None
    ) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE clusters SET state = ?, label = ? WHERE id = ?",
                (state.value, label, cluster_id),
            )
        return cursor.rowcount > 0

    def add_segment(self, meeting_id: int, segment: NewSegment) -> int:
        self._require_meeting(meeting_id)
        with self._conn:
            cursor = self._conn.execute(
                'INSERT INTO segments(meeting_id, "start", "end", text,'
                " speaker_id, cluster_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    segment.start,
                    segment.end,
                    segment.text,
                    segment.speaker_id,
                    segment.cluster_id,
                ),
            )
        return last_id(cursor)

    def segments_for_meeting(self, meeting_id: int) -> list[Segment]:
        rows = self._conn.execute(
            'SELECT id, meeting_id, "start", "end", text, speaker_id, cluster_id'
            " FROM segments WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
        return [
            Segment(
                id=int(row["id"]),
                meeting_id=int(row["meeting_id"]),
                start=float(row["start"]),
                end=float(row["end"]),
                text=str(row["text"]),
                speaker_id=row["speaker_id"],
                cluster_id=row["cluster_id"],
            )
            for row in rows
        ]

    def add_meeting_speaker(self, link: ClusterLink) -> None:
        """Attach (or re-attach) a cluster of a meeting to a speaker."""
        self._require_meeting(link.meeting_id)
        with self._conn:
            self._conn.execute(
                "INSERT INTO meeting_speakers(meeting_id, speaker_id, cluster_id,"
                " confidence) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(cluster_id) DO UPDATE SET"
                " speaker_id = excluded.speaker_id,"
                " confidence = excluded.confidence",
                (
                    link.meeting_id,
                    link.speaker_id,
                    link.cluster_id,
                    link.confidence,
                ),
            )

    def meeting_speakers_for_meeting(self, meeting_id: int) -> list[MeetingSpeakerLink]:
        rows = self._conn.execute(
            "SELECT meeting_id, speaker_id, cluster_id, confidence"
            " FROM meeting_speakers WHERE meeting_id = ? ORDER BY cluster_id",
            (meeting_id,),
        ).fetchall()
        return [
            MeetingSpeakerLink(
                meeting_id=int(row["meeting_id"]),
                speaker_id=row["speaker_id"],
                cluster_id=int(row["cluster_id"]),
                confidence=row["confidence"],
            )
            for row in rows
        ]

    def create_job(self, meeting_id: int, state: JobState = JobState.QUEUED) -> int:
        self._require_meeting(meeting_id)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO jobs(meeting_id, state) VALUES (?, ?)",
                (meeting_id, state.value),
            )
        return last_id(cursor)

    def update_job(
        self, job_id: int, state: JobState, error: str | None = None
    ) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE jobs SET state = ?, error = ? WHERE id = ?",
                (state.value, error, job_id),
            )
        return cursor.rowcount > 0

    def get_job(self, job_id: int) -> Job | None:
        row = self._conn.execute(
            "SELECT id, meeting_id, state, error, created_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return Job(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            state=JobState(row["state"]),
            error=row["error"],
            created_at=str(row["created_at"]),
        )

    def has_active_jobs(self) -> bool:
        """True while any job is queued or running (the idle watchdog defers)."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE state IN (?, ?) LIMIT 1",
            (JobState.QUEUED.value, JobState.RUNNING.value),
        ).fetchone()
        return row is not None

    def _require_meeting(self, meeting_id: int) -> None:
        if self.get_meeting(meeting_id) is None:
            raise MeetingNotFoundError(meeting_id)
