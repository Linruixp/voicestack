"""Word→speaker alignment for the speaker-service.

Merges an ASR transcript (:mod:`asr`: ``{language, segments:[{start, end,
text, words:[{word, start, end}]}]}``) with diarization turns (:mod:`diarize`:
``[{start, end, speaker}]``) into a transcript whose segments carry a
``speaker_id`` - the same alignment step VoiceStudio performs with
WhisperX + pyannote.

Alignment is purely temporal: every ASR span (each segment, and each word when
the ASR provides them) is assigned to the diarization turn with the **maximum
temporal overlap**. The text is never tokenized, so CJK transcripts - which
have no whitespace word boundaries - align exactly like English ones.

Tie-break (deterministic, input-order-independent up to exact duplicates):

1. greater temporal overlap wins;
2. on an exact overlap tie, the smaller temporal gap wins (zero for every
   overlapping turn);
3. then the turn that starts earlier wins;
4. then the turn that appears earlier in the input list wins.

When no turn overlaps the span at all, steps 2-4 pick the temporally nearest
turn, so no span is ever left unassigned. Empty diarization is a documented
degraded mode: every segment/word receives :data:`FALLBACK_SPEAKER` and the
result carries a warning - it never raises.

The segment's ``speaker_id`` follows the segment span; per-word ids refine it
for spans that straddle a speaker change (the segment keeps the dominant
speaker, which is what the pipeline reports).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from asr import Transcript
from diarize import SpeakerTurn

FALLBACK_SPEAKER = "SPEAKER_00"
NO_TURNS_WARNING = "diarization returned no turns; falling back to a single speaker"


@dataclass(frozen=True, slots=True)
class AlignedWord:
    """A recognized word plus the speaker it temporally belongs to."""

    word: str
    start: float
    end: float
    speaker_id: str


@dataclass(frozen=True, slots=True)
class AlignedSegment:
    """A transcription segment plus its dominant speaker."""

    start: float
    end: float
    text: str
    speaker_id: str
    words: tuple[AlignedWord, ...] = ()


@dataclass(frozen=True, slots=True)
class AlignedTranscript:
    """Aligned ASR result: per-segment/per-word ``speaker_id`` plus warnings."""

    language: str
    segments: tuple[AlignedSegment, ...] = ()
    speakers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire schema ``{language, speakers, warnings, segments}``."""
        return {
            "language": self.language,
            "speakers": list(self.speakers),
            "warnings": list(self.warnings),
            "segments": [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "speaker_id": segment.speaker_id,
                    "words": [
                        {
                            "word": word.word,
                            "start": word.start,
                            "end": word.end,
                            "speaker_id": word.speaker_id,
                        }
                        for word in segment.words
                    ],
                }
                for segment in self.segments
            ],
        }


def _overlap(start: float, end: float, turn: SpeakerTurn) -> float:
    """Return the seconds shared by ``[start, end]`` and ``turn`` (0 if disjoint)."""
    return max(0.0, min(end, turn["end"]) - max(start, turn["start"]))


def _gap(start: float, end: float, turn: SpeakerTurn) -> float:
    """Return the seconds between ``[start, end]`` and ``turn`` (0 if disjoint-free)."""
    return max(0.0, max(start, turn["start"]) - min(end, turn["end"]))


def _pick_turn(start: float, end: float, turns: Sequence[SpeakerTurn]) -> SpeakerTurn:
    """Pick the turn best matching ``[start, end]`` under the documented tie-break.

    Primary key is the overlap (maximized); ties fall back to the smaller gap,
    then the earlier start, then input order - so the winner never depends on
    the ordering of the callers' data.
    """

    def rank(indexed: tuple[int, SpeakerTurn]) -> tuple[float, float, float, int]:
        index, turn = indexed
        return (
            -_overlap(start, end, turn),
            _gap(start, end, turn),
            turn["start"],
            index,
        )

    return min(enumerate(turns), key=rank)[1]


def speaker_for_span(
    start: float, end: float, turns: Sequence[SpeakerTurn]
) -> str | None:
    """Return the speaker label for ``[start, end]``, or ``None`` without turns."""
    if not turns:
        return None
    return _pick_turn(start, end, turns)["speaker"]


def align_transcript(
    transcript: Transcript, turns: Sequence[SpeakerTurn]
) -> AlignedTranscript:
    """Assign every segment/word to the speaker with the maximum temporal overlap.

    Empty diarization degrades gracefully: every span becomes
    :data:`FALLBACK_SPEAKER` and :data:`NO_TURNS_WARNING` is returned instead of
    raising.
    """
    degraded = not turns
    warnings = (NO_TURNS_WARNING,) if degraded else ()

    segments: list[AlignedSegment] = []
    speakers: list[str] = []
    for segment in transcript.segments:
        if degraded:
            speaker_id = FALLBACK_SPEAKER
        else:
            speaker_id = _pick_turn(segment.start, segment.end, turns)["speaker"]

        words = tuple(
            AlignedWord(
                word=word.word,
                start=word.start,
                end=word.end,
                speaker_id=(
                    FALLBACK_SPEAKER
                    if degraded
                    else _pick_turn(word.start, word.end, turns)["speaker"]
                ),
            )
            for word in segment.words
        )

        if speaker_id not in speakers:
            speakers.append(speaker_id)
        segments.append(
            AlignedSegment(
                start=segment.start,
                end=segment.end,
                text=segment.text,
                speaker_id=speaker_id,
                words=words,
            )
        )

    return AlignedTranscript(
        language=transcript.language,
        segments=tuple(segments),
        speakers=tuple(speakers),
        warnings=warnings,
    )
