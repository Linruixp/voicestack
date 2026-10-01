"""Domain vocabulary of the enrollment workflow.

Frozen outcome value objects and typed errors raised by :mod:`enrollment`
(which re-exports them for callers). Kept separate so the merge/split SQL
logic and this vocabulary each stay well under the 250-LOC ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from registry import RegistryError

# A cluster needs at least this much voiced audio to yield a trustworthy
# canonical voiceprint; a 0.3 s cluster is refused.
MIN_VOICED_SECONDS: Final = 1.0


class EnrollmentError(RegistryError):
    """Enrollment or transcript editing was refused."""


class ClusterNotFoundError(EnrollmentError):
    """A cluster id does not belong to the referenced meeting."""

    def __init__(self, meeting_id: int, cluster_id: int) -> None:
        super().__init__(
            f"cluster {cluster_id} does not belong to meeting {meeting_id}"
        )
        self.meeting_id = meeting_id
        self.cluster_id = cluster_id


class ClusterTooShortError(EnrollmentError):
    """A cluster had too little voiced audio for a voiceprint."""

    def __init__(
        self, cluster_id: int, voiced_seconds: float, required_seconds: float
    ) -> None:
        super().__init__(
            f"cluster {cluster_id} has {voiced_seconds:.2f}s of voiced audio;"
            f" at least {required_seconds:.1f}s is required to build a voiceprint"
        )
        self.cluster_id = cluster_id
        self.voiced_seconds = voiced_seconds
        self.required_seconds = required_seconds


class MeetingAudioUnavailableError(EnrollmentError):
    """The meeting has no audio file on disk to embed from."""

    def __init__(self, meeting_id: int) -> None:
        super().__init__(f"meeting {meeting_id} has no available audio")
        self.meeting_id = meeting_id


class InvalidSplitError(EnrollmentError):
    """The split point would leave one side of the cluster empty."""

    def __init__(self) -> None:
        super().__init__("split point must fall between two segments")


class SelfMergeError(EnrollmentError):
    """A speaker cannot be merged into itself."""

    def __init__(self, speaker_id: int) -> None:
        super().__init__(f"cannot merge speaker {speaker_id} into itself")
        self.speaker_id = speaker_id


@dataclass(frozen=True, slots=True)
class EnrolledSpeaker:
    """Outcome of enrolling an unknown cluster as a new speaker."""

    speaker_id: int
    voiceprint_id: int
    cluster_id: int
    name: str


@dataclass(frozen=True, slots=True)
class AttachedVoiceprint:
    """Outcome of attaching a cluster's voiceprint to an existing speaker."""

    speaker_id: int
    voiceprint_id: int
    cluster_id: int
    similarity: float | None


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """How many references moved from the source to the target speaker."""

    target_speaker_id: int
    source_speaker_id: int
    voiceprints_moved: int
    links_moved: int
    segments_moved: int


@dataclass(frozen=True, slots=True)
class SplitOutcome:
    """How many segments moved into the newly created cluster."""

    cluster_id: int
    new_cluster_id: int
    moved_segments: int
