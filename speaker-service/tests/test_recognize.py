"""Cross-recording speaker recognition from ENROLLED voiceprints.

Given: one speaker enrolled through ``enrollment.enroll_speaker`` from recording
1 (a meeting persisted by the pipeline) plus a second, NEW recording by the same
speaker and a never-enrolled stranger. When: ``pipeline.transcribe_meeting``
runs on each new recording with the default calibrated threshold. Then: the
returning speaker's clusters resolve to the stored NAME with no manual step,
the stranger stays ``unknown`` (never a false name), recognition never
re-enrolls anyone, and the revision-drift guard does not misfire on voiceprints
this same embedder stored. The deterministic fake-stage tests pin the wiring on
every machine; the fixture tests exercise the real pinned models.
"""

from __future__ import annotations

import json
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

import api_service
import asr
import diarize
import enrollment
import pipeline
from embed import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION, IdentityEmbedder
from registry import ClusterState, JobState, Registry, open_registry

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "meetings"
ENROLLED_NAME = "Samantha"
CALIBRATED_THRESHOLD = 0.641
SAMPLE_RATE = 16_000


def _whisper_cached() -> bool:
    try:
        return asr.model_snapshot_path().is_dir()
    except FileNotFoundError:
        return False


requires_models = pytest.mark.skipif(
    not (diarize.model_is_cached() and _whisper_cached()),
    reason="pinned whisper + pyannote snapshots are not cached",
)


@dataclass(frozen=True, slots=True)
class FakeEmbedding:
    vector: np.ndarray
    model_id: str = MODEL_ID
    revision: str = MODEL_REVISION
    dim: int = EMBEDDING_DIM


class FakeEmbedder:
    """Positive audio maps to axis 1 ("Alice"); negative audio to axis 2."""

    def embed_waveform(
        self, waveform: np.ndarray, sample_rate: int = SAMPLE_RATE
    ) -> FakeEmbedding:
        vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        vector[1 if float(np.mean(waveform)) > 0 else 2] = 1.0
        return FakeEmbedding(vector=vector)

    def embed_cluster(self, members: list[FakeEmbedding]) -> FakeEmbedding:
        mean = np.mean(np.stack([member.vector for member in members]), axis=0)
        return FakeEmbedding(vector=(mean / np.linalg.norm(mean)).astype(np.float32))


def _fake_transcriber() -> Callable[[Path], asr.Transcript]:
    def transcribe(path: Path) -> asr.Transcript:
        return asr.Transcript(
            language="en",
            text="Hello there. Welcome back.",
            segments=(
                asr.Segment(start=0.0, end=1.0, text=" Hello there."),
                asr.Segment(start=1.0, end=2.0, text=" Welcome back."),
            ),
        )

    return transcribe


def _fake_diarizer() -> Callable[[Path], list[diarize.SpeakerTurn]]:
    def diarize_audio(path: Path) -> list[diarize.SpeakerTurn]:
        return [diarize.SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_00")]

    return diarize_audio


def _fake_config() -> pipeline.PipelineConfig:
    return pipeline.PipelineConfig(
        transcribe=_fake_transcriber(),
        diarize=_fake_diarizer(),
        embedder=FakeEmbedder(),
    )


def _write_wav(path: Path, frames: bytes) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames)
    return path


def _block(seconds: float, amplitude: float) -> bytes:
    value = int(amplitude * 32767)
    return value.to_bytes(2, "little", signed=True) * int(seconds * SAMPLE_RATE)


@dataclass(frozen=True, slots=True)
class FakeEnrollment:
    registry: Registry
    speaker_id: int
    first_meeting_id: int


