"""Calibrated cosine matching of probe embeddings to enrolled voiceprints.

The runtime path is one sqlite-vec query: ``k=1`` cosine KNN over
``voiceprints`` (``distance = 1 - cosine``). The decision order is deliberate:

1. provenance gate - a query vector not produced by the running identity model
   is refused loudly (:class:`MatchError`); matching a foreign vector against
   pinned voiceprints is exactly the cross-revision false positive to avoid.
2. evidence gate - if the nearest voiceprint's similarity is below the
   threshold, the speaker is ``unknown``. A drifted voiceprint that no probe
   comes near is not a re-enrollment signal; it is simply not a match.
3. drift gate - if a match WOULD be declared but the stored voiceprint names a
   different ``model_id``/``revision``/``dim``, it is refused with
   ``re_enroll_required`` (never a name): the two vectors live in different
   embedding spaces.
4. otherwise the speaker's name is returned.

The threshold is never hardcoded at runtime: it resolves to
``config.Settings.match_threshold`` (``VASTACK_MATCH_THRESHOLD``), which ships
the value produced by ``calibrate.calibrate`` on the labeled meeting fixtures
(see ``scripts/calibrate_match.py`` for the protocol and FAR/FRR).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from config import get_settings
from embed import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION
from registry import Registry
from registry_models import VersionedVector

__all__ = [
    "MatchDecision",
    "MatchError",
    "MatchStatus",
    "match_speaker",
]

_PINNED = (MODEL_ID, MODEL_REVISION, EMBEDDING_DIM)


class MatchError(RuntimeError):
    """A matching request could not be honored (bad threshold, foreign query)."""


class MatchStatus(StrEnum):
    """Outcome of matching one probe against the registry."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    RE_ENROLL_REQUIRED = "re_enroll_required"


@dataclass(frozen=True, slots=True)
class MatchDecision:
    """A matching verdict; ``speaker_id``/``name`` are set only when ``KNOWN``."""

    status: MatchStatus
    threshold: float
    speaker_id: int | None = None
    name: str | None = None
    similarity: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view (used by the HTTP layer and evidence capture)."""
        return {
            "status": self.status.value,
            "threshold": self.threshold,
            "speaker_id": self.speaker_id,
            "name": self.name,
            "similarity": self.similarity,
            "reason": self.reason,
        }


def _resolve_threshold(threshold: float | None) -> float:
    resolved = float(get_settings().match_threshold if threshold is None else threshold)
    if not 0.0 < resolved < 1.0:
        raise MatchError(
            f"match threshold must be strictly between 0 and 1, got {resolved}"
        )
    return resolved


def match_speaker(
    registry: Registry,
    embedding: VersionedVector,
    *,
    threshold: float | None = None,
) -> MatchDecision:
    """Match ``embedding`` against the registry (cosine KNN, ``k=1``).

    Returns ``known`` with the speaker's id/name, ``unknown`` when nothing is
    close enough, or ``re_enroll_required`` when the winning voiceprint was
    stored by a different embedding model revision.
    """
    resolved = _resolve_threshold(threshold)
    if (embedding.model_id, embedding.revision, embedding.dim) != _PINNED:
        raise MatchError(
            f"query embedding was produced by {embedding.model_id}@"
            f"{embedding.revision} (dim {embedding.dim}), not the running "
            f"model {MODEL_ID}@{MODEL_REVISION}; refusing to match across "
            "embedding revisions"
        )
    matches = registry.nearest_voiceprints(embedding, k=1)
    if not matches:
        return MatchDecision(
            MatchStatus.UNKNOWN, resolved, reason="no voiceprints enrolled"
        )
    best = matches[0]
    similarity = 1.0 - best.distance
    if similarity < resolved:
        return MatchDecision(
            MatchStatus.UNKNOWN,
            resolved,
            similarity=similarity,
            reason=(
                f"nearest cosine {similarity:.4f} is below the threshold {resolved:.4f}"
            ),
        )
    if (best.model_id, best.revision, best.dim) != _PINNED:
        return MatchDecision(
            MatchStatus.RE_ENROLL_REQUIRED,
            resolved,
            similarity=similarity,
            reason=(
                f"voiceprint {best.voiceprint_id} was stored by "
                f"{best.model_id}@{best.revision}; re-enroll with the running "
                f"model {MODEL_ID}@{MODEL_REVISION}"
            ),
        )
    speaker = registry.get_speaker(best.speaker_id)
    if speaker is None:
        raise MatchError(
            f"registry is inconsistent: voiceprint {best.voiceprint_id} "
            f"references missing speaker {best.speaker_id}"
        )
    return MatchDecision(
        MatchStatus.KNOWN,
        resolved,
        speaker_id=speaker.id,
        name=speaker.name,
        similarity=similarity,
        reason=f"cosine {similarity:.4f} is at or above {resolved:.4f}",
    )
