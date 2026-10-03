"""End-to-end tests for the meeting transcription pipeline.

Given: a durable registry (with enrolled speakers) and meeting audio - either
synthetic, silent, or a two-speaker clip assembled from the labeled fixtures.
When: ``pipeline.transcribe_meeting`` runs ASR → diarization → alignment →
embedding → matching in one call.
Then: every segment carries a ``speaker_id`` (an enrolled NAME or ``unknown``),
a stranger comes back as a persisted unknown cluster with a provisional id and
NO name, silent audio yields an empty transcript without loading models, and
failures mark the job ``failed`` with the error.
"""

from __future__ import annotations

import json
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import numpy as np
import pytest

import asr
import diarize
import pipeline
from embed import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION, IdentityEmbedder
from registry import ClusterState, JobState, open_registry

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "meetings"
GROUND_TRUTH = json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))
SAMPLE_RATE = GROUND_TRUTH["sample_rate"]
GAP_SECONDS = GROUND_TRUTH["gap_seconds"]
ENROLLED_NAME = "Samantha"


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


def basis(index: int) -> np.ndarray:
    vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vector[index] = 1.0
    return vector


class FakeEmbedder:
    """Positive audio maps to axis 1 ("Alice"); negative audio to axis 2."""

    def embed_waveform(
        self, waveform: np.ndarray, sample_rate: int = 16_000
    ) -> FakeEmbedding:
        return FakeEmbedding(vector=basis(1 if float(np.mean(waveform)) > 0 else 2))

    def embed_cluster(self, members: list[FakeEmbedding]) -> FakeEmbedding:
        mean = np.mean(np.stack([member.vector for member in members]), axis=0)
        return FakeEmbedding(vector=mean / np.linalg.norm(mean))


def _fake_transcriber() -> Callable[[Path], asr.Transcript]:
    def transcribe(path: Path) -> asr.Transcript:
        return asr.Transcript(
            language="en",
            text="Good morning everyone. Thanks for joining.",
            segments=(
                asr.Segment(start=0.0, end=1.0, text=" Good morning everyone."),
                asr.Segment(start=1.0, end=2.0, text=" Thanks for joining."),
            ),
        )

    return transcribe


def _fake_diarizer() -> Callable[[Path], list[diarize.SpeakerTurn]]:
    def diarize_audio(path: Path) -> list[diarize.SpeakerTurn]:
        return [
            diarize.SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00"),
            diarize.SpeakerTurn(start=1.0, end=2.0, speaker="SPEAKER_01"),
        ]

    return diarize_audio


def _explode(path: Path) -> NoReturn:
    raise AssertionError(f"stage must not run: {path}")


def _explode_transcriber(path: Path) -> NoReturn:
    raise RuntimeError("asr exploded")


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


def _read_segment(path: Path, start: float, end: float) -> bytes:
    with wave.open(str(path), "rb") as handle:
        handle.setpos(int(round(start * SAMPLE_RATE)))
        return handle.readframes(
            int(round(end * SAMPLE_RATE)) - int(round(start * SAMPLE_RATE))
        )


def _build_meeting(directory: Path, name_a: str, name_b: str) -> Path:
    frames = bytearray()
    gap = b"\x00\x00" * int(GAP_SECONDS * SAMPLE_RATE)
    for index in range(3):
        for name in (name_a, name_b):
            segment = GROUND_TRUTH["files"][name]["segments"][index]
            frames += _read_segment(FIXTURES / name, segment["start"], segment["end"])
            frames += gap
    return _write_wav(directory / "two_speaker_meeting.wav", bytes(frames))


