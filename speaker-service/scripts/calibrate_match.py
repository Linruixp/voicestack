#!/usr/bin/env python3
"""Calibrate the voiceprint match threshold on the labeled meeting fixtures.

Enrolls both speakers from recording 1 (Samantha/Fred), then probes recording 2
through the real sqlite-vec registry (k=2, cosine) to collect genuine and
impostor similarities. A third voice (ZH), never enrolled, is probed against
both enrolled speakers and contributes its similarities to the impostor class,
so FAR is measured against a speaker the registry has never seen. The script
sweeps candidate thresholds, reports precision/FAR/FRR, and prints the
calibration chosen by ``match.calibrate`` (maximum-margin midpoint, refused
below 0.9 precision).

Usage: uv run python scripts/calibrate_match.py [--report out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibrate import calibrate, sweep_thresholds  # noqa: E402
from config import get_settings  # noqa: E402
from embed import (  # noqa: E402
    EMBEDDING_DIM,
    MODEL_ID,
    MODEL_REVISION,
    IdentityEmbedder,
)
from match import match_speaker  # noqa: E402
from registry import open_registry  # noqa: E402

MEETINGS = Path.home() / "voicestack" / "fixtures" / "meetings"
AUDIO = Path.home() / "voicestack" / "fixtures" / "audio"
ENROLLMENT = {"Samantha": "spkA_rec1.wav", "Fred": "spkB_rec1.wav"}
PROBES = {"A2": ("spkA_rec2.wav", "Samantha"), "B2": ("spkB_rec2.wav", "Fred")}
UNSEEN = {"ZH": AUDIO / "zh_30s.wav"}
SWEEP = [round(0.30 + 0.05 * step, 2) for step in range(14)]


def _collect_trials(
    registry, vectors: dict, ids: dict[str, int]
) -> dict[str, list[dict]]:
    trials: dict[str, list[dict]] = {"genuine": [], "impostor": []}
    for probe, (_, speaker) in PROBES.items():
        distances = {
            match.speaker_id: 1.0 - match.distance
            for match in registry.nearest_voiceprints(vectors[probe], k=2)
        }
        for candidate, speaker_id in ids.items():
            bucket = "genuine" if candidate == speaker else "impostor"
            trials[bucket].append(
                {
                    "probe": probe,
                    "speaker": candidate,
                    "similarity": distances[speaker_id],
                }
            )
    for probe in UNSEEN:
        distances = {
            match.speaker_id: 1.0 - match.distance
            for match in registry.nearest_voiceprints(vectors[probe], k=2)
        }
        for candidate, speaker_id in ids.items():
            trials["impostor"].append(
                {
                    "probe": probe,
                    "speaker": candidate,
                    "similarity": distances[speaker_id],
                }
            )
    return trials


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    embedder = IdentityEmbedder()
    vectors = {
        name: embedder.embed_file(MEETINGS / filename)
        for name, filename in ENROLLMENT.items()
    }
    for label, (filename, _) in PROBES.items():
        vectors[label] = embedder.embed_file(MEETINGS / filename)
    vectors.update({name: embedder.embed_file(path) for name, path in UNSEEN.items()})

    with tempfile.TemporaryDirectory() as temporary:
        with open_registry(Path(temporary) / "calibration.db") as registry:
            ids = {}
            for name in ENROLLMENT:
                speaker_id = registry.add_speaker(name)
                registry.add_voiceprint(speaker_id, vectors[name])
                ids[name] = speaker_id
            trials = _collect_trials(registry, vectors, ids)
            genuine = [trial["similarity"] for trial in trials["genuine"]]
            impostor = [trial["similarity"] for trial in trials["impostor"]]
            calibration = calibrate(genuine, impostor)
            breakpoints = {
                round(calibration.impostor_max, 6),
                round(calibration.threshold, 6),
                round(calibration.genuine_min, 6),
            }
            sweep = sweep_thresholds(
                genuine, impostor, sorted(set(SWEEP) | breakpoints)
            )
            decisions = {
                label: match_speaker(registry, vectors[label]).to_dict()
                for label in [*PROBES, *UNSEEN]
            }

    shipped = get_settings().match_threshold
    report = {
        "task": "20",
        "kind": "match-threshold-calibration",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fixture": {
            "enrollment": {
                name: f"fixtures/meetings/{filename}"
                for name, filename in ENROLLMENT.items()
            },
            "probes": {
                label: f"fixtures/meetings/{filename}"
                for label, (filename, _) in PROBES.items()
            },
            "unseen": {
                label: f"fixtures/audio/{path.name}" for label, path in UNSEEN.items()
            },
        },
        "embedding": {
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "dim": EMBEDDING_DIM,
        },
        "trials": trials,
        "sweep": [
            {
                "threshold": point.threshold,
                "true_positives": point.true_positives,
                "false_positives": point.false_positives,
                "false_negatives": point.false_negatives,
                "precision": point.precision,
                "far": point.far,
                "frr": point.frr,
            }
            for point in sweep
        ],
        "calibration": {
            "threshold": calibration.threshold,
            "precision": calibration.precision,
            "far": calibration.far,
            "frr": calibration.frr,
            "genuine_min": calibration.genuine_min,
            "impostor_max": calibration.impostor_max,
            "genuine_trials": calibration.genuine_trials,
            "impostor_trials": calibration.impostor_trials,
        },
        "shipped_threshold": shipped,
        "shipped_matches_calibration": shipped == calibration.threshold,
        "match_decisions": decisions,
    }
    encoded = json.dumps(report, indent=2)
    print(encoded)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["shipped_matches_calibration"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
