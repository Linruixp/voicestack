"""Request models for the mutating routes of the localhost HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpeakerCreate(_Strict):
    name: str
    organization: str | None = None
    notes: str | None = None


class SpeakerPatch(_Strict):
    """Only fields present in the request body are applied."""

    name: str | None = None
    organization: str | None = None
    notes: str | None = None


class VoiceprintAttach(_Strict):
    """Attach the cluster's canonically embedded voiceprint to a speaker."""

    meeting_id: int
    cluster_id: int


class MergeRequest(_Strict):
    source_speaker_id: int


class SplitRequest(_Strict):
    """Segments starting at or after ``at`` seconds move to the new cluster."""

    at: float


class IdentifyRequest(_Strict):
    audio_path: str
