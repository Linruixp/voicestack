"""Behavior tests for the versioned WeSpeaker identity embedder.

Given: labeled fixtures where speaker A appears in two recordings (rec1, rec2)
and speaker B appears in two recordings (rec1, rec2).
When: each recording is embedded with the single pinned identity model, and a
cluster is built from its member recordings.
Then: every vector is a 256-d L2-normalized unit vector that carries its
`{model_id, revision, dim}` provenance, same-speaker similarity is clearly
higher than cross-speaker similarity, and a cluster embedding is the
re-normalized mean of its members.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from embed import (
    EMBEDDING_DIM,
    MODEL_ID,
    MODEL_REVISION,
    Embedding,
    IdentityEmbedder,
    cosine_similarity,
)

FIXTURES = Path.home() / "voicestack" / "fixtures" / "meetings"
RECORDINGS = {
    "A1": "spkA_rec1.wav",
    "A2": "spkA_rec2.wav",
    "B1": "spkB_rec1.wav",
    "B2": "spkB_rec2.wav",
}


@pytest.fixture(scope="module")
def embedder() -> IdentityEmbedder:
    return IdentityEmbedder()


@pytest.fixture(scope="module")
def per_recording(embedder: IdentityEmbedder) -> dict[str, Embedding]:
    return {
        key: embedder.embed_file(FIXTURES / name) for key, name in RECORDINGS.items()
    }


def test_embedding_is_unit_256d(per_recording: dict[str, Embedding]) -> None:
    # Given: one embedding per labeled recording
    # When: inspected
    # Then: every vector is 256-d and L2-normalized
    for key, embedding in per_recording.items():
        assert embedding.vector.shape == (EMBEDDING_DIM,), key
        assert embedding.vector.dtype == np.float32, key
        assert float(np.linalg.norm(embedding.vector)) == pytest.approx(
            1.0, abs=1e-5
        ), key


def test_every_vector_records_model_identity(
    per_recording: dict[str, Embedding],
) -> None:
    # Given: the single pinned identity model
    # When: vectors are produced
    # Then: model id, revision and dim are attached to every vector
    for key, embedding in per_recording.items():
        assert embedding.model_id == MODEL_ID, key
        assert embedding.revision == MODEL_REVISION, key
        assert embedding.dim == EMBEDDING_DIM, key


def test_same_speaker_similarity_exceeds_cross_speaker(
    per_recording: dict[str, Embedding],
) -> None:
    # Given: two recordings of A and two of B
    a1, a2, b1, b2 = (per_recording[k] for k in ("A1", "A2", "B1", "B2"))
    # When: cosine similarity is measured within and across speakers
    same_a = cosine_similarity(a1, a2)
    same_b = cosine_similarity(b1, b2)
    cross = [
        cosine_similarity(a1, b1),
        cosine_similarity(a1, b2),
        cosine_similarity(a2, b1),
        cosine_similarity(a2, b2),
    ]
    # Then: same-speaker beats cross-speaker with a wide margin
    assert min(same_a, same_b) > max(cross)
    assert min(same_a, same_b) > 0.75
    assert max(cross) < 0.5


def test_cluster_embedding_is_renormalized_mean(
    embedder: IdentityEmbedder, per_recording: dict[str, Embedding]
) -> None:
    # Given: two member recordings of speaker A
    a1, a2, b1 = per_recording["A1"], per_recording["A2"], per_recording["B1"]
    # When: the cluster embedding is computed
    cluster = embedder.embed_cluster([a1, a2])
    # Then: it is a unit 256-d vector carrying the same provenance
    assert cluster.vector.shape == (EMBEDDING_DIM,)
    assert float(np.linalg.norm(cluster.vector)) == pytest.approx(1.0, abs=1e-5)
    assert (cluster.model_id, cluster.revision, cluster.dim) == (
        MODEL_ID,
        MODEL_REVISION,
        EMBEDDING_DIM,
    )
    # And: it sits with its own speaker, away from the other speaker
    assert cosine_similarity(cluster, a1) > 0.95
    assert cosine_similarity(cluster, b1) < 0.5


def test_cluster_embedding_accepts_raw_vectors(
    per_recording: dict[str, Embedding],
) -> None:
    # Given: raw member vectors (as the registry would store them)
    a1, a2 = per_recording["A1"], per_recording["A2"]
    # When: a cluster is built from bare arrays
    cluster = IdentityEmbedder().embed_cluster([a1.vector, a2.vector])
    # Then: the result is still a normalized, versioned embedding
    assert cluster.vector.shape == (EMBEDDING_DIM,)
    assert float(np.linalg.norm(cluster.vector)) == pytest.approx(1.0, abs=1e-5)
    assert cluster.revision == MODEL_REVISION


def test_empty_cluster_is_rejected(embedder: IdentityEmbedder) -> None:
    # Given: no members
    # When/Then: cluster construction fails loudly instead of returning a zero vector
    with pytest.raises(ValueError, match="empty"):
        embedder.embed_cluster([])


def test_mismatched_member_dimension_is_rejected(embedder: IdentityEmbedder) -> None:
    # Given: a member vector that is not the model's dimension
    bad = np.ones(EMBEDDING_DIM - 1, dtype=np.float32)
    # When/Then: cluster construction rejects it (no silent shape coercion)
    with pytest.raises(ValueError, match="shape"):
        embedder.embed_cluster([bad])
