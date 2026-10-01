#!/usr/bin/env python3
"""Shared helpers for the VoiceStack fixture generators.

All audio fixtures are 16 kHz, mono, 16-bit PCM WAV so downstream ASR /
diarization / embedding tasks can rely on one canonical format.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUDIO = ROOT / "audio"
DOCS = ROOT / "docs"
MEETINGS = ROOT / "meetings"
REFS = ROOT / "refs"

SR = 16000
CHANNELS = 1
SAMPWIDTH = 2  # 16-bit
FRAME_BYTES = SR * SAMPWIDTH * CHANNELS  # bytes per second

# Deterministic inter-utterance gap (seconds) used by the meeting builders.
GAP = 0.4


def require(binary: str) -> None:
    if shutil.which(binary) is None:
        raise SystemExit(f"missing required binary: {binary}")


def run(cmd: list[str]) -> None:
    subprocess.run(
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
    )
    return float(out.strip())


def synth_wav(text: str, voice: str, out_path: Path) -> None:
    """Render `text` with macOS say and convert to canonical 16k mono WAV."""
    aiff = out_path.with_suffix(".aiff")
    run(["say", "-v", voice, "-o", str(aiff), text])
    run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(aiff),
            "-ar",
            str(SR),
            "-ac",
            str(CHANNELS),
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ]
    )
    aiff.unlink(missing_ok=True)


def silence_frames(seconds: float) -> bytes:
    return b"\x00\x00" * int(round(seconds * SR))


def read_pcm(path: Path) -> tuple[bytes, float]:
    """Return (frames_bytes, duration_s) for a canonical WAV, validating it."""
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == CHANNELS, f"{path}: not mono"
        assert w.getsampwidth() == SAMPWIDTH, f"{path}: not 16-bit"
        assert w.getframerate() == SR, f"{path}: not 16 kHz"
        n = w.getnframes()
        frames = w.readframes(n)
    return frames, n / SR


def write_wav(path: Path, frames: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPWIDTH)
        w.setframerate(SR)
        w.writeframes(frames)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
