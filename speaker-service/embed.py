"""Versioned WeSpeaker identity embeddings for enrollment and recognition.

One model, one vector space. Both enrollment (a named speaker's canonical
voiceprint) and recognition (a diarized cluster) use exactly the model pinned
here::

    pyannote/wespeaker-voxceleb-resnet34-LM @ 837717dd -> 256-d

Every returned vector carries its ``(model_id, revision, dim)`` provenance so
downstream storage can refuse to match across embedding-model revisions — an
incompatible vector space is the top wrong-implementation trap.

The model is ungated and is loaded offline from the shared Hugging Face cache
(``config.py`` points ``HF_HOME`` / ``HF_HUB_CACHE`` at ``~/.cache/huggingface``);
no download is ever attempted. CPU is the default device so concurrently
running ML tasks are not starved of unified memory.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import config  # noqa: F401  # sets HF_HOME / HF_HUB_CACHE before any HF import

# The pinned snapshot must be served from disk: a missing snapshot is a hard
# error, never a silent network download.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402
import torch  # noqa: E402
from pyannote.audio import Inference, Model  # noqa: E402

MODEL_ID: Final = "pyannote/wespeaker-voxceleb-resnet34-LM"
MODEL_REVISION: Final = "837717ddb9ff5507820346191109dc79c958d614"
EMBEDDING_DIM: Final = 256
SAMPLE_RATE: Final = 16_000


@dataclass(frozen=True, slots=True, eq=False)
class Embedding:
    """An L2-normalized identity vector plus the model that produced it."""

    vector: np.ndarray
    model_id: str = MODEL_ID
    revision: str = MODEL_REVISION
    dim: int = EMBEDDING_DIM

    def __post_init__(self) -> None:
        if self.vector.shape != (self.dim,):
            raise ValueError(
                f"embedding vector has shape {self.vector.shape}, expected ({self.dim},)"
            )
        if self.dim != EMBEDDING_DIM or (self.model_id, self.revision) != (
            MODEL_ID,
            MODEL_REVISION,
        ):
            raise ValueError(
                "embedding metadata does not match the pinned identity model"
            )


def _load_model() -> Model:
    """Load the pinned WeSpeaker model from the local HF cache (no network)."""
    model = Model.from_pretrained(MODEL_ID, revision=MODEL_REVISION, token=False)
    if model is None:
        raise RuntimeError(
            f"identity model {MODEL_ID}@{MODEL_REVISION} is not present in the HF cache"
        )
    return model


def _l2_normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0.0:
        raise ValueError("cannot normalize a zero or non-finite embedding")
    return (vector / norm).astype(np.float32)


def _vector_of(embedding: Embedding | np.ndarray) -> np.ndarray:
    return (
        embedding.vector if isinstance(embedding, Embedding) else np.asarray(embedding)
    )


def cosine_similarity(a: Embedding | np.ndarray, b: Embedding | np.ndarray) -> float:
    """Cosine similarity between two vectors; scale-invariant, so unit-input is optional."""
    va = _vector_of(a).astype(np.float64).reshape(-1)
    vb = _vector_of(b).astype(np.float64).reshape(-1)
    if va.shape != vb.shape:
        raise ValueError(
            f"cannot compare embeddings of shapes {va.shape} and {vb.shape}"
        )
    denominator = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denominator == 0.0:
        raise ValueError("cannot compare a zero embedding")
    return float(np.dot(va, vb) / denominator)


class IdentityEmbedder:
    """The single identity model, exposed as a 256-d unit-vector extractor."""

    def __init__(self, device: str | torch.device = "cpu") -> None:
        self._device = torch.device(device)
        self._inference = Inference(_load_model(), window="whole", device=self._device)

    @property
    def device(self) -> torch.device:
        return self._device

    def embed_file(self, path: str | Path) -> Embedding:
        """Embed an audio file (decoding/resampling handled by pyannote)."""
        return self._embed(np.asarray(self._inference(path)))

    def embed_waveform(
        self, waveform: np.ndarray, sample_rate: int = SAMPLE_RATE
    ) -> Embedding:
        """Embed an in-memory mono waveform already at the model sample rate."""
        audio = np.asarray(waveform, dtype=np.float32).reshape(-1)
        return self._embed(
            np.asarray(
                self._inference(
                    {
                        "waveform": torch.from_numpy(audio).unsqueeze(0),
                        "sample_rate": sample_rate,
                    }
                )
            )
        )

    def embed_cluster(self, members: Iterable[Embedding | np.ndarray]) -> Embedding:
        """Canonical voiceprint for a cluster: the re-normalized mean of its members."""
        vectors = [_vector_of(member).reshape(-1) for member in members]
        if not vectors:
            raise ValueError(
                "cannot build a cluster embedding from an empty member list"
            )
        for vector in vectors:
            if vector.shape != (EMBEDDING_DIM,):
                raise ValueError(
                    f"member vector has shape {vector.shape}, expected ({EMBEDDING_DIM},)"
                )
        return self._embed(np.mean(np.stack(vectors), axis=0))

    def _embed(self, raw: np.ndarray) -> Embedding:
        vector = np.asarray(raw, dtype=np.float32).reshape(-1)
        if vector.shape != (EMBEDDING_DIM,):
            raise ValueError(
                f"identity model returned shape {vector.shape}, expected ({EMBEDDING_DIM},)"
            )
        return Embedding(vector=_l2_normalize(vector))
