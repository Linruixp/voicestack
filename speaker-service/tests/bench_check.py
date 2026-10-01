#!/usr/bin/env python3
"""Compare a benchmark measurement against pre-committed ceilings.

One source of truth for the gate: `tests/bench.sh` (real run) and
`tests/bench.sh --falsify` (deliberately-lowered ceilings) both call this, so a
pass/fail decision can never diverge between the two paths.

Exit 0 when BOTH `peak_rss_bytes <= rss_ceiling` AND `wall_s <= wall_ceiling`;
exit 1 otherwise. Prints the comparison in both cases.

Usage:
    bench_check.py --measurement FILE --rss-ceiling BYTES --wall-ceiling SECONDS
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def evaluate(
    measurement: dict[str, object], rss_ceiling: int, wall_ceiling: float
) -> tuple[bool, list[str]]:
    """Return ``(ok, lines)`` for the ceiling comparison (pure, testable)."""
    wall = float(measurement["wall_s"])  # type: ignore[arg-type]
    rss = int(measurement["peak_rss_bytes"])  # type: ignore[arg-type]
    lines: list[str] = []
    ok = True
    if wall <= wall_ceiling:
        lines.append(f"  OK     wall {wall:.1f}s <= ceiling {wall_ceiling:.1f}s")
    else:
        ok = False
        lines.append(f"  BREACH wall {wall:.1f}s >  ceiling {wall_ceiling:.1f}s")
    if rss <= rss_ceiling:
        lines.append(f"  OK     rss  {rss} B <= ceiling {rss_ceiling} B")
    else:
        ok = False
        lines.append(f"  BREACH rss  {rss} B >  ceiling {rss_ceiling} B")
    lines.append(f"  RESULT {'PASS' if ok else 'FAIL'}")
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurement", required=True, type=Path)
    parser.add_argument("--rss-ceiling", required=True, type=int)
    parser.add_argument("--wall-ceiling", required=True, type=float)
    args = parser.parse_args(argv)

    measurement = json.loads(args.measurement.read_text(encoding="utf-8"))
    ok, lines = evaluate(measurement, args.rss_ceiling, args.wall_ceiling)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