@pytest.fixture
def fake_enrolled(tmp_path: Path) -> Iterator[FakeEnrollment]:
    """Enroll "Alice" from a first pipeline meeting via ``enroll_speaker``."""
    with open_registry(tmp_path / "registry.db") as registry:
        first = pipeline.transcribe_meeting(
            _write_wav(tmp_path / "rec1.wav", _block(2.0, 0.5)),
            "rec1",
            registry=registry,
            config=_fake_config(),
        )
        assert first.speakers == () and len(first.unknown_clusters) == 1
        enrolled = enrollment.enroll_speaker(
            registry,
            FakeEmbedder(),
            api_service.load_audio,
            name="Alice",
            meeting_id=first.meeting_id,
            cluster_id=first.unknown_clusters[0].cluster_id,
        )
        yield FakeEnrollment(registry, enrolled.speaker_id, first.meeting_id)


def test_returning_speaker_is_named_on_a_new_meeting(
    fake_enrolled: FakeEnrollment, tmp_path: Path
) -> None:
    # Given: "Alice" enrolled from rec1; When: rec2 (a NEW meeting) runs
    registry = fake_enrolled.registry
    result = pipeline.transcribe_meeting(
        _write_wav(tmp_path / "rec2.wav", _block(2.0, 0.5)),
        "rec2",
        registry=registry,
        config=_fake_config(),
    )

    # Then: every segment already carries the stored NAME - no manual step, and
    # nothing unknown is left for an operator to be prompted about
    assert result.meeting_id != fake_enrolled.first_meeting_id
    assert result.match_threshold == pytest.approx(CALIBRATED_THRESHOLD)
    assert {item.speaker_id for item in result.segments} == {"Alice"}
    assert result.speakers == ("Alice",)
    assert result.unknown_clusters == ()
    link = registry.meeting_speakers_for_meeting(result.meeting_id)[0]
    assert link.speaker_id == fake_enrolled.speaker_id
    assert link.confidence >= result.match_threshold
    # And: recognition never re-enrolls
    assert len(registry.voiceprints_for_speaker(fake_enrolled.speaker_id)) == 1
    assert [speaker.name for speaker in registry.list_speakers()] == ["Alice"]


def test_stranger_is_unknown_not_falsely_named(
    fake_enrolled: FakeEnrollment, tmp_path: Path
) -> None:
    # Given: "Alice" enrolled; When: a never-enrolled voice runs as a new meeting
    registry = fake_enrolled.registry
    result = pipeline.transcribe_meeting(
        _write_wav(tmp_path / "stranger.wav", _block(2.0, -0.5)),
        "stranger",
        registry=registry,
        config=_fake_config(),
    )

    # Then: the stranger is unknown - never a false name - and below threshold
    assert {item.speaker_id for item in result.segments} == {pipeline.UNKNOWN_SPEAKER}
    assert result.speakers == ()
    assert len(result.unknown_clusters) == 1
    unknown = result.unknown_clusters[0]
    assert unknown.status == "unknown"
    assert unknown.similarity is not None
    assert unknown.similarity < result.match_threshold
    # And: the unknown cluster persists with no label and no speaker link
    cluster = {
        item.id: item for item in registry.clusters_for_meeting(result.meeting_id)
    }[unknown.cluster_id]
    assert (cluster.state, cluster.label) == (ClusterState.UNKNOWN, None)
    # And: the stranger was never enrolled
    assert len(registry.voiceprints_for_speaker(fake_enrolled.speaker_id)) == 1
    assert [speaker.name for speaker in registry.list_speakers()] == ["Alice"]


@dataclass(frozen=True, slots=True)
class RealEnrollment:
    registry: Registry
    embedder: IdentityEmbedder
    speaker_id: int
    name: str
    first_meeting_id: int


@pytest.fixture(scope="module")
def real_enrolled(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RealEnrollment]:
    """Enroll Samantha from ``spkA_rec1.wav`` through the real models once."""
    base = tmp_path_factory.mktemp("recognize")
    registry = open_registry(base / "registry.db")
    embedder = IdentityEmbedder()
    try:
        first = pipeline.transcribe_meeting(
            FIXTURES / "spkA_rec1.wav",
            "rec1",
            registry=registry,
            config=pipeline.PipelineConfig(embedder=embedder),
        )
        assert first.speakers == () and len(first.unknown_clusters) == 1
        enrolled = enrollment.enroll_speaker(
            registry,
            embedder,
            api_service.load_audio,
            name=ENROLLED_NAME,
            meeting_id=first.meeting_id,
            cluster_id=first.unknown_clusters[0].cluster_id,
        )
        yield RealEnrollment(
            registry, embedder, enrolled.speaker_id, enrolled.name, first.meeting_id
        )
    finally:
        registry.close()


