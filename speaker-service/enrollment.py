"""Enrollment and transcript editing: new speaker, attach, merge, split.

The enrollment workflow turns an unknown diarized cluster into a named
identity. Two paths exist and must never be confused:

- :func:`enroll_speaker` creates a NEW speaker row and links the meeting;
- :func:`attach_voiceprint` adds the cluster's voiceprint to an EXISTING
  speaker, which is what keeps a returning, under-threshold voice from
  becoming a duplicate identity.

Both paths build the canonical voiceprint the same way: embed every cluster
segment at least ``pipeline.MIN_EMBED_SECONDS`` long and re-normalize the
mean of those members. A cluster with fewer than
``enrollment_models.MIN_VOICED_SECONDS`` of voiced audio is refused with
``ClusterTooShortError`` - a too-short cluster cannot produce a trustworthy
voiceprint. Neither path writes anything before the guard passes, so a
refused enrollment leaves no orphan speaker row and no voiceprint.

Merge folds a duplicate speaker into a target: voiceprints, meeting links
and segments are reassigned in ONE transaction, the moved clusters are
re-labelled with the target's name, and the duplicate row is deleted last.
Split moves the tail segments of a cluster into a fresh ``unknown`` cluster.

All SQL runs on the registry's own connection (the registry mix-ins cover
single-aggregate writes only); every mutation is a single transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pipeline
from enrollment_models import (
    MIN_VOICED_SECONDS,
    AttachedVoiceprint,
    ClusterNotFoundError,
    ClusterTooShortError,
    EnrolledSpeaker,
    InvalidSplitError,
    MeetingAudioUnavailableError,
    MergeOutcome,
    SelfMergeError,
    SplitOutcome,
)
from registry import (
    ClusterLink,
    ClusterState,
    Meeting,
    MeetingNotFoundError,
    Registry,
    Segment,
    SpeakerNotFoundError,
    VersionedVector,
)

if TYPE_CHECKING:
    from api_service import AudioLoader, Embedder


def cluster_voiceprint(
    embedder: Embedder,
    audio_loader: AudioLoader,
    audio_path: Path,
    segments: Sequence[Segment],
    *,
    cluster_id: int,
) -> VersionedVector:
    """Canonical voiceprint of one cluster, refusing too little voiced audio."""
    samples, sample_rate = audio_loader(audio_path)
    spans: list[tuple[int, int]] = []
    for segment in segments:
        start = max(0, round(segment.start * sample_rate))
        end = min(int(samples.shape[0]), round(segment.end * sample_rate))
        if end - start >= int(pipeline.MIN_EMBED_SECONDS * sample_rate):
            spans.append((start, end))
    voiced_seconds = sum(end - start for start, end in spans) / sample_rate
    if not spans or voiced_seconds < MIN_VOICED_SECONDS:
        raise ClusterTooShortError(cluster_id, voiced_seconds, MIN_VOICED_SECONDS)
    members = [
        embedder.embed_waveform(samples[start:end], sample_rate) for start, end in spans
    ]
    return embedder.embed_cluster(members)


def enroll_speaker(
    registry: Registry,
    embedder: Embedder,
    audio_loader: AudioLoader,
    *,
    name: str,
    meeting_id: int,
    cluster_id: int,
    organization: str | None = None,
    notes: str | None = None,
) -> EnrolledSpeaker:
    """Create a NEW speaker from a cluster; the voiceprint guard runs first."""
    meeting = _require_cluster(registry, meeting_id, cluster_id)
    segments = _segments_of(registry, meeting_id, cluster_id)
    voiceprint = cluster_voiceprint(
        embedder,
        audio_loader,
        _require_audio(meeting),
        segments,
        cluster_id=cluster_id,
    )
    speaker_id = registry.add_speaker(name, organization, notes)
    voiceprint_id = registry.add_voiceprint(speaker_id, voiceprint)
    speaker = registry.get_speaker(speaker_id)
    assert speaker is not None
    _apply_assignment(
        registry,
        meeting_id,
        cluster_id,
        speaker_id,
        ClusterState.NAMED,
        speaker.name,
        None,
    )
    return EnrolledSpeaker(speaker_id, voiceprint_id, cluster_id, speaker.name)


def attach_voiceprint(
    registry: Registry,
    embedder: Embedder,
    audio_loader: AudioLoader,
    *,
    speaker_id: int,
    meeting_id: int,
    cluster_id: int,
) -> AttachedVoiceprint:
    """Attach a cluster's voiceprint to an EXISTING speaker (never a new row)."""
    speaker = registry.get_speaker(speaker_id)
    if speaker is None:
        raise SpeakerNotFoundError(speaker_id)
    meeting = _require_cluster(registry, meeting_id, cluster_id)
    segments = _segments_of(registry, meeting_id, cluster_id)
    voiceprint = cluster_voiceprint(
        embedder,
        audio_loader,
        _require_audio(meeting),
        segments,
        cluster_id=cluster_id,
    )
    prior = registry.voiceprints_for_speaker(speaker_id)
    similarity: float | None = None
    if prior:
        from embed import cosine_similarity

        similarity = max(
            cosine_similarity(voiceprint.vector, item.vector) for item in prior
        )
    voiceprint_id = registry.add_voiceprint(speaker_id, voiceprint)
    _apply_assignment(
        registry,
        meeting_id,
        cluster_id,
        speaker_id,
        ClusterState.ATTACHED,
        speaker.name,
        similarity,
    )
    return AttachedVoiceprint(speaker_id, voiceprint_id, cluster_id, similarity)


