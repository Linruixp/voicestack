"""Core service layer behind the HTTP routes.

Every function receives the per-request ``registry.Registry`` plus the injected
collaborators in :class:`ServiceDeps` (meeting runner, lazy identity embedder,
audio loader, settings), so the route modules stay thin and tests run without
models. Cross-aggregate mutations live in :mod:`api_operations`.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from fastapi import HTTPException

import pipeline
from config import Settings
from pipeline import load_audio
from registry import ClusterState, Job, Meeting, Registry, Speaker
from summarizer import SummaryBackend

UNKNOWN_SPEAKER = pipeline.UNKNOWN_SPEAKER


class MeetingRunner(Protocol):
    def __call__(
        self, path: Path, title: str, registry: Registry
    ) -> pipeline.MeetingResult: ...


class Embedder(Protocol):
    def embed_file(self, path: str | Path) -> Any: ...

    def embed_waveform(
        self, waveform: np.ndarray, sample_rate: int = 16_000
    ) -> Any: ...

    def embed_cluster(self, members: Sequence[Any]) -> Any: ...


class AudioLoader(Protocol):
    def __call__(self, path: Path) -> tuple[np.ndarray, int]: ...


class LazyEmbedder:
    """Loads the (heavy) identity model on first use, once per process."""

    def __init__(self, factory: Callable[[], Embedder]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._embedder: Embedder | None = None

    def _load(self) -> Embedder:
        with self._lock:
            if self._embedder is None:
                self._embedder = self._factory()
            return self._embedder

    def embed_file(self, path: str | Path) -> Any:
        return self._load().embed_file(path)

    def embed_waveform(self, waveform: np.ndarray, sample_rate: int = 16_000) -> Any:
        return self._load().embed_waveform(waveform, sample_rate)

    def embed_cluster(self, members: Sequence[Any]) -> Any:
        return self._load().embed_cluster(members)


@dataclass(frozen=True, slots=True)
class ServiceDeps:
    """Injected collaborators, assembled once by ``create_app``."""

    runner: MeetingRunner
    embedder: Embedder
    audio_loader: AudioLoader
    settings: Settings
    summarizer: SummaryBackend | None = None


def default_runner(
    path: Path, title: str, registry: Registry
) -> pipeline.MeetingResult:
    return pipeline.transcribe_meeting(path, title, registry=registry)


def default_embedder_factory() -> Embedder:
    from embed import IdentityEmbedder

    return IdentityEmbedder()


def save_upload(directory: Path, filename: str | None, content: bytes) -> Path:
    """Write an uploaded file into the owner-only uploads directory."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    safe_name = Path(filename or "meeting.wav").name.strip() or "meeting.wav"
    target = directory / f"{uuid.uuid4().hex}_{safe_name}"
    target.write_bytes(content)
    os.chmod(target, 0o600)
    return target


def run_meeting(
    registry: Registry, deps: ServiceDeps, path: Path, title: str
) -> pipeline.MeetingResult:
    """Run the meeting pipeline; delete the upload when the switch is on."""
    try:
        return deps.runner(path, title, registry)
    finally:
        if deps.settings.delete_audio_after_transcribe:
            path.unlink(missing_ok=True)


def speaker_payload(
    speaker: Speaker, voiceprint_count: int, *, consent: Any = None
) -> dict[str, Any]:
    payload = {
        "id": speaker.id,
        "name": speaker.name,
        "organization": speaker.organization,
        "notes": speaker.notes,
        "title": speaker.title,
        "created_at": speaker.created_at,
        "voiceprint_count": voiceprint_count,
    }
    if consent is not None:
        payload["consent_granted_at"] = consent.granted_at
        payload["retention_until"] = consent.retention_until
        payload["consent_revoked_at"] = consent.revoked_at
        payload["consent_expired"] = _retention_expired(consent.retention_until)
    else:
        payload["consent_granted_at"] = None
        payload["retention_until"] = None
        payload["consent_revoked_at"] = None
        payload["consent_expired"] = False
    return payload


def _retention_expired(retention_until: str) -> bool:
    try:
        return datetime.fromisoformat(retention_until) <= datetime.now(UTC)
    except (ValueError, TypeError):
        return False


def meeting_payload(meeting: Meeting) -> dict[str, Any]:
    return {
        "id": meeting.id,
        "title": meeting.title,
        "date": meeting.date,
        "audio_path": meeting.audio_path,
        "created_at": meeting.created_at,
        "topic": meeting.topic,
        "location": meeting.location,
        "duration_s": meeting.duration_s,
        "summary_json": meeting.summary_json,
        "original_title": meeting.original_title,
    }


def meeting_result_payload(
    result: pipeline.MeetingResult, settings: Settings
) -> dict[str, Any]:
    """The POST /meetings payload: pipeline result plus handoff and ui_url."""
    payload = result.to_dict()
    if result.batch_id is not None:
        payload["ui_url"] = (
            f"{settings.resolved_ui_base_url}/?meeting={result.meeting_id}"
            f"&task=speakers&batch={result.batch_id}"
        )
        payload["handoff"] = {
            "needed": True,
            "batch_id": result.batch_id,
            "unknown_count": len(result.unknown_clusters),
            "threshold": settings.handoff_threshold,
        }
    else:
        payload["ui_url"] = None
        payload["handoff"] = {
            "needed": False,
            "batch_id": None,
            "unknown_count": len(result.unknown_clusters),
            "threshold": settings.handoff_threshold,
        }
    return payload


