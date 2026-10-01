#!/usr/bin/env python3
"""Task 30 acceptance scenario (the MCP-facing half of ``acceptance.sh``).

Spawns the registered MCP servers (commands read from
``~/.config/opencode/opencode.json``) and runs the end-to-end acceptance across:

* audiobooks - EN text PDF, ZH text PDF, and scanned PDF via MinerU
  (``acceptance_audiobooks``),
* VS-bridge speech/clone/translate and the speaker-identity protocol - enrolled +
  returning-speaker auto-recognition + unknown stranger + silent-recording
  failure path (``acceptance_speakers``),
* one call per OMO-facing capability, recorded in ``capabilities``.

Every conclusion is drawn from an ARTIFACT (file exists, independent ``ffprobe``
chapters/duration/codec, transcript JSON fields) - never from a log line. The
heavy environment setup (launchers, isolated data dir, offline sampler) lives in
``acceptance.sh``; this driver only writes the per-artifact PASS/FAIL JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from acceptance_audiobooks import audiobooks, mineru_health_capability
from acceptance_speakers import speech_capabilities, speaker_protocol
from acceptance_support import OPENCODE_JSON, Reporter, Session


def sessions() -> dict[str, Session]:
    registered = json.loads(OPENCODE_JSON.read_text())["mcp"]
    return {key: Session(key, cfg) for key, cfg in registered.items()}


async def run(evidence: Path) -> dict[str, Any]:
    rep = Reporter()
    sess = sessions()
    phases = [
        ("audiobooks", lambda: audiobooks(sess, rep, evidence)),
        ("speech_capabilities", lambda: speech_capabilities(sess, rep)),
        ("speaker_protocol", lambda: speaker_protocol(sess, rep)),
        ("mineru_health", lambda: mineru_health_capability(sess, rep)),
    ]
    for name, phase in phases:
        try:
            await phase()
        except Exception as exc:  # noqa: BLE001 - keep partial results and record the failure
            rep.check(
                f"phase_{name}",
                name,
                f"phase completed without crashing ({type(exc).__name__})",
                False,
                {"error": str(exc)},
            )
    report = rep.report()
    report.update(
        {
            "driver": "acceptance_mcp.py",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.evidence))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {
                "overall_pass": report["overall_pass"],
                "failed_checks": report["failed_checks"],
                "failed_capabilities": report["failed_capabilities"],
            },
            indent=2,
        )
    )
    print(f"wrote {args.out}")
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
