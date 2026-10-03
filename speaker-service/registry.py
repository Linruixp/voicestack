"""Durable speaker registry: one owner-only SQLite file (WAL + sqlite-vec).

Speakers, versioned voiceprints, meetings, transcripts, clusters and jobs are
persisted so a service restart loses nothing. Embeddings are stored with their
exact ``{model_id, revision, dim}`` provenance (``embed.Embedding``) and
indexed by sqlite-vec with the cosine metric, so a vector from another model
can never be silently matched. The HTTP service is the single writer; readers
and backups work concurrently thanks to WAL.

ON DELETE policy — see ``Registry.delete_speaker``: deleting a speaker purges
its voiceprints (and their vector index rows), nulls ``segments.speaker_id``
and ``meeting_speakers.speaker_id``, and resets every cluster that was linked
to it back to ``state='unknown'`` with a cleared label. Meetings, segments and
jobs survive.

Implementation is split by aggregate: ``registry_speakers`` (speakers +
voiceprints), ``registry_meetings`` (meetings + clusters + segments + jobs),
``registry_schema`` (file lifecycle), ``registry_support`` (row marshalling).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from config import get_settings
from registry_batches import BatchOps
from registry_consents import ConsentOps
from registry_meetings import MeetingOps
from registry_models import (
    BatchItem,
    BatchState,
    Cluster,
    ClusterLink,
    ClusterState,
    InvalidEmbeddingError,
    Job,
    JobState,
    Match,
    Meeting,
    MeetingNotFoundError,
    MeetingSpeakerLink,
    NewSegment,
    RegistryError,
    Segment,
    Speaker,
    SpeakerBatch,
    SpeakerConsent,
    SpeakerNotFoundError,
    StoredVoiceprint,
    VersionedVector,
)
from registry_schema import SCHEMA_VERSION, connect as _connect, current_version
from registry_speakers import SpeakerOps

__all__ = [
    "BatchItem",
    "BatchState",
    "Cluster",
    "ClusterLink",
    "ClusterState",
    "InvalidEmbeddingError",
    "Job",
    "JobState",
    "Match",
    "Meeting",
    "MeetingNotFoundError",
    "MeetingSpeakerLink",
    "NewSegment",
    "Registry",
    "RegistryError",
    "SCHEMA_VERSION",
    "Segment",
    "Speaker",
    "SpeakerBatch",
    "SpeakerConsent",
    "SpeakerNotFoundError",
    "StoredVoiceprint",
    "VersionedVector",
    "default_db_path",
    "open_registry",
]

DEFAULT_DB_NAME = "voicestack.db"


def open_registry(db_path: Path | None = None) -> Registry:
    """Open (creating/migrating if needed) the registry at ``db_path``."""
    path = Path(db_path) if db_path is not None else default_db_path()
    return Registry(_connect(path))


def default_db_path() -> Path:
    """The app-data database path: ``.../VoiceStudioStack/voicestack.db``."""
    return get_settings().db_path


class Registry(SpeakerOps, MeetingOps, BatchOps, ConsentOps):
    """File-backed registry; use :func:`open_registry` (context manager)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Registry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def schema_version(self) -> int:
        version = current_version(self._conn)
        if version is None:
            raise RegistryError("registry has no schema_version row")
        return version
