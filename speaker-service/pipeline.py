"""End-to-end meeting transcription: ASR → diarization → alignment → identity.

:func:`transcribe_meeting` runs asr → diarize → align → embed → match over one
recording and persists meeting, job, clusters and segments via :mod:`registry`.
Every returned segment carries a ``speaker_id``: the matched speaker's NAME or
``"unknown"``. A cluster matching nobody is persisted with ``state='unknown'``
and NO name and returned in ``unknown_clusters`` with its provisional registry
``cluster_id`` - naming it is the enrollment workflow's job. Matched clusters
become ``state='attached'``. Jobs walk ``queued → running → done|failed``
(failure records its error and re-raises). No voiceprint is written here:
matching never enrolls or re-enrolls anyone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

import align
import asr
import diarize
from config import get_settings
from registry import (
    ClusterLink,
    ClusterState,
    JobState,
    NewSegment,
    Registry,
    open_registry,
)
from registry_models import VersionedVector

if TYPE_CHECKING:
    from match import MatchDecision

UNKNOWN_SPEAKER = "unknown"
NO_SPEECH_WARNING = "no speech detected; transcript is empty"
# Segments shorter than this are not embedded (a cluster with none is unknown).
# WeSpeaker ResNet34 returns a non-finite embedding when the slice is too short
# for its pooling window: measured, a 0.1 s slice (1600 samples @16k) is NaN
# while ~0.105 s+ is finite; 0.2 s keeps margin so matching cannot crash.
MIN_EMBED_SECONDS = 0.2


class ClusterEmbedder(Protocol):
    def embed_waveform(
        self, waveform: np.ndarray, sample_rate: int = 16_000
    ) -> VersionedVector: ...

    def embed_cluster(self, members: Sequence[VersionedVector]) -> VersionedVector: ...


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Per-run overrides; ``None`` selects the real pinned implementation."""

    transcribe: Callable[[Path], asr.Transcript] | None = None
    diarize: Callable[[Path], list[diarize.SpeakerTurn]] | None = None
    embedder: ClusterEmbedder | None = None
    match_threshold: float | None = None


@dataclass(frozen=True, slots=True)
class SegmentResult:
    """One persisted transcript segment with its resolved speaker."""

    id: int
    start: float
    end: float
    text: str
    speaker_id: str
    cluster_id: int
    similarity: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class UnknownCluster:
    cluster_id: int
    label: str
    status: str
    reason: str
    segment_count: int
    start: float
    end: float
    similarity: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MeetingResult:
    meeting_id: int
    job_id: int
    title: str
    audio_path: str
    language: str
    segments: tuple[SegmentResult, ...] = ()
    unknown_clusters: tuple[UnknownCluster, ...] = ()
    speakers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    match_threshold: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Job:
    registry: Registry
    meeting_id: int
    job_id: int
    title: str


def _resolve_threshold(override: float | None) -> float:
    value = float(get_settings().match_threshold if override is None else override)
    if not 0.0 < value < 1.0:
        raise ValueError(f"match threshold must be strictly between 0 and 1: {value}")
    return value


def _load_audio(path: Path) -> tuple[np.ndarray, int]:
    import torchaudio

    waveform, sample_rate = torchaudio.load(str(path))
    return waveform.mean(dim=0).numpy(), int(sample_rate)


def _voiceprint_for(
    embedder: ClusterEmbedder,
    audio: tuple[np.ndarray, int],
    segments: Sequence[align.AlignedSegment],
) -> VersionedVector | None:
    samples, sample_rate = audio
    members: list[VersionedVector] = []
    for segment in segments:
        start = max(0, round(segment.start * sample_rate))
        end = min(len(samples), round(segment.end * sample_rate))
        if end - start >= int(MIN_EMBED_SECONDS * sample_rate):
            members.append(embedder.embed_waveform(samples[start:end], sample_rate))
    return embedder.embed_cluster(members) if members else None