def test_known_and_unknown_segments_are_persisted(tmp_path: Path) -> None:
    # Given: Alice enrolled and a synthetic 2-speaker meeting (+1 s / -1 s blocks)
    audio = _write_wav(tmp_path / "meeting.wav", _block(1.0, 0.5) + _block(1.0, -0.5))
    with open_registry(tmp_path / "registry.db") as registry:
        alice = registry.add_speaker("Alice")
        registry.add_voiceprint(alice, FakeEmbedding(vector=basis(1)))

        # When: the full pipeline runs with deterministic fake stages
        result = pipeline.transcribe_meeting(
            audio,
            "Standup",
            registry=registry,
            config=pipeline.PipelineConfig(
                transcribe=_fake_transcriber(),
                diarize=_fake_diarizer(),
                embedder=FakeEmbedder(),
            ),
        )

        # Then: every segment is labelled - Alice by name, the stranger unknown
        assert [item.speaker_id for item in result.segments] == ["Alice", "unknown"]
        # And: the stranger is an unknown cluster with a provisional id, persisted
        # WITHOUT a name
        assert len(result.unknown_clusters) == 1
        unknown = result.unknown_clusters[0]
        assert unknown.cluster_id > 0 and unknown.label == "SPEAKER_01"
        clusters = {
            item.id: item for item in registry.clusters_for_meeting(result.meeting_id)
        }
        assert clusters[unknown.cluster_id].state is ClusterState.UNKNOWN
        assert clusters[unknown.cluster_id].label is None
        # And: the known cluster is attached to Alice and her name
        known_cluster = result.segments[0].cluster_id
        assert clusters[known_cluster].state is ClusterState.ATTACHED
        assert clusters[known_cluster].label == "Alice"
        links = {
            item.cluster_id: item
            for item in registry.meeting_speakers_for_meeting(result.meeting_id)
        }
        assert links[known_cluster].speaker_id == alice
        assert links[unknown.cluster_id].speaker_id is None
        # And: stored segments carry a speaker only where the speaker is known
        assert [
            (item.speaker_id, item.cluster_id)
            for item in registry.segments_for_meeting(result.meeting_id)
        ] == [(alice, known_cluster), (None, unknown.cluster_id)]
        # And: matching never enrolls (still one speaker, one voiceprint)
        assert [speaker.name for speaker in registry.list_speakers()] == ["Alice"]
        assert len(registry.voiceprints_for_speaker(alice)) == 1
        assert registry.get_job(result.job_id).state is JobState.DONE
        # And: the wire schema is JSON-serializable with the provisional id
        wire = json.loads(json.dumps(result.to_dict()))
        assert wire["segments"][0]["speaker_id"] == "Alice"
        assert wire["unknown_clusters"][0]["cluster_id"] == unknown.cluster_id


