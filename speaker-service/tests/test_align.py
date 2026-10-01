"""Behavior tests for temporal word→speaker alignment.

Rules pinned here: maximum temporal overlap picks the speaker; ties break by
smaller gap, earlier start, then input order; CJK text (no whitespace) aligns;
empty diarization degrades to one speaker + warning. Synthetic cases need no
models; integration cases run the real pinned ASR + diarization on the labeled
fixtures and require ≥90% duration-weighted assignment accuracy.
"""

from __future__ import annotations

import json
import re
import wave
from pathlib import Path

import pytest

import align
import asr
import diarize

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
MEETINGS = FIXTURES / "meetings"
AUDIO = FIXTURES / "audio"
GROUND_TRUTH = json.loads((MEETINGS / "ground_truth.json").read_text(encoding="utf-8"))
SAMPLE_RATE = GROUND_TRUTH["sample_rate"]
GAP_SECONDS = GROUND_TRUTH["gap_seconds"]
SEGMENTS_PER_SPEAKER = 3
CJK = re.compile(r"[\u4e00-\u9fff]")


def _whisper_available() -> bool:
    try:
        return asr.model_snapshot_path().is_dir()
    except FileNotFoundError:
        return False


requires_models = pytest.mark.skipif(
    not (diarize.model_is_cached() and _whisper_available()),
    reason="pinned whisper + pyannote snapshots are not cached",
)

AlignedMeeting = tuple[align.AlignedTranscript, list[diarize.SpeakerTurn], list[dict]]


def _turn(start: float, end: float, speaker: str) -> diarize.SpeakerTurn:
    return diarize.SpeakerTurn(start=start, end=end, speaker=speaker)


def _transcript(
    segments: list[tuple[float, float, str, list[tuple[str, float, float]]]],
    *,
    language: str = "en",
) -> asr.Transcript:
    return asr.Transcript(
        language=language,
        text=" ".join(item[2] for item in segments),
        segments=tuple(
            asr.Segment(
                start=item[0],
                end=item[1],
                text=item[2],
                words=tuple(
                    asr.Word(word=word[0], start=word[1], end=word[2])
                    for word in item[3]
                ),
            )
            for item in segments
        ),
    )


# --- overlap rule (synthetic, no models) -----------------------------------


@pytest.mark.parametrize(
    ("turns", "span", "expected"),
    [
        # maximum overlap wins: 3.0 s vs 1.0 s
        ([_turn(0.0, 10.0, "A"), _turn(10.0, 20.0, "B")], (9.0, 13.0), "B"),
        # exact 50/50 overlap tie -> earliest turn
        ([_turn(0.0, 1.0, "A"), _turn(1.0, 2.0, "B")], (0.5, 1.5), "A"),
        # no overlap at all -> nearest turn by gap (0.9 s vs 2.9 s)
        ([_turn(0.0, 1.0, "A"), _turn(5.0, 6.0, "B")], (3.9, 4.1), "B"),
    ],
    ids=["max-overlap", "exact-tie-earliest", "gap-nearest"],
)
def test_overlap_rule_and_tie_breaks(turns, span, expected) -> None:
    aligned = align.align_transcript(
        _transcript([(span[0], span[1], "text", [])]), turns
    )
    assert aligned.segments[0].speaker_id == expected


def test_words_carry_their_own_speaker_ids_across_a_change() -> None:
    # Given: one ASR segment with words on both sides of a speaker change
    turns = [_turn(0.0, 10.0, "SPEAKER_00"), _turn(10.0, 20.0, "SPEAKER_01")]
    transcript = _transcript(
        [(9.0, 12.0, "switch now", [("switch", 9.0, 10.0), ("now", 10.5, 12.0)])]
    )

    # When: aligned
    aligned = align.align_transcript(transcript, turns)

    # Then: the segment follows the dominant span, each word its own span
    assert aligned.segments[0].speaker_id == "SPEAKER_01"
    assert [word.speaker_id for word in aligned.segments[0].words] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]


