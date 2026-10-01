"""Core service layer behind the HTTP routes.

Every function receives the per-request ``registry.Registry`` plus the injected
collaborators in :class:`ServiceDeps` (meeting runner, lazy identity embedder,
audio loader, settings), so the route modules stay thin and tests run without
models. Cross-aggregate mutations live in :mod:`api_operations`.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from fastapi import HTTPException

import pipeline
from config import Settings
from registry import ClusterState, Job, Meeting, Registry, Speaker

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


def default_runner(
    path: Path, title: str, registry: Registry
) -> pipeline.MeetingResult:
    return pipeline.transcribe_meeting(path, title, registry=registry)


def default_embedder_factory() -> Embedder:
    from embed import IdentityEmbedder

    return IdentityEmbedder()


def load_audio(path: Path) -> tuple[np.ndarray, int]:
    import torchaudio

    waveform, sample_rate = torchaudio.load(str(path))
    return waveform.mean(dim=0).numpy(), int(sample_rate)


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


def speaker_payload(speaker: Speaker, voiceprint_count: int) -> dict[str, Any]:
    return {
        "id": speaker.id,
        "name": speaker.name,
        "organization": speaker.organization,
        "notes": speaker.notes,
        "created_at": speaker.created_at,
        "voiceprint_count": voiceprint_count,
    }


def meeting_payload(meeting: Meeting) -> dict[str, Any]:
    return {
        "id": meeting.id,
        "title": meeting.title,
        "date": meeting.date,
        "audio_path": meeting.audio_path,
        "created_at": meeting.created_at,
    }


def job_payload(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "meeting_id": job.meeting_id,
        "state": job.state.value,
        "error": job.error,
        "created_at": job.created_at,
    }


def meeting_detail(registry: Registry, meeting_id: int) -> dict[str, Any]:
    """The transcript view: segments with resolved names plus unknown clusters."""
    meeting = registry.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, f"meeting {meeting_id} does not exist")
    segments = registry.segments_for_meeting(meeting_id)
    names = {speaker.id: speaker.name for speaker in registry.list_speakers()}
    links = registry.meeting_speakers_for_meeting(meeting_id)
    links_by_cluster = {link.cluster_id: link for link in links}
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
        "speakers": sorted(
            {
                speaker
                for speaker in (item["speaker_name"] for item in payloads)
                if speaker
            }
        ),
        "segments": payloads,
        "unknown_clusters": unknown,
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
    "run_meeting",
    "save_upload",
    "speaker_payload",
]
