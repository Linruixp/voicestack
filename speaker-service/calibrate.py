"""Offline threshold calibration for voiceprint matching.

Scores labeled genuine/impostor similarity trials (see
``scripts/calibrate_match.py`` for the fixture protocol), reports
precision/FAR/FRR at every candidate threshold, and picks the shipped
threshold as the maximum-margin midpoint between the worst genuine and the
best impostor score. Calibration refuses to ship anything below the requested
precision instead of papering over overlapping classes.

The calibrated value lives in ``config.Settings.match_threshold`` on purpose:
the runtime matcher never hardcodes a threshold.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from match import MatchError

__all__ = [
    "Calibration",
    "CalibrationError",
    "ThresholdPoint",
    "calibrate",
    "sweep_thresholds",
]


class CalibrationError(MatchError):
    """Calibration trials cannot produce a threshold at the requested precision."""


@dataclass(frozen=True, slots=True)
class ThresholdPoint:
    """Precision/FAR/FRR at one candidate threshold.

    ``far = false_positives / impostor_trials`` and
    ``frr = false_negatives / genuine_trials``; precision is 0 when the
    threshold accepts nothing (no positive predictions).
    """

    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    far: float
    frr: float


@dataclass(frozen=True, slots=True)
class Calibration:
    """The shipped threshold plus the trial metrics it was chosen from."""

    threshold: float
    precision: float
    far: float
    frr: float
    genuine_min: float
    impostor_max: float
    genuine_trials: int
    impostor_trials: int


def sweep_thresholds(
    genuine: Sequence[float],
    impostor: Sequence[float],
    thresholds: Iterable[float],
) -> list[ThresholdPoint]:
    """Score every candidate threshold against labeled genuine/impostor trials."""
    genuine_scores = [float(score) for score in genuine]
    impostor_scores = [float(score) for score in impostor]
    if not genuine_scores or not impostor_scores:
        raise CalibrationError(
            "calibration needs at least one genuine and one impostor trial"
        )
    points: list[ThresholdPoint] = []
    for candidate in thresholds:
        threshold = float(candidate)
        if not 0.0 <= threshold <= 1.0:
            raise CalibrationError(
                f"candidate threshold must be within [0, 1], got {threshold}"
            )
        true_positives = sum(score >= threshold for score in genuine_scores)
        false_positives = sum(score >= threshold for score in impostor_scores)
        false_negatives = len(genuine_scores) - true_positives
        predicted_positive = true_positives + false_positives
        points.append(
            ThresholdPoint(
                threshold=threshold,
                true_positives=true_positives,
                false_positives=false_positives,
                false_negatives=false_negatives,
                precision=(
                    true_positives / predicted_positive if predicted_positive else 0.0
                ),
                far=false_positives / len(impostor_scores),
                frr=false_negatives / len(genuine_scores),
            )
        )
    return points


def calibrate(
    genuine: Sequence[float],
    impostor: Sequence[float],
    *,
    min_precision: float = 0.9,
) -> Calibration:
    """Pick the maximum-margin threshold between the observed score classes.

    The midpoint between the worst genuine and the best impostor maximizes the
    distance to both classes. The metrics at that threshold are recomputed and
    the calibration refuses (``CalibrationError``) to ship anything below
    ``min_precision`` - overlapping classes must be fixed, not papered over.
    """
    if not 0.0 < min_precision <= 1.0:
        raise CalibrationError(
            f"min_precision must be within (0, 1], got {min_precision}"
        )
    worst_genuine = min(float(score) for score in genuine)
    best_impostor = max(float(score) for score in impostor)
    threshold = round((worst_genuine + best_impostor) / 2.0, 3)
    if not 0.0 < threshold < 1.0:
        raise CalibrationError(
            f"calibrated threshold {threshold} is not strictly within (0, 1)"
        )
    point = sweep_thresholds(genuine, impostor, [threshold])[0]
    if point.precision < min_precision:
        raise CalibrationError(
            f"genuine and impostor scores are not separable at precision >= "
            f"{min_precision}: threshold {threshold:.3f} gives precision "
            f"{point.precision:.3f}, FAR {point.far:.3f}, FRR {point.frr:.3f}"
        )
    return Calibration(
        threshold=threshold,
        precision=point.precision,
        far=point.far,
        frr=point.frr,
        genuine_min=worst_genuine,
        impostor_max=best_impostor,
        genuine_trials=len(genuine),
        impostor_trials=len(impostor),
    )
