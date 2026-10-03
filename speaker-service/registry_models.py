"""Domain vocabulary of the durable speaker registry.

Frozen value objects returned by ``registry.Registry`` plus the state enums and
typed errors. ``embed.Embedding`` satisfies :class:`VersionedVector`
structurally, so this module never imports torch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np


class VersionedVector(Protocol):
    """Structural contract: a vector that names the model that produced it."""

    vector: np.ndarray
    model_id: str
    revision: str
    dim: int


class ClusterState(StrEnum):
    """Lifecycle of a diarized cluster."""

    UNKNOWN = "unknown"
    NAMED = "named"
    ATTACHED = "attached"


class JobState(StrEnum):
    """Lifecycle of a meeting-processing job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class BatchState(StrEnum):
    """Lifecycle of a speaker-naming batch."""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class RegistryError(RuntimeError):
    """Base class for registry failures."""


class SpeakerNotFoundError(RegistryError):
    """A referenced speaker does not exist."""

    def __init__(self, speaker_id: int) -> None:
        super().__init__(f"speaker {speaker_id} does not exist")
        self.speaker_id = speaker_id


class MeetingNotFoundError(RegistryError):
    """A referenced meeting does not exist."""

    def __init__(self, meeting_id: int) -> None:
        super().__init__(f"meeting {meeting_id} does not exist")
        self.meeting_id = meeting_id


class InvalidEmbeddingError(RegistryError):
    """An embedding lacked pinned-model provenance or the expected shape."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Speaker:
    id: int
    name: str
    organization: str | None
    notes: str | None
    created_at: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class StoredVoiceprint:
    """A stored vector plus the exact model revision that produced it."""

    id: int
    speaker_id: int
    vector: np.ndarray
    model_id: str
    revision: str
    dim: int
    created_at: str


@dataclass(frozen=True, slots=True)
class Match:
    """One cosine KNN hit; ``distance`` is ``1 - cosine_similarity``."""

    speaker_id: int
    voiceprint_id: int
    model_id: str
    revision: str
    dim: int
    distance: float


@dataclass(frozen=True, slots=True)
class Meeting:
    id: int
    title: str
    date: str | None
    audio_path: str | None
    created_at: str
    topic: str | None = None
    location: str | None = None
    duration_s: float | None = None
    summary_json: str | None = None
    original_title: str | None = None


@dataclass(frozen=True, slots=True)
class Cluster:
    id: int
    meeting_id: int
    label: str | None
    state: ClusterState
    created_at: str


@dataclass(frozen=True, slots=True)
class Segment:
    id: int
    meeting_id: int
    start: float
    end: float
    text: str
    speaker_id: int | None
    cluster_id: int | None


@dataclass(frozen=True, slots=True)
class MeetingSpeakerLink:
    """A cluster mapped to a speaker; ``speaker_id`` is None while unknown."""

    meeting_id: int
    speaker_id: int | None
    cluster_id: int
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    meeting_id: int
    state: JobState
    error: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class NewSegment:
    """Input for ``Registry.add_segment`` (segment id assigned by SQLite)."""

    start: float
    end: float
    text: str
    speaker_id: int | None = None
    cluster_id: int | None = None


@dataclass(frozen=True, slots=True)
class ClusterLink:
    """Input for ``Registry.add_meeting_speaker``; omit speaker while unknown."""

    meeting_id: int
    cluster_id: int
    speaker_id: int | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class SpeakerBatch:
    """A pending set of unknown clusters to name for one meeting."""

    id: int
    meeting_id: int
    state: BatchState
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class BatchItem:
    """One unknown cluster awaiting a naming decision."""

    id: int
    batch_id: int
    cluster_id: int
    suggested_speaker_id: int | None
    similarity: float | None
    resolution: str | None
    resolved_speaker_id: int | None
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class SpeakerConsent:
    """A recorded consent for storing a speaker's voiceprint."""

    id: int
    speaker_id: int
    granted_at: str
    purpose: str
    retention_until: str
    source_batch_id: int | None
    revoked_at: str | None