class _RecordingEmbedder(FakeEmbedder):
    """Counts ``embed_waveform`` calls so a guard can be proven, not assumed."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_waveform(
        self, waveform: np.ndarray, sample_rate: int = 16_000
    ) -> FakeEmbedding:
        self.calls += 1
        return super().embed_waveform(waveform, sample_rate)


def test_sub_window_segment_is_not_embedded_cluster_becomes_unknown(
    tmp_path: Path,
) -> None:
    # Given: a meeting whose only segment (0.1 s) is shorter than the identity
    # model's minimum viable window - the case that crashed matching on the
    # 20-minute fixture before MIN_EMBED_SECONDS was raised above 0.1 s
    audio = _write_wav(tmp_path / "short.wav", _block(0.2, 0.5))
    embedder = _RecordingEmbedder()

    def transcribe(path: Path) -> asr.Transcript:
        return asr.Transcript(
            language="en",
            text="hi",
            segments=(asr.Segment(start=0.0, end=0.1, text=" hi"),),
        )

    def diarize_audio(path: Path) -> list[diarize.SpeakerTurn]:
        return [diarize.SpeakerTurn(start=0.0, end=0.1, speaker="SPEAKER_00")]

    with open_registry(tmp_path / "registry.db") as registry:
        # When: the pipeline runs
        result = pipeline.transcribe_meeting(
            audio,
            "short",
            registry=registry,
            config=pipeline.PipelineConfig(
                transcribe=transcribe, diarize=diarize_audio, embedder=embedder
            ),
        )

    # Then: no embedding is attempted and the cluster is an unknown with a
    # bodiless reason instead of raising mid-meeting
    assert embedder.calls == 0
    assert [item.speaker_id for item in result.segments] == ["unknown"]
    assert len(result.unknown_clusters) == 1
    assert result.unknown_clusters[0].reason == "cluster has no embeddable segment"


def test_silent_audio_yields_empty_transcript_without_diarizing(
    tmp_path: Path,
) -> None:
    # Given: digital silence and a diarizer that must never be called
    audio = _write_wav(tmp_path / "silence.wav", b"\x00\x00" * SAMPLE_RATE)
    with open_registry(tmp_path / "registry.db") as registry:
        # When: the pipeline runs with the real ASR silence short-circuit
        result = pipeline.transcribe_meeting(
            audio,
            registry=registry,
            config=pipeline.PipelineConfig(diarize=_explode),
        )
        # Then: empty transcript + warning, and a done job with nothing persisted
        assert result.segments == () and result.unknown_clusters == ()
        assert result.warnings == (pipeline.NO_SPEECH_WARNING,)
        assert registry.clusters_for_meeting(result.meeting_id) == []
        assert registry.segments_for_meeting(result.meeting_id) == []
        assert registry.get_job(result.job_id).state is JobState.DONE


def test_handoff_batch_created_when_unknowns_meet_threshold(tmp_path: Path) -> None:
    # Given: a two-second meeting with unknown clusters and threshold 1
    audio = _write_wav(tmp_path / "meeting.wav", _block(1.0, 0.5) + _block(1.0, -0.5))
    with open_registry(tmp_path / "registry.db") as registry:
        result = pipeline.transcribe_meeting(
            audio,
            "Standup",
            registry=registry,
            config=pipeline.PipelineConfig(
                transcribe=_fake_transcriber(),
                diarize=_fake_diarizer(),
                embedder=FakeEmbedder(),
                handoff_threshold=1,
            ),
        )
        # Then: a batch is persisted and open for the meeting
        assert result.batch_id is not None
        batch = registry.get_speaker_batch(result.batch_id)
        assert batch is not None and batch.meeting_id == result.meeting_id
        assert registry.open_batch_for_meeting(result.meeting_id).id == result.batch_id


def test_no_batch_when_below_threshold(tmp_path: Path) -> None:
    # Given: the same meeting but a threshold above the unknown count
    audio = _write_wav(tmp_path / "meeting.wav", _block(1.0, 0.5) + _block(1.0, -0.5))
    with open_registry(tmp_path / "registry.db") as registry:
        result = pipeline.transcribe_meeting(
            audio,
            "Standup",
            registry=registry,
            config=pipeline.PipelineConfig(
                transcribe=_fake_transcriber(),
                diarize=_fake_diarizer(),
                embedder=FakeEmbedder(),
                handoff_threshold=99,
            ),
        )
        # Then: no batch is created
        assert result.batch_id is None


def test_transcribe_persists_meeting_date_and_duration(tmp_path: Path) -> None:
    # Given: a two-second synthetic meeting run with deterministic fake stages
    audio = _write_wav(tmp_path / "meeting.wav", _block(1.0, 0.5) + _block(1.0, -0.5))
    with open_registry(tmp_path / "registry.db") as registry:
        result = pipeline.transcribe_meeting(
            audio,
            "Standup",
            registry=registry,
            config=pipeline.PipelineConfig(
                transcribe=_fake_transcriber(),
                diarize=_fake_diarizer(),
                embedder=FakeEmbedder(),
            ),
        )
        meeting = registry.get_meeting(result.meeting_id)
        segments = registry.segments_for_meeting(result.meeting_id)
        # Then: the date comes from the file mtime and the duration is the last end
        assert meeting.date
        assert meeting.duration_s == pytest.approx(max(item.end for item in segments))
        assert meeting.original_title == "Standup"


def test_failure_marks_job_failed_and_reraises(tmp_path: Path) -> None:
    # Given: a transcriber that fails
    audio = _write_wav(tmp_path / "meeting.wav", b"\x00\x00" * SAMPLE_RATE)
    with open_registry(tmp_path / "registry.db") as registry:
        # When/Then: the pipeline surfaces the error
        with pytest.raises(RuntimeError, match="asr exploded"):
            pipeline.transcribe_meeting(
                audio,
                registry=registry,
                config=pipeline.PipelineConfig(transcribe=_explode_transcriber),
            )
        # And: the job records the failure; the meeting survives without segments
        job = registry.get_job(1)  # the only job in this registry
        assert job is not None and job.state is JobState.FAILED
        assert job.error == "RuntimeError: asr exploded"
        assert registry.segments_for_meeting(job.meeting_id) == []


def test_undecodable_audio_rolls_back_meeting_and_job(tmp_path: Path) -> None:
    # Given: a transcriber that reports the input is not decodable audio
    audio = _write_wav(tmp_path / "not_audio.wav", _block(1.0, 0.5))

    def transcribe(path: Path) -> asr.Transcript:
        raise asr.InvalidAudioError(f"{path} is not decodable audio")

    with open_registry(tmp_path / "registry.db") as registry:
        # When/Then: the typed pre-transcription failure propagates
        with pytest.raises(asr.InvalidAudioError):
            pipeline.transcribe_meeting(
                audio,
                registry=registry,
                config=pipeline.PipelineConfig(transcribe=transcribe),
            )
        # And: no orphan meeting or job remains (unlike a mid-pipeline failure)
        assert registry.list_meetings() == []
        assert registry.get_job(1) is None


def test_missing_audio_raises_before_any_registry_write(tmp_path: Path) -> None:
    with open_registry(tmp_path / "registry.db") as registry:
        with pytest.raises(FileNotFoundError):
            pipeline.transcribe_meeting(tmp_path / "nope.wav", registry=registry)
        assert registry.list_meetings() == []


@requires_models
def test_fixture_meeting_resolves_name_and_reports_unknown_cluster(
    tmp_path: Path,
) -> None:
    # Given: Samantha enrolled from recording 1, and a meeting built from her
    # unseen recording 2 plus never-enrolled Fred
    audio = _build_meeting(tmp_path, "spkA_rec2.wav", "spkB_rec2.wav")
    embedder = IdentityEmbedder()
    with open_registry(tmp_path / "registry.db") as registry:
        samantha = registry.add_speaker(ENROLLED_NAME)
        registry.add_voiceprint(
            samantha, embedder.embed_file(FIXTURES / "spkA_rec1.wav")
        )
        # When: the real pipeline runs (real ASR + diarization + embeddings)
        result = pipeline.transcribe_meeting(
            audio,
            "Planning sync",
            registry=registry,
            config=pipeline.PipelineConfig(embedder=embedder),
        )
        # Then: every segment is labelled, with the enrolled NAME and a stranger
        assert result.segments
        labels = {item.speaker_id for item in result.segments}
        assert all(label for label in labels)
        assert ENROLLED_NAME in labels
        assert pipeline.UNKNOWN_SPEAKER in labels
        assert len(result.unknown_clusters) >= 1
        # And: each unknown cluster has a provisional id and a diarization label
        # (never a name) and persists with state unknown and no label
        clusters = {
            item.id: item for item in registry.clusters_for_meeting(result.meeting_id)
        }
        for unknown in result.unknown_clusters:
            assert unknown.cluster_id > 0
            assert unknown.label.startswith("SPEAKER_")
            assert clusters[unknown.cluster_id].state is ClusterState.UNKNOWN
            assert clusters[unknown.cluster_id].label is None
        # And: nothing was auto-enrolled beyond the one fixture voiceprint
        assert len(registry.list_speakers()) == 1
        assert len(registry.voiceprints_for_speaker(samantha)) == 1
        assert registry.get_job(result.job_id).state is JobState.DONE
        # And: the wire schema, captured for the evidence record
        print(json.dumps(result.to_dict(), indent=2))