def _run_meeting(job: _Job, path: Path, config: PipelineConfig) -> MeetingResult:
    transcript = (config.transcribe or asr.transcribe_file)(path)
    threshold = _resolve_threshold(config.match_threshold)
    if not transcript.segments:
        return MeetingResult(
            meeting_id=job.meeting_id,
            job_id=job.job_id,
            title=job.title,
            audio_path=str(path),
            language=transcript.language,
            warnings=(NO_SPEECH_WARNING,),
            match_threshold=threshold,
        )

    diarize_audio = config.diarize or diarize.diarize
    aligned = align.align_transcript(transcript, diarize_audio(path))
    audio = _load_audio(path)
    from embed import IdentityEmbedder
    from match import MatchStatus, match_speaker

    embedder = config.embedder or IdentityEmbedder()

    grouped: dict[str, list[align.AlignedSegment]] = {}
    for segment in aligned.segments:
        grouped.setdefault(segment.speaker_id, []).append(segment)

    segments: list[SegmentResult] = []
    unknown_clusters: list[UnknownCluster] = []
    names: list[str] = []
    for label, cluster_segments in grouped.items():
        voiceprint = _voiceprint_for(embedder, audio, cluster_segments)
        decision: MatchDecision | None = (
            None
            if voiceprint is None
            else match_speaker(job.registry, voiceprint, threshold=threshold)
        )
        known = decision is not None and decision.status is MatchStatus.KNOWN
        name = decision.name if known else None
        speaker_row = decision.speaker_id if known else None
        similarity = decision.similarity if decision is not None else None
        cluster_id = job.registry.add_cluster(
            job.meeting_id,
            label=name,
            state=ClusterState.ATTACHED if known else ClusterState.UNKNOWN,
        )
        job.registry.add_meeting_speaker(
            ClusterLink(
                meeting_id=job.meeting_id,
                cluster_id=cluster_id,
                speaker_id=speaker_row,
                confidence=similarity if known else None,
            )
        )
        for segment in cluster_segments:
            segment_id = job.registry.add_segment(
                job.meeting_id,
                NewSegment(
                    start=segment.start,
                    end=segment.end,
                    text=segment.text,
                    speaker_id=speaker_row,
                    cluster_id=cluster_id,
                ),
            )
            segments.append(
                SegmentResult(
                    id=segment_id,
                    start=segment.start,
                    end=segment.end,
                    text=segment.text,
                    speaker_id=name or UNKNOWN_SPEAKER,
                    cluster_id=cluster_id,
                    similarity=similarity,
                )
            )
        if known:
            names.append(name)
            continue
        status = "unknown" if decision is None else decision.status.value
        reason = (
            "cluster has no embeddable segment" if decision is None else decision.reason
        )
        unknown_clusters.append(
            UnknownCluster(
                cluster_id=cluster_id,
                label=label,
                status=status,
                reason=reason,
                segment_count=len(cluster_segments),
                start=min(item.start for item in cluster_segments),
                end=max(item.end for item in cluster_segments),
                similarity=similarity,
            )
        )

    segments.sort(key=lambda item: (item.start, item.end))
    return MeetingResult(
        meeting_id=job.meeting_id,
        job_id=job.job_id,
        title=job.title,
        audio_path=str(path),
        language=aligned.language,
        segments=tuple(segments),
        unknown_clusters=tuple(unknown_clusters),
        speakers=tuple(dict.fromkeys(names)),
        warnings=aligned.warnings,
        match_threshold=threshold,
    )


def transcribe_meeting(
    audio_path: str | Path,
    meeting_title: str | None = None,
    *,
    registry: Registry | None = None,
    config: PipelineConfig | None = None,
) -> MeetingResult:
    """Transcribe, diarize, identify and persist one meeting recording.

    ``registry`` defaults to the app-data registry (opened/closed here);
    ``config`` injects stage fakes in tests. A failure marks the job ``failed``
    with its error and re-raises.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(path)

    overrides = config or PipelineConfig()
    store = registry if registry is not None else open_registry()
    try:
        title = meeting_title or path.stem
        meeting_id = store.create_meeting(title, audio_path=str(path))
        job_id = store.create_job(meeting_id, JobState.QUEUED)
        store.update_job(job_id, JobState.RUNNING)
        job = _Job(store, meeting_id, job_id, title)
        try:
            result = _run_meeting(job, path, overrides)
        except Exception as exc:
            store.update_job(job_id, JobState.FAILED, f"{type(exc).__name__}: {exc}")
            raise
        store.update_job(job_id, JobState.DONE)
        return result
    finally:
        if registry is None:
            store.close()
