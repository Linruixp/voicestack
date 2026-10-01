"""Behavior tests for the durable speaker registry (SQLite + sqlite-vec).

Given: a registry on a private per-test database file.
When: speakers, versioned voiceprints, meetings, transcripts, clusters and
jobs are written, and the database is closed and reopened (a service restart).
Then: every row survives with stable ids, embeddings keep their model
provenance, the cosine index ranks by similarity, the file is owner-only, and
deleting a speaker applies the documented purge/null/reset policy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import sqlite_vec

from registry import (
    SCHEMA_VERSION,
    ClusterLink,
    ClusterState,
    JobState,
    NewSegment,
    open_registry,
)

DIM = 256
MODEL_ID = "pyannote/wespeaker-voxceleb-resnet34-LM"
REVISION = "837717ddb9ff5507820346191109dc79c958d614"


@dataclass(frozen=True, slots=True)
class FakeEmbedding:
    """Structural stand-in for ``embed.Embedding`` (no torch import in tests)."""

    vector: np.ndarray
    model_id: str = MODEL_ID
    revision: str = REVISION
    dim: int = DIM


def basis(index: int, weight: float = 1.0) -> np.ndarray:
    """A 256-d unit vector: ``weight`` on axis ``index``, the rest on axis 0."""
    vector = np.zeros(DIM, dtype=np.float32)
    vector[index] = weight
    if index != 0:
        vector[0] = float(np.sqrt(max(0.0, 1.0 - weight * weight)))
    return vector


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def test_open_initializes_required_schema_and_wal(db_path: Path) -> None:
    # Given: a fresh database path
    # When: the registry is opened
    with open_registry(db_path) as registry:
        assert registry.schema_version() == SCHEMA_VERSION
        # Then: every required table exists, including the sqlite-vec index
        raw = sqlite3.connect(db_path)
        names = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "speakers",
            "voiceprints",
            "voiceprints_vec",
            "meetings",
            "segments",
            "clusters",
            "meeting_speakers",
            "jobs",
            "schema_version",
        } <= names
        # And: WAL is the persistent journal mode
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        raw.close()


def test_speaker_and_versioned_voiceprint_survive_reopen(db_path: Path) -> None:
    # Given: a speaker and a 256-d voiceprint carrying model provenance
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker(
            "Alice", organization="Acme", notes="enrolled 2026-10"
        )
        voiceprint_id = registry.add_voiceprint(
            speaker_id, FakeEmbedding(vector=basis(1))
        )
    # When: the database is closed and reopened (service restart)
    with open_registry(db_path) as registry:
        speaker = registry.get_speaker(speaker_id)
        voiceprints = registry.voiceprints_for_speaker(speaker_id)
    # Then: the speaker row survives intact
    assert speaker is not None
    assert (speaker.id, speaker.name, speaker.organization, speaker.notes) == (
        speaker_id,
        "Alice",
        "Acme",
        "enrolled 2026-10",
    )
    assert speaker.created_at.endswith("Z")
    # And: the voiceprint survives with its vector and provenance
    assert len(voiceprints) == 1
    stored = voiceprints[0]
    assert stored.id == voiceprint_id
    assert (stored.model_id, stored.revision, stored.dim) == (
        MODEL_ID,
        REVISION,
        DIM,
    )
    np.testing.assert_allclose(stored.vector, basis(1), atol=1e-6)


def test_meeting_transcript_clusters_and_jobs_survive_reopen(db_path: Path) -> None:
    # Given: a processed meeting with named + unknown clusters and a job
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("Alice")
        meeting_id = registry.create_meeting(
            "Standup", date="2026-10-01", audio_path="/tmp/standup.wav"
        )
        named_cluster = registry.add_cluster(
            meeting_id, label="Alice", state=ClusterState.NAMED
        )
        unknown_cluster = registry.add_cluster(meeting_id)
        first = registry.add_segment(
            meeting_id,
            NewSegment(start=0.0, end=1.5, text="hello", cluster_id=named_cluster),
        )
        second = registry.add_segment(
            meeting_id,
            NewSegment(start=1.5, end=3.0, text="world", cluster_id=unknown_cluster),
        )
        registry.add_meeting_speaker(
            ClusterLink(
                meeting_id=meeting_id,
                cluster_id=named_cluster,
                speaker_id=speaker_id,
                confidence=0.91,
            )
        )
        registry.add_meeting_speaker(
            ClusterLink(meeting_id=meeting_id, cluster_id=unknown_cluster)
        )
        job_id = registry.create_job(meeting_id)
        registry.update_job(job_id, JobState.DONE)
    # When: the database is reopened
    with open_registry(db_path) as registry:
        meeting = registry.get_meeting(meeting_id)
        segments = registry.segments_for_meeting(meeting_id)
        clusters = {
            cluster.id: cluster for cluster in registry.clusters_for_meeting(meeting_id)
        }
        links = {
            link.cluster_id: link
            for link in registry.meeting_speakers_for_meeting(meeting_id)
        }
        job = registry.get_job(job_id)
    # Then: the meeting and transcript survive
    assert meeting is not None and meeting.audio_path == "/tmp/standup.wav"
    assert [(s.id, s.text, s.speaker_id, s.cluster_id) for s in segments] == [
        (first, "hello", None, named_cluster),
        (second, "world", None, unknown_cluster),
    ]
    # And: cluster ids are stable, with the unknown cluster still unknown
    assert clusters[named_cluster].state is ClusterState.NAMED
    assert clusters[unknown_cluster].state is ClusterState.UNKNOWN
    assert clusters[unknown_cluster].label is None
    # And: the meeting<-cluster<-speaker link survives with a NULL speaker for unknown
    assert links[named_cluster].speaker_id == speaker_id
    assert links[named_cluster].confidence == pytest.approx(0.91)
    assert links[unknown_cluster].speaker_id is None
    # And: the job survives in its final state
    assert job is not None and job.state is JobState.DONE


def test_database_file_and_wal_siblings_are_owner_only(db_path: Path) -> None:
    # Given: an open registry with pending WAL data
    with open_registry(db_path) as registry:
        registry.add_speaker("Alice")
        # When/Then: the database file mode is 700 (owner-only, never world-readable)
        assert (db_path.stat().st_mode & 0o777) == 0o700
        # And: the WAL sidecars created in its image are owner-only too
        for suffix in ("-wal", "-shm"):
            sibling = Path(f"{db_path}{suffix}")
            assert sibling.exists(), sibling
            assert (sibling.stat().st_mode & 0o777) == 0o700


def test_nearest_voiceprints_rank_by_cosine_similarity(db_path: Path) -> None:
    # Given: two voices ~orthogonal in the embedding space
    with open_registry(db_path) as registry:
        alice = registry.add_speaker("Alice")
        bob = registry.add_speaker("Bob")
        registry.add_voiceprint(alice, FakeEmbedding(vector=basis(1)))
        registry.add_voiceprint(bob, FakeEmbedding(vector=basis(2)))
        # When: a query vector near Alice's voice is searched
        matches = registry.nearest_voiceprints(FakeEmbedding(vector=basis(1)), k=2)
    # Then: Alice ranks first at cosine distance ~0, Bob last at ~1
    assert [match.speaker_id for match in matches] == [alice, bob]
    assert matches[0].distance == pytest.approx(0.0, abs=1e-5)
    assert matches[1].distance == pytest.approx(1.0, abs=1e-5)
    assert (matches[0].model_id, matches[0].revision, matches[0].dim) == (
        MODEL_ID,
        REVISION,
        DIM,
    )


def test_deleting_speaker_purges_voiceprints_and_vector_index(db_path: Path) -> None:
    # Given: a speaker with an indexed voiceprint
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("Alice")
        registry.add_voiceprint(speaker_id, FakeEmbedding(vector=basis(1)))
        # When: the speaker is deleted under the documented ON DELETE policy
        assert registry.delete_speaker(speaker_id) is True
        # Then: the voiceprint rows and their sqlite-vec index entries are gone
        raw = sqlite3.connect(db_path)
        raw.enable_load_extension(True)
        sqlite_vec.load(raw)
        raw.enable_load_extension(False)
        assert raw.execute("SELECT count(*) FROM voiceprints").fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM voiceprints_vec").fetchone()[0] == 0
        raw.close()
        assert registry.nearest_voiceprints(basis(1), k=5) == []


def test_deleting_speaker_nulls_meeting_speaker_and_segment_links(
    db_path: Path,
) -> None:
    # Given: a meeting whose cluster and segment point at the speaker
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("Alice")
        meeting_id = registry.create_meeting("Standup")
        cluster_id = registry.add_cluster(
            meeting_id, label="Alice", state=ClusterState.ATTACHED
        )
        registry.add_segment(
            meeting_id,
            NewSegment(
                start=0.0,
                end=1.0,
                text="hello",
                speaker_id=speaker_id,
                cluster_id=cluster_id,
            ),
        )
        registry.add_meeting_speaker(
            ClusterLink(
                meeting_id=meeting_id,
                cluster_id=cluster_id,
                speaker_id=speaker_id,
                confidence=0.9,
            )
        )
        # When: the speaker is deleted
        registry.delete_speaker(speaker_id)
        # Then: links are nulled, not cascaded away (history survives)
        links = registry.meeting_speakers_for_meeting(meeting_id)
        segments = registry.segments_for_meeting(meeting_id)
        assert [(link.speaker_id, link.cluster_id) for link in links] == [
            (None, cluster_id)
        ]
        assert [(segment.text, segment.speaker_id) for segment in segments] == [
            ("hello", None)
        ]
        assert registry.get_meeting(meeting_id) is not None


def test_deleting_speaker_resets_linked_clusters_to_unknown(db_path: Path) -> None:
    # Given: a named cluster attached to the speaker
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("Alice")
        meeting_id = registry.create_meeting("Standup")
        cluster_id = registry.add_cluster(
            meeting_id, label="Alice", state=ClusterState.ATTACHED
        )
        registry.add_meeting_speaker(
            ClusterLink(
                meeting_id=meeting_id, cluster_id=cluster_id, speaker_id=speaker_id
            )
        )
        # When: the speaker is deleted
        registry.delete_speaker(speaker_id)
        # Then: the cluster is back to unknown with its label cleared
        cluster = registry.clusters_for_meeting(meeting_id)[0]
        assert cluster.id == cluster_id
        assert cluster.state is ClusterState.UNKNOWN
        assert cluster.label is None