def test_cjk_words_align_without_whitespace() -> None:
    # Given: a Chinese transcript with one timestamped token per character
    turns = [_turn(0.0, 4.0, "SPEAKER_00"), _turn(4.0, 8.0, "SPEAKER_01")]
    transcript = _transcript(
        [
            (
                3.2,
                5.0,
                "你好世界",
                [
                    ("你", 3.2, 3.6),
                    ("好", 3.6, 4.0),
                    ("世", 4.0, 4.5),
                    ("界", 4.5, 5.0),
                ],
            )
        ],
        language="zh",
    )

    # When: aligned (no whitespace tokenization anywhere)
    aligned = align.align_transcript(transcript, turns)

    # Then: both the segment and every character land on the right speaker
    assert aligned.segments[0].speaker_id == "SPEAKER_01"
    assert [word.speaker_id for word in aligned.segments[0].words] == [
        "SPEAKER_00",
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_01",
    ]


def test_cjk_segment_without_word_timestamps_still_aligns() -> None:
    # Given: a Chinese segment with no word-level detail at all
    turns = [_turn(0.0, 5.0, "SPEAKER_00"), _turn(5.0, 8.0, "SPEAKER_01")]
    transcript = _transcript([(0.0, 8.0, "这是一个没有空格的句子", [])], language="zh")

    # When: aligned
    aligned = align.align_transcript(transcript, turns)

    # Then: the segment span alone decides (5.0 s vs 3.0 s of overlap)
    assert aligned.segments[0].speaker_id == "SPEAKER_00"
    assert aligned.segments[0].words == ()


# --- degraded inputs (synthetic, no models) --------------------------------


def test_empty_diarization_falls_back_to_single_speaker_with_warning() -> None:
    # Given: diarization that returned nothing
    transcript = _transcript(
        [(0.0, 2.0, "hello", [("hello", 0.0, 2.0)])], language="en"
    )

    # When: aligned
    aligned = align.align_transcript(transcript, [])

    # Then: a single-speaker result plus a warning, never an exception
    assert aligned.warnings, "expected a fallback warning"
    assert any("diari" in warning.lower() for warning in aligned.warnings)
    assert aligned.segments[0].speaker_id == align.FALLBACK_SPEAKER
    assert aligned.segments[0].words[0].speaker_id == align.FALLBACK_SPEAKER
    assert aligned.speakers == (align.FALLBACK_SPEAKER,)


def test_empty_transcript_is_returned_without_error() -> None:
    # Given: a transcript with no segments
    empty = asr.Transcript(language="", text="", segments=())

    # When: aligned against real turns
    aligned = align.align_transcript(empty, [_turn(0.0, 1.0, "SPEAKER_00")])

    # Then: it stays empty, with no crash and no invented speakers
    assert aligned.segments == ()
    assert aligned.speakers == ()


def test_to_dict_emits_speaker_ids() -> None:
    # Given: an aligned transcript
    turns = [_turn(0.0, 1.0, "SPEAKER_00")]
    transcript = _transcript([(0.0, 1.0, "hi", [("hi", 0.0, 1.0)])])
    aligned = align.align_transcript(transcript, turns)

    # When: serialized
    data = aligned.to_dict()

    # Then: the wire schema carries speaker_id at segment and word level
    assert data["language"] == "en"
    assert data["speakers"] == ["SPEAKER_00"]
    for segment in data["segments"]:
        assert set(segment) == {"start", "end", "text", "speaker_id", "words"}
        assert segment["speaker_id"] == "SPEAKER_00"
        for word in segment["words"]:
            assert set(word) == {"word", "start", "end", "speaker_id"}


# --- real models on the labeled fixtures -----------------------------------


def _read_segment(path: Path, start: float, end: float) -> bytes:
    with wave.open(str(path), "rb") as handle:
        handle.setpos(int(round(start * SAMPLE_RATE)))
        return handle.readframes(
            int(round(end * SAMPLE_RATE)) - int(round(start * SAMPLE_RATE))
        )


