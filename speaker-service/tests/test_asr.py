"""Real-behavior tests for the mlx-whisper ASR module.

These run against the pre-fetched large-v3 model and the shared audio fixtures
(no mocks, no network). Transcription is expensive, so the two speech fixtures
are transcribed once per module via fixtures.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import asr

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "audio"
CJK = re.compile(r"[\u4e00-\u9fff]")


def _model_available() -> bool:
    try:
        return asr.model_snapshot_path().is_dir()
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _model_available(),
    reason="pinned whisper model snapshot not present",
)


@pytest.fixture(scope="module")
def en_raw() -> dict:
    return asr.transcribe_raw(FIXTURES / "en_30s.wav")


@pytest.fixture(scope="module")
def en_transcript(en_raw: dict) -> asr.Transcript:
    return asr.parse_transcript(en_raw)


@pytest.fixture(scope="module")
def zh_transcript() -> asr.Transcript:
    return asr.transcribe_file(FIXTURES / "zh_30s.wav")


def test_en_fixture_detects_english(en_transcript: asr.Transcript) -> None:
    assert en_transcript.language.startswith("en")
    assert en_transcript.text.strip() != ""


def test_en_fixture_words_have_valid_timestamps(en_transcript: asr.Transcript) -> None:
    assert en_transcript.segments, "expected at least one segment"
    words = [word for segment in en_transcript.segments for word in segment.words]
    assert words, "expected word-level timestamps"
    for word in words:
        assert word.word.strip() != ""
        assert word.start < word.end, f"bad span for {word.word!r}"


def test_en_words_stay_within_their_segment(en_transcript: asr.Transcript) -> None:
    for segment in en_transcript.segments:
        for word in segment.words:
            assert segment.start <= word.start <= word.end <= segment.end + 1e-6


def test_zh_fixture_yields_cjk_transcript(zh_transcript: asr.Transcript) -> None:
    assert CJK.search(zh_transcript.text) is not None
    assert any(CJK.search(segment.text) for segment in zh_transcript.segments)


def test_silence_returns_empty_transcript_without_error() -> None:
    transcript = asr.transcribe_file(FIXTURES / "silence_5s.wav")
    assert transcript.text == ""
    assert transcript.segments == ()


def test_persist_raw_transcript_writes_schema(en_raw: dict, tmp_path: Path) -> None:
    audio = FIXTURES / "en_30s.wav"
    saved = asr.persist_raw_transcript(en_raw, audio, transcript_dir=tmp_path)

    assert saved.is_file()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["audio"] == str(audio.resolve())
    assert payload["model"]["repo_id"] == asr.MODEL_REPO_ID
    assert payload["model"]["revision"] == asr.MODEL_REVISION
    assert payload["language"] == en_raw["language"]
    assert payload["raw"]["segments"] == en_raw["segments"]


def test_transcript_to_dict_matches_wire_schema(en_transcript: asr.Transcript) -> None:
    data = en_transcript.to_dict()
    assert data["language"].startswith("en")
    for segment in data["segments"]:
        assert set(segment) == {"start", "end", "text", "words"}
        for word in segment["words"]:
            assert set(word) == {"word", "start", "end"}
            assert word["start"] < word["end"]