def merge_speakers(
    registry: Registry, *, target_id: int, source_id: int
) -> MergeOutcome:
    """Fold ``source_id`` into ``target_id``: move every reference, delete source."""
    target = registry.get_speaker(target_id)
    if target is None:
        raise SpeakerNotFoundError(target_id)
    if registry.get_speaker(source_id) is None:
        raise SpeakerNotFoundError(source_id)
    if target_id == source_id:
        raise SelfMergeError(source_id)
    with registry._conn as conn:
        voiceprints = conn.execute(
            "UPDATE voiceprints SET speaker_id = ? WHERE speaker_id = ?",
            (target_id, source_id),
        ).rowcount
        links = conn.execute(
            "UPDATE meeting_speakers SET speaker_id = ? WHERE speaker_id = ?",
            (target_id, source_id),
        ).rowcount
        segments = conn.execute(
            "UPDATE segments SET speaker_id = ? WHERE speaker_id = ?",
            (target_id, source_id),
        ).rowcount
        conn.execute(
            "UPDATE clusters SET label = ? WHERE id IN"
            " (SELECT cluster_id FROM meeting_speakers WHERE speaker_id = ?)",
            (target.name, target_id),
        )
        conn.execute("DELETE FROM speakers WHERE id = ?", (source_id,))
    return MergeOutcome(target_id, source_id, voiceprints, links, segments)


def split_cluster(
    registry: Registry, *, meeting_id: int, cluster_id: int, at: float
) -> SplitOutcome:
    """Move the cluster's segments starting at/after ``at`` into a new cluster."""
    _require_cluster(registry, meeting_id, cluster_id)
    segments = _segments_of(registry, meeting_id, cluster_id)
    moved = [segment for segment in segments if segment.start >= at]
    if not moved or len(moved) == len(segments):
        raise InvalidSplitError
    new_cluster_id = registry.add_cluster(meeting_id, state=ClusterState.UNKNOWN)
    registry.add_meeting_speaker(
        ClusterLink(meeting_id=meeting_id, cluster_id=new_cluster_id)
    )
    placeholders = ", ".join("?" * len(moved))
    with registry._conn as conn:
        conn.execute(
            "UPDATE segments SET cluster_id = ?, speaker_id = NULL"
            f" WHERE id IN ({placeholders})",
            (new_cluster_id, *[segment.id for segment in moved]),
        )
    return SplitOutcome(cluster_id, new_cluster_id, len(moved))


def _require_cluster(registry: Registry, meeting_id: int, cluster_id: int) -> Meeting:
    meeting = registry.get_meeting(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    if all(
        cluster.id != cluster_id
        for cluster in registry.clusters_for_meeting(meeting_id)
    ):
        raise ClusterNotFoundError(meeting_id, cluster_id)
    return meeting


def _require_audio(meeting: Meeting) -> Path:
    if not meeting.audio_path or not Path(meeting.audio_path).is_file():
        raise MeetingAudioUnavailableError(meeting.id)
    return Path(meeting.audio_path)


def _segments_of(
    registry: Registry, meeting_id: int, cluster_id: int
) -> tuple[Segment, ...]:
    return tuple(
        segment
        for segment in registry.segments_for_meeting(meeting_id)
        if segment.cluster_id == cluster_id
    )


def _apply_assignment(
    registry: Registry,
    meeting_id: int,
    cluster_id: int,
    speaker_id: int,
    state: ClusterState,
    label: str,
    confidence: float | None,
) -> None:
    registry.update_cluster(cluster_id, state, label)
    registry.add_meeting_speaker(
        ClusterLink(
            meeting_id=meeting_id,
            cluster_id=cluster_id,
            speaker_id=speaker_id,
            confidence=confidence,
        )
    )
    with registry._conn as conn:
        conn.execute(
            "UPDATE segments SET speaker_id = ? WHERE cluster_id = ?",
            (speaker_id, cluster_id),
        )
