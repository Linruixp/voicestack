"""Behavior tests for calibrated speaker matching + the revision-drift guard.

Given: (a) synthetic unit vectors in a private registry, and (b) the labeled
2-speaker meeting fixtures (A in rec1+rec2, B in rec1+rec2) plus a third voice
(ZH) absent from the registry.
When: a probe embedding is matched against enrolled voiceprints at the shipped
threshold, stored provenance is hand-edited to a foreign revision, and the
score sweep is calibrated from synthetic trials.
Then: returning speakers are named at >= 0.9 precision, an unseen speaker is
`unknown` (never named), a drifted voiceprint yields `re_enroll_required`
(never a name), and calibration ships the maximum-margin threshold between the
observed genuine and impostor classes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import sqlite_vec

from calibrate import CalibrationError, calibrate, sweep_thresholds
from config import Settings
from embed import (
    EMBEDDING_DIM,
    MODEL_ID,
    MODEL_REVISION,
    Embedding,
    IdentityEmbedder,
    cosine_similarity,
)
from match import MatchError, MatchStatus, match_speaker
from registry import open_registry

FIXTURE_MEETINGS = Path.home() / "voicestack" / "fixtures" / "meetings"
FIXTURE_AUDIO = Path.home() / "voicestack" / "fixtures" / "audio"


@dataclass(frozen=True, slots=True)
class FakeEmbedding:
    """Structural stand-in for ``embed.Embedding`` at the pinned provenance."""

    vector: np.ndarray
    model_id: str = MODEL_ID
    revision: str = MODEL_REVISION
    dim: int = EMBEDDING_DIM


def basis(index: int, weight: float = 1.0) -> np.ndarray:
    """A 256-d unit vector: ``weight`` on axis ``index``, the rest on axis 0."""
    vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vector[index] = weight
    if index != 0:
        vector[0] = float(np.sqrt(max(0.0, 1.0 - weight * weight)))
    return vector


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def enrolled(db_path: Path) -> Path:
    """Registry with Alice (axis 1) and Bob (axis 2) enrolled."""
    with open_registry(db_path) as registry:
        alice = registry.add_speaker("Alice")
        bob = registry.add_speaker("Bob")
        registry.add_voiceprint(alice, FakeEmbedding(vector=basis(1)))
        registry.add_voiceprint(bob, FakeEmbedding(vector=basis(2)))
    return db_path


def test_known_speaker_is_matched_by_name(enrolled: Path) -> None:
    # Given: Alice enrolled at axis 1
    with open_registry(enrolled) as registry:
        # When: a probe near Alice's voice is matched
        decision = match_speaker(
            registry, FakeEmbedding(vector=basis(1)), threshold=0.5
        )
    # Then: the decision names Alice
    assert decision.status is MatchStatus.KNOWN
    assert (decision.speaker_id, decision.name) == (1, "Alice")
    assert decision.similarity == pytest.approx(1.0, abs=1e-5)
    assert decision.to_dict()["status"] == "known"


def test_distant_speaker_is_unknown(enrolled: Path) -> None:
    # Given: Alice at axis 1 and Bob at axis 2
    with open_registry(enrolled) as registry:
        # When: a probe orthogonal to both is matched above a strict threshold
        decision = match_speaker(
            registry, FakeEmbedding(vector=basis(3)), threshold=0.5
        )
    # Then: the nearest is below threshold -> unknown, never a name
    assert decision.status is MatchStatus.UNKNOWN
    assert decision.name is None
    assert decision.similarity == pytest.approx(0.0, abs=1e-5)


def test_empty_registry_is_unknown(db_path: Path) -> None:
    # Given: no enrolled voiceprints
    with open_registry(db_path) as registry:
        # When/Then: matching reports unknown without inventing a or a score
        decision = match_speaker(registry, FakeEmbedding(vector=basis(1)))
    assert decision.status is MatchStatus.UNKNOWN
    assert decision.similarity is None


def test_foreign_stored_revision_requires_re_enroll(db_path: Path) -> None:
    # Given: a voiceprint stored by a DIFFERENT embedding revision
    with open_registry(db_path) as registry:
        alice = registry.add_speaker("Alice")
        registry.add_voiceprint(
            alice, FakeEmbedding(vector=basis(1), revision="0" * 40)
        )
        # When: a same-speaker probe (identical vector) is matched
        decision = match_speaker(registry, FakeEmbedding(vector=basis(1)))
    # Then: it is refused for re-enrollment, never matched by name
    assert decision.status is MatchStatus.RE_ENROLL_REQUIRED
    assert (decision.speaker_id, decision.name) == (None, None)


def test_foreign_stored_model_id_requires_re_enroll(db_path: Path) -> None:
    # Given: a voiceprint whose stored model id differs from the running one
    with open_registry(db_path) as registry:
        alice = registry.add_speaker("Alice")
        registry.add_voiceprint(
            alice, FakeEmbedding(vector=basis(1), model_id="other/model")
        )
        # When/Then: a close probe is refused, not named
        decision = match_speaker(registry, FakeEmbedding(vector=basis(1)))
    assert decision.status is MatchStatus.RE_ENROLL_REQUIRED
    assert decision.name is None


def test_distant_foreign_revision_is_unknown(db_path: Path) -> None:
    # Given: a drifted voiceprint that is far from the probe (below threshold)
    with open_registry(db_path) as registry:
        alice = registry.add_speaker("Alice")
        registry.add_voiceprint(
            alice, FakeEmbedding(vector=basis(1), revision="0" * 40)
        )
        # When: the probe is far below the threshold
        decision = match_speaker(
            registry, FakeEmbedding(vector=basis(2)), threshold=0.5
        )
    # Then: no match would have happened anyway -> unknown, not re-enroll
    assert decision.status is MatchStatus.UNKNOWN


def test_foreign_query_provenance_is_rejected(enrolled: Path) -> None:
    # Given: pinned voiceprints in the registry
    with open_registry(enrolled) as registry:
        # When/Then: a query vector from a foreign revision is refused loudly
        with pytest.raises(MatchError):
            match_speaker(registry, FakeEmbedding(vector=basis(1), revision="0" * 40))


def test_sweep_reports_precision_far_and_frr() -> None:
    # Given: two genuine and two impostor similarity trials
    genuine, impostor = [0.95, 0.85], [0.2, 0.1]
    # When: the sweep evaluates four thresholds
    points = {
        point.threshold: point
        for point in sweep_thresholds(genuine, impostor, [0.99, 0.9, 0.5, 0.05])
    }
    # Then: accepts/rejects, precision, FAR and FRR are all derived from the trials
    assert (points[0.5].true_positives, points[0.5].false_positives) == (2, 0)
    assert (points[0.5].precision, points[0.5].far, points[0.5].frr) == (1.0, 0.0, 0.0)
    assert (points[0.9].true_positives, points[0.9].false_negatives) == (1, 1)
    assert (points[0.9].precision, points[0.9].far, points[0.9].frr) == (1.0, 0.0, 0.5)
    assert (points[0.05].false_positives, points[0.05].precision) == (2, 0.5)
    assert (points[0.05].far, points[0.05].frr) == (1.0, 0.0)
    # And: a reject-everything threshold scores no precision and total FRR
    assert (points[0.99].precision, points[0.99].far, points[0.99].frr) == (
        0.0,
        0.0,
        1.0,
    )


def test_calibrate_picks_maximum_margin_threshold() -> None:
    # Given: cleanly separable trials (worst genuine 0.85, best impostor 0.2)
    # When: calibration picks its threshold
    calibration = calibrate([0.95, 0.85], [0.2, 0.1])
    # Then: it is the midpoint (maximum margin to both classes) with zero error
    assert calibration.threshold == pytest.approx(0.525)
    assert (calibration.precision, calibration.far, calibration.frr) == (1.0, 0.0, 0.0)
    assert (calibration.genuine_min, calibration.impostor_max) == (0.85, 0.2)


def test_calibrate_rejects_inseparable_scores() -> None:
    # Given: overlapping classes (best impostor 0.7 beats worst genuine 0.6)
    # When/Then: calibration refuses to ship a < 0.9-precision threshold
    with pytest.raises(CalibrationError, match="precision"):
        calibrate([0.8, 0.6], [0.7, 0.4])


@pytest.fixture(scope="module")
def embedder() -> IdentityEmbedder:
    return IdentityEmbedder()


@pytest.fixture(scope="module")
def voices(embedder: IdentityEmbedder) -> dict[str, Embedding]:
    recordings = {
        "A1": FIXTURE_MEETINGS / "spkA_rec1.wav",
        "A2": FIXTURE_MEETINGS / "spkA_rec2.wav",
        "B1": FIXTURE_MEETINGS / "spkB_rec1.wav",
        "B2": FIXTURE_MEETINGS / "spkB_rec2.wav",
        "ZH": FIXTURE_AUDIO / "zh_30s.wav",
    }
    return {key: embedder.embed_file(path) for key, path in recordings.items()}


@pytest.fixture
def enrolled_fixture_speakers(db_path: Path, voices) -> Path:
    """Registry with Samantha (rec1) and Fred (rec1) enrolled."""
    with open_registry(db_path) as registry:
        samantha = registry.add_speaker("Samantha")
        fred = registry.add_speaker("Fred")
        registry.add_voiceprint(samantha, voices["A1"])
        registry.add_voiceprint(fred, voices["B1"])
    return db_path


def test_fixture_scores_calibrate_to_shipped_threshold(voices) -> None:
    # Given: enrollment from rec1, probes from rec2, and the unseen ZH voice
    genuine = [
        cosine_similarity(voices["A1"], voices["A2"]),
        cosine_similarity(voices["B1"], voices["B2"]),
    ]
    impostor = [
        cosine_similarity(voices["A1"], voices["B2"]),
        cosine_similarity(voices["B1"], voices["A2"]),
        cosine_similarity(voices["A1"], voices["ZH"]),
        cosine_similarity(voices["B1"], voices["ZH"]),
    ]
    # When: the calibrated midpoint is compared with the SHIPPED config default
    calibration = calibrate(genuine, impostor)
    shipped = Settings.model_fields["match_threshold"].default
    point = sweep_thresholds(genuine, impostor, [shipped])[0]
    # Then: the shipped default IS the calibrated one, at >= 0.9 precision / 0 FAR
    assert shipped == calibration.threshold
    assert calibration.precision >= 0.9
    assert point.precision >= 0.9
    assert calibration.far == 0.0 and calibration.frr == 0.0


def test_returning_speakers_are_matched_at_high_precision(
    enrolled_fixture_speakers: Path, voices
) -> None:
    # Given: Samantha and Fred enrolled from rec1
    with open_registry(enrolled_fixture_speakers) as registry:
        # When: the rec2 probes are matched at the shipped threshold
        a_decision = match_speaker(registry, voices["A2"])
        b_decision = match_speaker(registry, voices["B2"])
    # Then: both returning speakers are named correctly
    assert (a_decision.status, a_decision.name) == (MatchStatus.KNOWN, "Samantha")
    assert (b_decision.status, b_decision.name) == (MatchStatus.KNOWN, "Fred")
    # And: every accepted match is at or above the shipped threshold
    assert all(
        decision.similarity >= decision.threshold
        for decision in (a_decision, b_decision)
    )


def test_unseen_speaker_is_unknown(enrolled_fixture_speakers: Path, voices) -> None:
    # Given: a third voice absent from the registry
    with open_registry(enrolled_fixture_speakers) as registry:
        # When: it is matched against Samantha and Fred
        decision = match_speaker(registry, voices["ZH"])
    # Then: it is unknown, never assigned a name
    assert decision.status is MatchStatus.UNKNOWN
    assert decision.name is None
    assert decision.similarity < decision.threshold


def test_hand_edited_revision_returns_re_enroll_required(db_path: Path, voices) -> None:
    # Given: Samantha enrolled, then her stored revision hand-edited (drift)
    with open_registry(db_path) as registry:
        samantha = registry.add_speaker("Samantha")
        registry.add_voiceprint(samantha, voices["A1"])
    raw = sqlite3.connect(db_path)
    raw.enable_load_extension(True)
    sqlite_vec.load(raw)
    raw.enable_load_extension(False)
    raw.execute(
        "UPDATE voiceprints SET revision = ? WHERE speaker_id = ?",
        ("deadbeef" * 5, samantha),
    )
    raw.commit()
    raw.close()
    # When: Samantha's own rec2 probe is matched
    with open_registry(db_path) as registry:
        decision = match_speaker(registry, voices["A2"])
    # Then: the match is refused for re-enrollment, never named
    assert decision.status is MatchStatus.RE_ENROLL_REQUIRED
    assert (decision.speaker_id, decision.name) == (None, None)
