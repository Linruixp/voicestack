"""Enrollment, attach-to-existing, merge and split behavior.

Given: a durable registry holding a meeting whose cluster is unknown, plus
fake identity-embedding seams. When: a cluster is enrolled as a NEW speaker,
attached to an EXISTING one, two speakers are merged, or a cluster is split.
Then: enrollment creates exactly one speaker/voiceprint/link, attach never
duplicates the speaker, merge moves every reference to the target, split
divides the cluster, and a sub-second cluster is refused (domain + HTTP 400).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

import api_operations
import api_service
import enrollment
import pipeline
from api_service import ServiceDeps
from config import Settings
from registry import (
    ClusterLink,
    ClusterState,
    NewSegment,
    Registry,
    open_registry,
)


def _two_second_loader(path: Path) -> tuple[np.ndarray, int]:
    """Positive mono audio long enough to embed every standard fixture segment."""
    return np.full(32_000, 0.5, dtype=np.float32), 16_000


def _meeting_with_cluster(
    registry: Registry,
    tmp_path: Path,
    segments: tuple[tuple[float, float], ...] = ((0.0, 1.0), (1.0, 2.0)),
) -> tuple[int, int]:
    """Persist a meeting with one cluster of ``segments`` and a real audio path."""
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"RIFF-fake-meeting-audio")
    meeting_id = registry.create_meeting("Meeting", audio_path=str(audio))
    cluster_id = registry.add_cluster(meeting_id)
    registry.add_meeting_speaker(ClusterLink(meeting_id, cluster_id))
    for index, (start, end) in enumerate(segments):
        registry.add_segment(
            meeting_id, NewSegment(start, end, f"line {index}", None, cluster_id)
        )
    return meeting_id, cluster_id


def _unused_runner(
    path: Path, title: str, registry: Registry
) -> pipeline.MeetingResult:
    raise AssertionError("the attach adapter must not run the meeting pipeline")


def test_enroll_new_speaker_creates_speaker_voiceprint_and_meeting_link(
    db_path: Path, tmp_path: Path, embedder_class: type
) -> None:
    # Given: an unknown cluster with two voiced segments
    with open_registry(db_path) as registry:
        meeting_id, cluster_id = _meeting_with_cluster(registry, tmp_path)

        # When: it is enrolled as a new speaker with all optional fields
        result = enrollment.enroll_speaker(
            registry,
            embedder_class(),
            _two_second_loader,
            name="Ada",
            organization="ACME",
            notes="first",
            meeting_id=meeting_id,
            cluster_id=cluster_id,
        )

        # Then: exactly one speaker, one voiceprint and one meeting link exist
        speaker = registry.get_speaker(result.speaker_id)
        assert speaker is not None
        assert (speaker.name, speaker.organization, speaker.notes) == (
            "Ada",
            "ACME",
            "first",
        )
        voiceprints = registry.voiceprints_for_speaker(result.speaker_id)
        assert [item.id for item in voiceprints] == [result.voiceprint_id]
        links = registry.meeting_speakers_for_meeting(meeting_id)
        assert [link.speaker_id for link in links] == [result.speaker_id]
        assert links[0].confidence is None

        # And: the cluster is named and its segments point at the new speaker
        cluster = registry.clusters_for_meeting(meeting_id)[0]
        assert (cluster.state, cluster.label) == (ClusterState.NAMED, "Ada")
        segments = registry.segments_for_meeting(meeting_id)
        assert [segment.speaker_id for segment in segments] == [result.speaker_id] * 2

    # And: after a reopen, the meeting resolves every segment to the named speaker
    with open_registry(db_path) as reopened:
        detail = api_service.meeting_detail(reopened, meeting_id)
        assert {segment["speaker_name"] for segment in detail["segments"]} == {"Ada"}
        assert all(segment["speaker_id"] is not None for segment in detail["segments"])
        assert (detail["unknown_clusters"], detail["speakers"]) == ([], ["Ada"])


def test_attach_to_existing_adds_a_second_voiceprint_without_a_new_speaker(
    db_path: Path, tmp_path: Path, embedder_class: type
) -> None:
    # Given: one existing speaker and two meetings, each with an unknown cluster
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("Ada")
        first_meeting, first_cluster = _meeting_with_cluster(registry, tmp_path)
        second_meeting, second_cluster = _meeting_with_cluster(registry, tmp_path)

        # When: both clusters are attached to the SAME existing speaker
        first, second = (
            enrollment.attach_voiceprint(
                registry,
                embedder_class(),
                _two_second_loader,
                speaker_id=speaker_id,
                meeting_id=meeting,
                cluster_id=cluster,
            )
            for meeting, cluster in (
                (first_meeting, first_cluster),
                (second_meeting, second_cluster),
            )
        )

        # Then: the speaker row count is unchanged and it owns two voiceprints
        assert [item.name for item in registry.list_speakers()] == ["Ada"]
        assert first.similarity is None
        assert second.similarity == pytest.approx(1.0)
        assert len(registry.voiceprints_for_speaker(speaker_id)) == 2
        for meeting_id in (first_meeting, second_meeting):
            links = registry.meeting_speakers_for_meeting(meeting_id)
            assert [link.speaker_id for link in links] == [speaker_id]


def test_merge_moves_every_reference_to_the_target_and_drops_the_source(
    db_path: Path, tmp_path: Path, embedder_class: type
) -> None:
    # Given: a target and a duplicate whose voiceprint, link and segments exist
    with open_registry(db_path) as registry:
        target = registry.add_speaker("Ada")
        source = registry.add_speaker("Ada Duplicate")
        registry.add_consent(
            source, purpose="enrollment", retention_until="2027-01-01T00:00:00Z"
        )
        meeting_id, cluster_id = _meeting_with_cluster(registry, tmp_path)
        enrollment.attach_voiceprint(
            registry,
            embedder_class(),
            _two_second_loader,
            speaker_id=source,
            meeting_id=meeting_id,
            cluster_id=cluster_id,
        )

        # When: the duplicate is merged into the target
        result = enrollment.merge_speakers(registry, target_id=target, source_id=source)

        # Then: every reference moved and the duplicate row is gone
        assert (
            result.voiceprints_moved,
            result.links_moved,
            result.segments_moved,
        ) == (
            1,
            1,
            2,
        )
        assert registry.get_speaker(source) is None
        assert [c.speaker_id for c in registry.consents_for_speaker(target)] == [target]
        assert registry.consents_for_speaker(source) == []
        assert [
            item.speaker_id for item in registry.voiceprints_for_speaker(target)
        ] == [target]
        assert [
            link.speaker_id
            for link in registry.meeting_speakers_for_meeting(meeting_id)
        ] == [target]
        assert [
            segment.speaker_id for segment in registry.segments_for_meeting(meeting_id)
        ] == [target, target]

        # And: the meeting now resolves to the target in transcript and detail
        detail = api_service.meeting_detail(registry, meeting_id)
        assert {segment["speaker_name"] for segment in detail["segments"]} == {"Ada"}
        assert detail["speakers"] == ["Ada"]

        # And: merging a speaker into itself is refused
        with pytest.raises(enrollment.SelfMergeError):
            enrollment.merge_speakers(registry, target_id=target, source_id=target)


def test_split_divides_a_cluster_at_a_segment_boundary(
    db_path: Path, tmp_path: Path
) -> None:
    # Given: one cluster holding segments [0,1) and [1,2)
    with open_registry(db_path) as registry:
        meeting_id, cluster_id = _meeting_with_cluster(registry, tmp_path)

        # When: the tail is split into a new cluster
        result = enrollment.split_cluster(
            registry, meeting_id=meeting_id, cluster_id=cluster_id, at=1.0
        )

        # Then: each side owns one segment and the new cluster is unknown
        clusters = registry.clusters_for_meeting(meeting_id)
        assert [cluster.id for cluster in clusters] == [
            cluster_id,
            result.new_cluster_id,
        ]
        assert result.moved_segments == 1
        segments = registry.segments_for_meeting(meeting_id)
        assert (segments[0].cluster_id, segments[1].cluster_id) == (
            cluster_id,
            result.new_cluster_id,
        )
        assert segments[1].speaker_id is None
        links = {
            link.cluster_id: link
            for link in registry.meeting_speakers_for_meeting(meeting_id)
        }
        assert links[result.new_cluster_id].speaker_id is None

        # And: a split that would leave one side empty is refused
        with pytest.raises(enrollment.InvalidSplitError):
            enrollment.split_cluster(
                registry, meeting_id=meeting_id, cluster_id=cluster_id, at=5.0
            )


def test_cluster_shorter_than_one_voiced_second_is_refused(
    db_path: Path, tmp_path: Path, embedder_class: type
) -> None:
    # Given: a cluster whose only segment spans 0.3 s of voiced audio
    with open_registry(db_path) as registry:
        meeting_id, cluster_id = _meeting_with_cluster(
            registry, tmp_path, ((0.0, 0.3),)
        )

        # When/Then: enrolling is refused and leaves no orphan speaker row
        with pytest.raises(enrollment.ClusterTooShortError) as enrolling:
            enrollment.enroll_speaker(
                registry,
                embedder_class(),
                _two_second_loader,
                name="Ada",
                meeting_id=meeting_id,
                cluster_id=cluster_id,
            )
        assert enrolling.value.voiced_seconds == pytest.approx(0.3)
        assert registry.list_speakers() == []

        # And: attaching the same cluster to an existing speaker is refused too
        speaker_id = registry.add_speaker("Ada")
        with pytest.raises(enrollment.ClusterTooShortError):
            enrollment.attach_voiceprint(
                registry,
                embedder_class(),
                _two_second_loader,
                speaker_id=speaker_id,
                meeting_id=meeting_id,
                cluster_id=cluster_id,
            )
        assert registry.voiceprints_for_speaker(speaker_id) == []


def test_short_cluster_is_refused_through_the_http_adapter(
    db_path: Path, tmp_path: Path, embedder_class: type
) -> None:
    # Given: a 0.3 s cluster and the injectable service dependencies
    with open_registry(db_path) as registry:
        meeting_id, cluster_id = _meeting_with_cluster(
            registry, tmp_path, ((0.0, 0.3),)
        )
        speaker_id = registry.add_speaker("Ada")
        deps = ServiceDeps(
            runner=_unused_runner,
            embedder=embedder_class(),
            audio_loader=_two_second_loader,
            settings=Settings(data_dir=tmp_path, token=None),
        )

        # When: the route-level adapter is asked to attach it
        with pytest.raises(HTTPException) as refused:
            api_operations.attach_voiceprint(
                registry, deps, speaker_id, meeting_id, cluster_id
            )

        # Then: the domain guard surfaces as HTTP 400 with a clear message
        assert refused.value.status_code == 400
        assert "voiced audio" in str(refused.value.detail)


def test_enroll_speaker_stores_job_title(
    db_path: Path, tmp_path: Path, embedder_class: type
) -> None:
    # Given: an unknown cluster with voiced segments
    with open_registry(db_path) as registry:
        meeting_id, cluster_id = _meeting_with_cluster(registry, tmp_path)
        # When: it is enrolled with an organization and a job title
        result = enrollment.enroll_speaker(
            registry,
            embedder_class(),
            _two_second_loader,
            name="张三",
            organization="某某科技",
            title="产品总监",
            meeting_id=meeting_id,
            cluster_id=cluster_id,
        )
        # Then: both are persisted on the speaker
        speaker = registry.get_speaker(result.speaker_id)
        assert speaker is not None
        assert speaker.organization == "某某科技"
        assert speaker.title == "产品总监"