def speaker_sources(registry: Registry, speaker_id: int) -> list[dict[str, Any]]:
    """The speaker's linked clusters — the audio sources usable for re-enrollment."""
    if registry.get_speaker(speaker_id) is None:
        raise HTTPException(404, f"speaker {speaker_id} does not exist")
    sources: list[dict[str, Any]] = []
    for link in registry.links_for_speaker(speaker_id):
        meeting = registry.get_meeting(link.meeting_id)
        members = [
            segment
            for segment in registry.segments_for_meeting(link.meeting_id)
            if segment.cluster_id == link.cluster_id
        ]
        if not members:
            continue
        sources.append(
            {
                "cluster_id": link.cluster_id,
                "meeting_id": link.meeting_id,
                "meeting_title": meeting.title if meeting else f"#{link.meeting_id}",
                "segment_count": len(members),
                "start": min(segment.start for segment in members),
                "end": max(segment.end for segment in members),
            }
        )
    return sources


def meeting_row_payload(
    registry: Registry, meeting: Meeting, names: dict[int, str] | None = None
) -> dict[str, Any]:
    if names is None:
        names = {speaker.id: speaker.name for speaker in registry.list_speakers()}
    links = registry.meeting_speakers_for_meeting(meeting.id)
    participants = sorted(
        {names[link.speaker_id] for link in links if link.speaker_id in names}
    )
    unknown_count = sum(
        1
        for cluster in registry.clusters_for_meeting(meeting.id)
        if cluster.state is ClusterState.UNKNOWN
    )
    payload = meeting_payload(meeting)
    payload["participants"] = participants
    payload["unknown_count"] = unknown_count
    return payload


def list_meeting_rows(registry: Registry, **filters: Any) -> list[dict[str, Any]]:
    names = {speaker.id: speaker.name for speaker in registry.list_speakers()}
    return [
        meeting_row_payload(registry, meeting, names)
        for meeting in registry.search_meetings(**filters)
    ]


def job_payload(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "meeting_id": job.meeting_id,
        "state": job.state.value,
        "error": job.error,
        "created_at": job.created_at,
    }


def _parsed_summary(meeting: Meeting) -> dict[str, Any] | None:
    if not meeting.summary_json:
        return None
    try:
        return json.loads(meeting.summary_json)
    except ValueError:
        return None


def meeting_detail(registry: Registry, meeting_id: int) -> dict[str, Any]:
    """The transcript view: segments with resolved names plus unknown clusters."""
    meeting = registry.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, f"meeting {meeting_id} does not exist")
    segments = registry.segments_for_meeting(meeting_id)
    names = {speaker.id: speaker.name for speaker in registry.list_speakers()}
    links = registry.meeting_speakers_for_meeting(meeting_id)
    links_by_cluster = {link.cluster_id: link for link in links}
    batch = registry.open_batch_for_meeting(meeting_id)
    payloads = [
        {
            "id": segment.id,
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "speaker_id": segment.speaker_id,
            "speaker_name": names.get(segment.speaker_id),
            "cluster_id": segment.cluster_id,
        }
        for segment in segments
    ]
    unknown = []
    for cluster in registry.clusters_for_meeting(meeting_id):
        if cluster.state is not ClusterState.UNKNOWN:
            continue
        members = [item for item in segments if item.cluster_id == cluster.id]
        link = links_by_cluster.get(cluster.id)
        unknown.append(
            {
                "cluster_id": cluster.id,
                "label": cluster.label,
                "state": cluster.state.value,
                "speaker_id": link.speaker_id if link else None,
                "segment_count": len(members),
                "start": min((item.start for item in members), default=0.0),
                "end": max((item.end for item in members), default=0.0),
            }
        )
    return {
        "meeting": meeting_payload(meeting),
        "summary": _parsed_summary(meeting),
        "speakers": sorted(
            {
                speaker
                for speaker in (item["speaker_name"] for item in payloads)
                if speaker
            }
        ),
        "segments": payloads,
        "unknown_clusters": unknown,
        "handoff": {
            "needed": batch is not None,
            "batch_id": batch.id if batch else None,
            "unknown_count": len(unknown),
        },
        "links": [
            {
                "cluster_id": link.cluster_id,
                "speaker_id": link.speaker_id,
                "confidence": link.confidence,
            }
            for link in links
        ],
    }


def identify_audio(registry: Registry, deps: ServiceDeps, path: Path) -> dict[str, Any]:
    """Embed one clip and return the best enrolled match (or ``unknown``)."""
    if not path.is_file():
        raise HTTPException(404, f"audio file {path} does not exist")
    from match import MatchError, match_speaker

    try:
        embedding = deps.embedder.embed_file(path)
        return match_speaker(registry, embedding).to_dict()
    except MatchError as exc:
        raise HTTPException(400, str(exc)) from exc


__all__ = [
    "AudioLoader",
    "Embedder",
    "LazyEmbedder",
    "MeetingRunner",
    "ServiceDeps",
    "default_embedder_factory",
    "default_runner",
    "identify_audio",
    "job_payload",
    "load_audio",
    "meeting_detail",
    "meeting_payload",
    "meeting_result_payload",
    "run_meeting",
    "save_upload",
    "speaker_payload",
]
