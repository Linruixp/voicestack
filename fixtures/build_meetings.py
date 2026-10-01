#!/usr/bin/env python3
"""Build the LABELED meeting fixtures with macOS `say`.

Produces (fixtures/meetings/, 16 kHz mono 16-bit PCM):

  spkA_rec1.wav  spkA_rec2.wav  spkB_rec1.wav  spkB_rec2.wav
      Four single-speaker recordings. Speaker A appears in rec1 + rec2 and
      speaker B appears in rec1 + rec2, so each speaker has >= 2 recordings
      (needed for calibration in task 20 and returning-speaker recognition
      in task 24). Each file is built by concatenating that speaker's
      utterances and every utterance boundary is written to ground truth.

  ground_truth.json
      Per-file {speaker, voice, language, duration, transcript, segments}.

  meeting_20min.wav + meeting_20min.ground_truth.json
      A ~20 minute two-speaker conversation built by concatenating
      ALTERNATING utterances from both speakers; ground truth records every
      {speaker, start, end, text} segment exactly as constructed.

Because we construct the audio ourselves we know the ground truth exactly:
segment boundaries are derived from real sample offsets, so they match the
audio to the sample.
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (
    GAP,
    MEETINGS,
    SR,
    silence_frames,
    synth_wav,  # noqa: E402
    write_json,
    write_wav,
)

SPEAKER_VOICE = {"A": "Samantha", "B": "Fred"}
LANGUAGE = "en"

# --- utterance pools -------------------------------------------------------
# Each utterance is ~10-14 s of natural English, so the 20-minute meeting is
# assembled from a compact, deterministic pool (each utterance is synthesised
# once and reused).
A_LINES = [
    "Good morning everyone, thank you for joining today's planning session. "
    "Before we dive into the agenda, I want to review the progress we made "
    "last week and highlight a few open questions that still need answers.",
    "The first item on our list is the rollout schedule. We have three "
    "regional teams, and each of them needs a clear timeline, a named owner, "
    "and a rollback plan in case something goes wrong during deployment.",
    "I spoke with the finance group yesterday, and they confirmed the budget "
    "for the second quarter. That means we can move ahead with the hardware "
    "purchase without waiting for additional approvals.",
    "Let me summarize where we stand. We have finished the design review, we "
    "have a working prototype, and we are waiting on the security audit "
    "before we can ship the first release to customers.",
    "One thing I want to emphasize is documentation. If we do not write down "
    "our decisions, the next person who joins this project will spend weeks "
    "reconstructing what we already know, and that slows everyone down.",
    "Could you take a look at the incident report from Tuesday? I want to "
    "understand why the backup job failed, and whether we need to change the "
    "alerting thresholds so we catch that kind of problem earlier.",
]

B_LINES = [
    "Thanks, that is a helpful summary. From my side, the engineering team "
    "has been focused on stability. We fixed the two most common crashes, and "
    "we are now measuring how often they occur in the field.",
    "I have a concern about the timeline. If the audit does not start until "
    "next month, we will be testing and documenting at the same time, which "
    "historically has led to mistakes and rushed releases.",
    "On the budget question, I think we should keep a reserve. Hardware "
    "prices have been moving around, and I would rather have room to absorb a "
    "surprise than come back asking for more money later.",
    "Let me add one point about the customers. A few of our largest accounts "
    "have asked for an early preview, and I believe giving them access now "
    "would generate useful feedback before the official launch.",
    "I can own the rollout plan for the eastern region. I already have "
    "contacts there, and I understand the local constraints, so I can put "
    "together a draft by the end of the week.",
    "Before we close, I want to make sure we agree on the next steps. Who is "
    "responsible for the security audit, and what do you need from the rest "
    "of us to get it started?",
]

POOL = [
    (speaker, line)
    for a, b in zip(A_LINES, B_LINES)
    for speaker, line in (("A", a), ("B", b))
]


def build(out_path: Path, items: list[tuple[str, str]], cache: dict, tmp: Path) -> dict:
    """Concatenate (speaker, text) utterances with a fixed gap after each.

    Returns the ground-truth block for the produced file. Boundaries are
    derived from real sample counts so they match the audio exactly.
    """
    pieces: list[bytes] = []
    gap = silence_frames(GAP)
    cursor = 0  # samples
    segments: list[dict] = []
    for speaker, text in items:
        key = (SPEAKER_VOICE[speaker], text)
        if key not in cache:
            wav = tmp / f"u_{len(cache):02d}.wav"
            synth_wav(text, key[0], wav)
            with wave.open(str(wav), "rb") as w:
                frames = w.readframes(w.getnframes())
                dur = w.getnframes() / SR
            cache[key] = (frames, dur)
        frames, dur = cache[key]
        start = cursor / SR
        end = (cursor + len(frames) // 2) / SR
        segments.append(
            {
                "speaker": speaker,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
            }
        )
        pieces.append(frames)
        pieces.append(gap)
        cursor += len(frames) // 2 + len(gap) // 2

    write_wav(out_path, b"".join(pieces))
    duration = round(cursor / SR, 3)
    return {
        "file": out_path.name,
        "duration": duration,
        "segments": segments,
    }


def main() -> int:
    MEETINGS.mkdir(parents=True, exist_ok=True)
    cache: dict = {}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 2 speakers x 2 recordings — single speaker per file.
        single_specs = {
            "spkA_rec1.wav": [("A", A_LINES[0]), ("A", A_LINES[1]), ("A", A_LINES[2])],
            "spkA_rec2.wav": [("A", A_LINES[3]), ("A", A_LINES[4]), ("A", A_LINES[5])],
            "spkB_rec1.wav": [("B", B_LINES[0]), ("B", B_LINES[1]), ("B", B_LINES[2])],
            "spkB_rec2.wav": [("B", B_LINES[3]), ("B", B_LINES[4]), ("B", B_LINES[5])],
        }

        files_block: dict = {}
        for name, items in single_specs.items():
            block = build(MEETINGS / name, items, cache, tmp)
            speaker = items[0][0]
            block["speaker"] = speaker
            block["voice"] = SPEAKER_VOICE[speaker]
            block["language"] = LANGUAGE
            block["transcript"] = " ".join(t for _, t in items)
            # reorder keys for readability
            files_block[name] = {
                "speaker": block["speaker"],
                "voice": block["voice"],
                "language": block["language"],
                "duration": block["duration"],
                "transcript": block["transcript"],
                "segments": block["segments"],
            }

        ground_truth = {
            "schema": "voicestack.fixtures.meetings/v1",
            "description": "2-speaker x 2-recording labeled set; one speaker "
            "per file, each speaker present in rec1 and rec2.",
            "generator": "fixtures/build_meetings.py (macOS say + Python wave)",
            "sample_rate": SR,
            "channels": 1,
            "sample_format": "s16le",
            "gap_seconds": GAP,
            "speakers": {s: {"voice": v} for s, v in SPEAKER_VOICE.items()},
            "files": files_block,
        }
        write_json(MEETINGS / "ground_truth.json", ground_truth)

        # ~20 minute two-speaker meeting: alternating utterances, extended.
        target_s = 1200.0
        items: list[tuple[str, str]] = []
        approx = 0.0
        i = 0
        while approx < target_s:
            items.append(POOL[i % len(POOL)])
            approx = sum(cache[(SPEAKER_VOICE[s], t)][1] + GAP for s, t in items)
            i += 1
        m_block = build(MEETINGS / "meeting_20min.wav", items, cache, tmp)
        meeting_gt = {
            "schema": "voicestack.fixtures.meeting20/v1",
            "description": "~20 minute two-speaker meeting built by "
            "concatenating alternating utterances.",
            "generator": "fixtures/build_meetings.py (macOS say + Python wave)",
            "sample_rate": SR,
            "channels": 1,
            "sample_format": "s16le",
            "gap_seconds": GAP,
            "speakers": {s: {"voice": v} for s, v in SPEAKER_VOICE.items()},
            "duration": m_block["duration"],
            "turn_count": len(m_block["segments"]),
            "segments": m_block["segments"],
        }
        write_json(MEETINGS / "meeting_20min.ground_truth.json", meeting_gt)

    print(f"  {len(files_block)} single-speaker recordings + ground_truth.json")
    print(
        f"  meeting_20min.wav  {m_block['duration']:.1f}s  "
        f"({len(m_block['segments'])} turns)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
