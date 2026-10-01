#!/usr/bin/env python3
"""Explicit CPU-vs-MPS probe for the pinned pyannote diarization pipeline.

Loads community-1 on each supported device (cpu, mps) and diarizes the given
clip, recording success/failure, wall time, speakers and turns. The resulting
JSON is the device-decision evidence: the default ``VASTACK_DIARIZE_DEVICE`` is
pinned from this measurement, never silently assumed.

Usage: uv run --no-sync python scripts/probe_device.py <audio.wav> [report.json]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diarize  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    audio = Path(sys.argv[1])
    if not audio.is_file():
        print(f"audio file not found: {audio}")
        return 2
    report_path = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else audio.with_suffix(".device-probe.json")
    )

    configured = diarize.get_settings().diarize_device
    report: dict = {
        "model": {"repo_id": diarize.MODEL_REPO_ID, "revision": diarize.MODEL_REVISION},
        "audio": str(audio),
        "configured_device": configured,
        "mps_available": diarize._mps_available(),
        "attempts": [],
    }
    print(f"[probe] configured device: {configured}", flush=True)

    for name in diarize.SUPPORTED_DEVICES:
        attempt: dict = {"device": name}
        try:
            started = time.perf_counter()
            pipeline = diarize.load_pipeline(name)
            attempt["load_seconds"] = round(time.perf_counter() - started, 2)
            started = time.perf_counter()
            turns = diarize.diarize(audio, pipeline=pipeline)
            attempt.update(
                ok=True,
                run_seconds=round(time.perf_counter() - started, 2),
                n_turns=len(turns),
                speakers=sorted({turn["speaker"] for turn in turns}),
                turns=turns,
            )
            print(
                f"[probe] {name}: OK {len(turns)} turns / "
                f"{len(attempt['speakers'])} speakers in {attempt['run_seconds']}s",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - the failure itself is the evidence
            attempt.update(
                ok=False, error_type=type(exc).__name__, error=str(exc)[:500]
            )
            print(f"[probe] {name}: FAIL {type(exc).__name__}: {exc}"[:300], flush=True)
        report["attempts"].append(attempt)

    successful = {a["device"]: a for a in report["attempts"] if a.get("ok")}
    if len(successful) == 2:
        report["turns_identical"] = (
            successful["cpu"]["turns"] == successful["mps"]["turns"]
        )
    if configured in successful:
        report["decision"] = f"keep {configured} (configured device works)"
    elif successful:
        fallback = next(iter(successful))
        report["decision"] = (
            f"configured device {configured!r} failed; pin VASTACK_DIARIZE_DEVICE="
            f"{fallback} only after manual review"
        )
    else:
        report["decision"] = "no device worked; diarization is not runnable here"
    print(f"[probe] decision: {report['decision']}", flush=True)

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[probe] report -> {report_path}", flush=True)
    return 0 if configured in successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