@requires_models
def test_fixture_recording_2_resolves_the_enrolled_name(
    real_enrolled: RealEnrollment,
) -> None:
    # Given: Samantha enrolled from recording 1; When: recording 2 runs as a NEW
    # meeting through the real ASR + diarization + identity models
    registry = real_enrolled.registry
    result = pipeline.transcribe_meeting(
        FIXTURES / "spkA_rec2.wav",
        "rec2",
        registry=registry,
        config=pipeline.PipelineConfig(embedder=real_enrolled.embedder),
    )

    # Then: every segment already carries her stored NAME - no manual step
    assert result.meeting_id != real_enrolled.first_meeting_id
    assert result.match_threshold == pytest.approx(CALIBRATED_THRESHOLD)
    assert result.segments
    assert {item.speaker_id for item in result.segments} == {ENROLLED_NAME}
    assert result.speakers == (ENROLLED_NAME,)
    assert result.unknown_clusters == ()
    link = registry.meeting_speakers_for_meeting(result.meeting_id)[0]
    assert link.speaker_id == real_enrolled.speaker_id
    assert link.confidence is not None and link.confidence >= result.match_threshold
    cluster = registry.clusters_for_meeting(result.meeting_id)[0]
    assert (cluster.state, cluster.label) == (ClusterState.ATTACHED, ENROLLED_NAME)
    # And: a correctly-enrolled print never triggers the drift guard or re-enroll
    assert len(registry.voiceprints_for_speaker(real_enrolled.speaker_id)) == 1
    assert [speaker.name for speaker in registry.list_speakers()] == [ENROLLED_NAME]
    assert registry.get_job(result.job_id).state is JobState.DONE
    print(
        json.dumps(
            {
                "meeting_id": result.meeting_id,
                "name": ENROLLED_NAME,
                "threshold": result.match_threshold,
                "similarity": link.confidence,
                "segments": len(result.segments),
                "unknown_clusters": 0,
            }
        )
    )


@requires_models
def test_fixture_stranger_stays_unknown_never_falsely_named(
    real_enrolled: RealEnrollment,
) -> None:
    # Given: Samantha enrolled; When: never-enrolled Fred's recording runs as a
    # NEW meeting
    registry = real_enrolled.registry
    result = pipeline.transcribe_meeting(
        FIXTURES / "spkB_rec1.wav",
        "stranger",
        registry=registry,
        config=pipeline.PipelineConfig(embedder=real_enrolled.embedder),
    )

    # Then: no segment gets a false name - the cluster is plainly unknown (the
    # revision-drift guard must NOT fire for prints stored by the same model)
    assert result.segments
    labels = {item.speaker_id for item in result.segments}
    assert labels == {pipeline.UNKNOWN_SPEAKER}
    assert ENROLLED_NAME not in labels
    assert result.speakers == ()
    assert result.unknown_clusters
    for unknown in result.unknown_clusters:
        assert unknown.status == "unknown"
        assert unknown.similarity is not None
        assert unknown.similarity < result.match_threshold
        cluster = {
            item.id: item for item in registry.clusters_for_meeting(result.meeting_id)
        }[unknown.cluster_id]
        assert (cluster.state, cluster.label) == (ClusterState.UNKNOWN, None)
    # And: the stranger was never enrolled
    assert len(registry.voiceprints_for_speaker(real_enrolled.speaker_id)) == 1
    assert [speaker.name for speaker in registry.list_speakers()] == [ENROLLED_NAME]
    print(
        json.dumps(
            {
                "meeting_id": result.meeting_id,
                "labels": sorted(labels),
                "similarity": result.unknown_clusters[0].similarity,
                "threshold": result.match_threshold,
            }
        )
    )
