"""Speech recognition for the speaker-service.

Wraps ``mlx-whisper`` (large-v3) to produce timestamped segments and
word-level timestamps plus the detected language. The model is pinned to an
exact revision and resolved from the pre-fetched Hugging Face hub cache, so
transcription never touches the network.

The module also pre-screens for digital silence: Whisper happily
hallucinates on a silent clip, so a fully silent input returns an empty
transcript instead of invented text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Model-cache reuse -----------------------------------------------------
# Must run before any Hugging Face import so the shared hub is used instead of
# a fresh per-service cache (same defaults as config.py).
_HF_HOME = Path.home() / ".cache" / "huggingface"
os.environ.setdefault("HF_HOME", str(_HF_HOME))
os.environ.setdefault("HF_HUB_CACHE", str(_HF_HOME / "hub"))

MODEL_REPO_ID = "mlx-community/whisper-large-v3-mlx"
MODEL_REVISION = "49e6aa286ad60c14352c404340ded53710378a11"

TRANSCRIPT_DIR = (
    Path.home() / "Library" / "Application Support" / "VoiceStudioStack" / "transcripts"
)

# RMS threshold (dBFS) below which a clip is considered physically silent.
SILENCE_THRESHOLD_DBFS = -50.0


@dataclass(frozen=True, slots=True)
class Word:
    """A single recognized word with its time span (seconds)."""

    word: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Segment:
    """A transcription segment with word-level detail."""

    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()


@dataclass(frozen=True, slots=True)
class Transcript:
    """Normalized ASR result: detected language plus timestamped segments."""

    language: str
    text: str
    segments: tuple[Segment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire schema ``{language, segments:[{start,end,text,words}]}``."""
        return {
            "language": self.language,
            "segments": [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "words": [
                        {"word": word.word, "start": word.start, "end": word.end}
                        for word in segment.words
                    ],
                }
                for segment in self.segments
            ],
        }


def model_snapshot_path() -> Path:
    """Return the local path of the pinned, pre-fetched model snapshot.

    Never downloads: raises ``FileNotFoundError`` when the pinned revision is
    not already present in the hub cache.
    """
    hub_cache = Path(os.environ["HF_HUB_CACHE"])
    repo_dir = hub_cache / f"models--{MODEL_REPO_ID.replace('/', '--')}"
    snapshot = repo_dir / "snapshots" / MODEL_REVISION
    weights = snapshot / "weights.npz"
    if not weights.is_file():
        weights = snapshot / "weights.safetensors"
    if (snapshot / "config.json").is_file() and weights.is_file():
        return snapshot

    # Fallback for a non-standard cache layout: local-only resolution (offline).
    try:
        from huggingface_hub import snapshot_download

        return Path(
            snapshot_download(
                repo_id=MODEL_REPO_ID,
                revision=MODEL_REVISION,
                local_files_only=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - re-raised with actionable context
        raise FileNotFoundError(
            f"pinned model {MODEL_REPO_ID}@{MODEL_REVISION} not found in "
            f"{hub_cache}; run scripts/fetch_models.py first ({exc})"
        ) from exc


def _rms_dbfs(audio_path: Path) -> float | None:
    """Return the clip's RMS level in dBFS, or ``None`` if it is unreadable."""
    try:
        with wave.open(str(audio_path), "rb") as handle:
            sample_width = handle.getsampwidth()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, OSError):
        return None

    if not frames:
        return None

    if sample_width == 1:
        samples = array("B", frames)
        scale, offset = 128.0, -1.0
    elif sample_width == 2:
        samples = array("h", frames)
        scale, offset = 32768.0, 0.0
    elif sample_width == 4:
        samples = array("i", frames)
        scale, offset = 2147483648.0, 0.0
    else:
        return None

    if not samples:
        return None

    mean_square = sum(((value / scale) + offset) ** 2 for value in samples) / len(
        samples
    )
    if mean_square <= 0.0:
        return float("-inf")
    return 10.0 * math.log10(mean_square)


def _is_silent(audio_path: Path) -> bool:
    level = _rms_dbfs(audio_path)
    return level is not None and level <= SILENCE_THRESHOLD_DBFS


def transcribe_raw(
    audio_path: str | os.PathLike[str],
    *,
    language: str | None = None,
    word_timestamps: bool = True,
) -> dict[str, Any]:
    """Run mlx-whisper and return its raw result dict.

    Fully silent input short-circuits to an empty result (no model load), so
    silence is handled gracefully instead of triggering a hallucination.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(path)

    if _is_silent(path):
        return {"text": "", "segments": [], "language": language or ""}

    import mlx_whisper

    return mlx_whisper.transcribe(
        str(path),
        path_or_hf_repo=str(model_snapshot_path()),
        word_timestamps=word_timestamps,
        language=language,
        verbose=None,
    )


def parse_transcript(raw: dict[str, Any]) -> Transcript:
    """Normalize a raw mlx-whisper result into a :class:`Transcript`."""
    segments: list[Segment] = []
    for segment in raw.get("segments") or []:
        words = tuple(
            Word(
                word=str(word.get("word", "")),
                start=float(word["start"]),
                end=float(word["end"]),
            )
            for word in segment.get("words") or []
        )
        segments.append(
            Segment(
                start=float(segment["start"]),
                end=float(segment["end"]),
                text=str(segment.get("text", "")),
                words=words,
            )
        )
    return Transcript(
        language=str(raw.get("language") or ""),
        text=str(raw.get("text") or "").strip(),
        segments=tuple(segments),
    )


def transcribe_file(
    audio_path: str | os.PathLike[str],
    *,
    language: str | None = None,
    word_timestamps: bool = True,
) -> Transcript:
    """Transcribe ``audio_path`` into a normalized :class:`Transcript`."""
    return parse_transcript(
        transcribe_raw(audio_path, language=language, word_timestamps=word_timestamps)
    )


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def persist_raw_transcript(
    raw: dict[str, Any],
    audio_path: str | os.PathLike[str],
    *,
    transcript_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Persist the raw transcription JSON and return the written path."""
    source = Path(audio_path)
    directory = Path(transcript_dir) if transcript_dir else TRANSCRIPT_DIR
    directory.mkdir(parents=True, exist_ok=True)

    digest = _stream_sha256(source)[:12] if source.is_file() else "unknown"
    destination = directory / f"{source.stem}-{digest}.json"
    payload = {
        "audio": str(source.resolve()),
        "model": {"repo_id": MODEL_REPO_ID, "revision": MODEL_REVISION},
        "language": raw.get("language"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw,
    }
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destination


def transcribe_and_persist(
    audio_path: str | os.PathLike[str],
    *,
    language: str | None = None,
    word_timestamps: bool = True,
    transcript_dir: str | os.PathLike[str] | None = None,
) -> tuple[Transcript, Path]:
    """Transcribe, persist the raw result, and return ``(transcript, path)``."""
    raw = transcribe_raw(audio_path, language=language, word_timestamps=word_timestamps)
    saved = persist_raw_transcript(raw, audio_path, transcript_dir=transcript_dir)
    return parse_transcript(raw), saved
