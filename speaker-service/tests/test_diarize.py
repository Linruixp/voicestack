"""Real-behavior tests for pyannote diarization and the explicit device policy.

The 2-speaker clip is assembled at test time from the single-speaker fixtures
(no audio is committed). The happy path runs the real, pre-fetched
community-1 model offline. Device resolution is additionally tested with MPS
forced unavailable, proving there is no silent MPS assumption and no
fallback.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

import diarize

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "meetings"
GROUND_TRUTH = json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))
SAMPLE_RATE = GROUND_TRUTH["sample_rate"]
GAP_SECONDS = GROUND_TRUTH["gap_seconds"]
SEGMENTS_PER_SPEAKER = 3

requires_model = pytest.mark.skipif(
    not diarize.model_is_cached(),
    reason="pinned pyannote diarization snapshot is not cached",
)


def _read_segment(path: Path, start: float, end: float) -> bytes:
    with wave.open(str(path), "rb") as handle:
        handle.setpos(int(round(start * SAMPLE_RATE)))
        return handle.readframes(
            int(round(end * SAMPLE_RATE)) - int(round(start * SAMPLE_RATE))
        )


@pytest.fixture(scope="module")
def two_speaker_clip(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, list[dict]]:
    """Assemble alternating A/B turns; return the clip and its ground truth."""
    expected: list[dict] = []
    frames = bytearray()
    gap = b"\x00\x00" * int(GAP_SECONDS * SAMPLE_RATE)
    for index in range(SEGMENTS_PER_SPEAKER):
        for name in ("spkA_rec1.wav", "spkB_rec1.wav"):
            info = GROUND_TRUTH["files"][name]
            segment = info["segments"][index]
            start = len(frames) / 2 / SAMPLE_RATE
            frames += _read_segment(FIXTURES / name, segment["start"], segment["end"])
            end = len(frames) / 2 / SAMPLE_RATE
            frames += gap
            expected.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": info["speaker"],
                }
            )

    path = tmp_path_factory.mktemp("audio") / "two_speakers.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))
    return path, expected


@pytest.fixture(scope="module")
def turns(two_speaker_clip: tuple[Path, list[dict]]) -> list[diarize.SpeakerTurn]:
    path, _ = two_speaker_clip
    return diarize.diarize(path)


def test_resolved_device_is_pinned_and_reported() -> None:
    device = diarize.resolve_device()
    print(f"resolved diarization device: {device}")
    assert device == diarize.get_settings().diarize_device
    assert device in diarize.SUPPORTED_DEVICES


def test_mps_unavailable_raises_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diarize, "_mps_available", lambda: False)
    with pytest.raises(diarize.DeviceUnavailableError, match="cpu"):
        diarize.resolve_device("mps")
    assert diarize.resolve_device("cpu") == "cpu"


def test_unsupported_device_is_rejected() -> None:
    with pytest.raises(diarize.DeviceUnavailableError, match="VASTACK_DIARIZE_DEVICE"):
        diarize.resolve_device("cuda")


def test_missing_model_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diarize.load_pipeline.cache_clear()
    monkeypatch.setattr(diarize, "read_hf_token", lambda: "hf_test_token")

    def offline_miss(*args: object, **kwargs: object) -> None:
        raise LocalEntryNotFoundError("offline: pinned snapshot is not cached")

    monkeypatch.setattr(diarize, "_from_pretrained", offline_miss)
    with pytest.raises(diarize.ModelUnavailableError) as excinfo:
        diarize.load_pipeline("cpu")

    message = str(excinfo.value)
    assert diarize.MODEL_REPO_ID in message
    assert diarize.MODEL_REVISION in message
    assert "accept" in message.lower() and "license" in message.lower()
    assert "fetch_models" in message
    diarize.load_pipeline.cache_clear()


@requires_model
def test_two_speaker_clip_yields_at_least_two_turns(
    turns: list[diarize.SpeakerTurn],
) -> None:
    assert len(turns) >= 2, turns
    assert len({turn["speaker"] for turn in turns}) >= 2, turns
    for turn in turns:
        assert set(turn) == {"start", "end", "speaker"}
        assert turn["start"] < turn["end"], turn


@requires_model
def test_detected_clusters_match_ground_truth_speakers(
    turns: list[diarize.SpeakerTurn],
    two_speaker_clip: tuple[Path, list[dict]],
) -> None:
    _, expected = two_speaker_clip
    best_label: dict[str, str] = {}
    for speaker in sorted({item["speaker"] for item in expected}):
        speech = sum(
            item["end"] - item["start"]
            for item in expected
            if item["speaker"] == speaker
        )
        overlap: dict[str, float] = {}
        for item in (i for i in expected if i["speaker"] == speaker):
            for turn in turns:
                shared = min(item["end"], turn["end"]) - max(
                    item["start"], turn["start"]
                )
                if shared > 0:
                    overlap[turn["speaker"]] = (
                        overlap.get(turn["speaker"], 0.0) + shared
                    )
        assert overlap, f"no detected turn overlaps ground-truth speaker {speaker}"
        label = max(overlap, key=overlap.__getitem__)
        best_label[speaker] = label
        assert overlap[label] / speech >= 0.5, (speaker, label, overlap[label], speech)

    assert len(set(best_label.values())) == 2, best_label