@pytest.fixture(scope="module")
def two_speaker_meeting(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, list[dict]]:
    """Assemble alternating A/B utterances; return the clip and its ground truth."""
    expected: list[dict] = []
    frames = bytearray()
    gap = b"\x00\x00" * int(GAP_SECONDS * SAMPLE_RATE)
    for index in range(SEGMENTS_PER_SPEAKER):
        for name in ("spkA_rec1.wav", "spkB_rec1.wav"):
            info = GROUND_TRUTH["files"][name]
            segment = info["segments"][index]
            start = len(frames) / 2 / SAMPLE_RATE
            frames += _read_segment(MEETINGS / name, segment["start"], segment["end"])
            end = len(frames) / 2 / SAMPLE_RATE
            frames += gap
            expected.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": info["speaker"],
                }
            )

    path = tmp_path_factory.mktemp("align") / "two_speakers.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))
    return path, expected


@pytest.fixture(scope="module")
def aligned_meeting(two_speaker_meeting: tuple[Path, list[dict]]) -> AlignedMeeting:
    path, expected = two_speaker_meeting
    transcript = asr.transcribe_file(path)
    turns = diarize.diarize(path)
    return align.align_transcript(transcript, turns), turns, expected


def _speaker_mapping(
    expected: list[dict], turns: list[diarize.SpeakerTurn]
) -> dict[str, str]:
    """Map each ground-truth speaker to the diarization label it overlaps most."""
    overlap: dict[str, dict[str, float]] = {}
    for item in expected:
        for turn in turns:
            shared = min(item["end"], turn["end"]) - max(item["start"], turn["start"])
            if shared > 0:
                labels = overlap.setdefault(item["speaker"], {})
                labels[turn["speaker"]] = labels.get(turn["speaker"], 0.0) + shared
    return {spk: max(labels, key=labels.__getitem__) for spk, labels in overlap.items()}


@requires_models
def test_meeting_spans_all_carry_speakers(aligned_meeting: AlignedMeeting) -> None:
    aligned, turns, _ = aligned_meeting
    assert len(turns) >= 2, turns
    assert aligned.segments, "expected transcribed segments"
    assert aligned.warnings == ()
    for segment in aligned.segments:
        assert segment.speaker_id, segment
        for word in segment.words:
            assert word.speaker_id, word
    assert len(aligned.speakers) == 2


@requires_models
def test_two_speaker_accuracy_at_least_90pct(aligned_meeting: AlignedMeeting) -> None:
    aligned, turns, expected = aligned_meeting
    mapping = _speaker_mapping(expected, turns)
    assert len(set(mapping.values())) == 2, mapping

    total = 0.0
    correct = 0.0
    for segment in aligned.segments:
        for word in segment.words:
            best: dict | None = None
            best_overlap = 0.0
            for item in expected:
                shared = min(word.end, item["end"]) - max(word.start, item["start"])
                if shared > best_overlap:
                    best_overlap, best = shared, item
            duration = max(word.end - word.start, 0.0)
            if best is None or duration <= 0:
                continue
            total += duration
            if word.speaker_id == mapping[best["speaker"]]:
                correct += duration

    accuracy = correct / total
    print(f"ALIGNMENT_ACCURACY={accuracy:.4f} ({correct:.2f}s / {total:.2f}s)")
    assert total > 0
    assert accuracy >= 0.90, f"assignment accuracy {accuracy:.3f} below 0.90"


@requires_models
def test_zh_fixture_aligns_end_to_end() -> None:
    # Given: the Chinese fixture through the real ASR + diarization stack
    transcript = asr.transcribe_file(AUDIO / "zh_30s.wav")
    assert CJK.search(transcript.text), transcript.text
    turns = diarize.diarize(AUDIO / "zh_30s.wav")

    # When: aligned
    aligned = align.align_transcript(transcript, turns)

    # Then: CJK text is preserved and every segment has a speaker
    assert aligned.segments, "expected transcribed segments"
    for segment in aligned.segments:
        assert CJK.search(segment.text), segment.text
        assert segment.speaker_id
    assert aligned.speakers
