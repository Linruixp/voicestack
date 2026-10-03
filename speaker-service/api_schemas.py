"""Request models for the mutating routes of the localhost HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpeakerCreate(_Strict):
    name: str
    organization: str | None = None
    notes: str | None = None
    title: str | None = None


class SpeakerPatch(_Strict):
    """Only fields present in the request body are applied."""

    name: str | None = None
    organization: str | None = None
    notes: str | None = None
    title: str | None = None


class VoiceprintAttach(_Strict):
    """Attach the cluster's canonically embedded voiceprint to a speaker.

    ``cluster_id`` is globally unique, so ``meeting_id`` is optional: when it is
    omitted the route resolves the meeting from the cluster. Supplying it is a
    checked assertion that the cluster belongs to that meeting.
    """

    cluster_id: int
    meeting_id: int | None = None


class EnrollRequest(_Strict):
    """Create a NEW speaker from a diarized cluster (atomic enrollment).

    Delegates to ``enrollment.enroll_speaker``: the cluster's voiceprint guard
    runs before the speaker row is written, so a refused enrollment leaves no
    orphan speaker or voiceprint.
    """

    name: str
    cluster_id: int
    organization: str | None = None
    notes: str | None = None
    title: str | None = None


class MeetingPatch(_Strict):
    """Only fields present in the request body are applied."""

    title: str | None = None
    topic: str | None = None
    location: str | None = None


class SegmentPatch(_Strict):
    """Inline correction of one transcript segment's text."""

    text: str = Field(min_length=1, max_length=20_000)


class MergeRequest(_Strict):
    source_speaker_id: int


class SplitRequest(_Strict):
    """Segments starting at or after ``at`` seconds move to the new cluster."""

    at: float


class IdentifyRequest(_Strict):
    audio_path: str


class ReEnrollRequest(_Strict):
    """Re-register a speaker's voice from one diarized cluster."""

    cluster_id: int


class BatchResolveItem(_Strict):
    """One decision for a pending cluster in a speaker batch."""

    cluster_id: int
    action: Literal["enroll", "attach", "skip"]
    name: str | None = None
    organization: str | None = None
    title: str | None = None
    speaker_id: int | None = None
    remember: bool = False


class BatchResolveRequest(_Strict):
    items: list[BatchResolveItem]
