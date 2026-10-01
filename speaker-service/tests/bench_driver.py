#!/usr/bin/env python3
"""Run one meeting through ``pipeline.transcribe_meeting`` and emit a summary.

Executed by ``tests/bench.sh`` under ``/usr/bin/time -l`` so the parent records
the process's exact peak RSS. This is the service's real meeting code path
(ASR → diarization → alignment → embedding → match); the uvicorn worker runs the
same modules in-process, so measuring this process measures the service workload.

Environment (required):
    BENCH_AUDIO      path to the meeting recording
    BENCH_DRIVER_OUT path to write the JSON summary
Optional:
    BENCH_RELEASE_WHISPER=1   remediation: release the mlx-whisper weights as soon
                              as ASR finishes, so they do not stay resident while
                              pyannote/WeSpeaker load (bounds peak RSS).

The registry is isolated by the caller via ``VASTACK_DATA_DIR`` (never the
operator's real database).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Run from anywhere: the service modules live in the parent of this tests/ dir.
SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))


def _transcribe_then_release_whisper(audio_path: Path):  # type: ignore[no-untyped-def]
    """ASR, then drop the cached mlx-whisper model before diarization loads."""
    import gc

    import asr

    result = asr.transcribe_file(audio_path)
    try:
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.model = None
        ModelHolder.model_path = None
    except Exception:  # pragma: no cover - remediation best-effort only
        pass
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:  # pragma: no cover
        pass
    return result


def main() -> int:
    audio = Path(os.environ["BENCH_AUDIO"])
    out = Path(os.environ["BENCH_DRIVER_OUT"])
    if not audio.is_file():
        print(f"ERROR: fixture not found: {audio}", file=sys.stderr)
        return 2

    import pipeline

    config = None
    if os.environ.get("BENCH_RELEASE_WHISPER") == "1":
        config = pipeline.PipelineConfig(transcribe=_transcribe_then_release_whisper)

    started = time.perf_counter()
    result = pipeline.transcribe_meeting(str(audio), audio.stem, config=config)
    wall_s = time.perf_counter() - started

    summary = {
        "wall_s": wall_s,
        "segments": len(result.segments),
        "unknown_clusters": len(result.unknown_clusters),
        "speakers": list(result.speakers),
        "language": result.language,
        "warnings": list(result.warnings),
        "meeting_id": result.meeting_id,
        "job_id": result.job_id,
    }
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
