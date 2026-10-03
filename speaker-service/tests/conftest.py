"""Shared fixtures for the HTTP API tests.

The app is built through ``app.create_app`` with every seam injected: the
registry lives in a per-test file, the meeting runner is a deterministic fake
(no ASR / diarization models), and the identity embedder plus audio loader are
fakes. The service token is a stub handed to ``create_app``, so no test ever
reads the Keychain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pipeline
from app import create_app
from config import Settings
from registry import ClusterLink, JobState, NewSegment, Registry, open_registry

TOKEN = "test-service-token"
ORIGIN = "http://127.0.0.1:3910"
MODEL_ID = "pyannote/wespeaker-voxceleb-resnet34-LM"
REVISION = "837717ddb9ff5507820346191109dc79c958d614"
DIM = 256


@dataclass(frozen=True, slots=True)
class FakeEmbedding:
    """Structural stand-in for ``embed.Embedding`` (no torch import)."""

    vector: np.ndarray
    model_id: str = MODEL_ID
    revision: str = REVISION
    dim: int = DIM


def basis(index: int, weight: float = 1.0) -> np.ndarray:
    """A 256-d unit vector: ``weight`` on axis ``index``, the rest on axis 0."""
    vector = np.zeros(DIM, dtype=np.float32)
    vector[index] = weight
    if index != 0:
        vector[0] = float(np.sqrt(max(0.0, 1.0 - weight * weight)))
    return vector


class FakeEmbedder:
    """Deterministic identity embedder: one axis per speaker (no torch)."""

    def __init__(
        self,
        file_axis: int = 1,
        positive_axis: int = 1,
        negative_axis: int = 2,
    ) -> None:
        self._file_axis = file_axis
        self._positive_axis = positive_axis
        self._negative_axis = negative_axis

    def embed_file(self, path: str | Path) -> FakeEmbedding:
        return FakeEmbedding(vector=basis(self._file_axis))

    def embed_waveform(
        self, waveform: np.ndarray, sample_rate: int = 16_000
    ) -> FakeEmbedding:
        axis = (
            self._positive_axis
            if float(np.mean(waveform)) >= 0.0
            else self._negative_axis
        )
        return FakeEmbedding(vector=basis(axis))

    def embed_cluster(self, members: list[FakeEmbedding]) -> FakeEmbedding:
        mean = np.mean(np.stack([member.vector for member in members]), axis=0)
        return FakeEmbedding(vector=(mean / np.linalg.norm(mean)).astype(np.float32))


def fake_runner(path: Path, title: str, registry: Registry) -> pipeline.MeetingResult:
    """Persist one meeting, one done job, one unknown cluster, two segments."""
    meeting_id = registry.create_meeting(title, audio_path=str(path))
    job_id = registry.create_job(meeting_id, JobState.QUEUED)
    registry.update_job(job_id, JobState.RUNNING)
    cluster_id = registry.add_cluster(meeting_id)
    registry.add_meeting_speaker(
        ClusterLink(meeting_id=meeting_id, cluster_id=cluster_id)
    )
    first = registry.add_segment(
        meeting_id, NewSegment(0.0, 1.0, "hello", None, cluster_id)
    )
    second = registry.add_segment(
        meeting_id, NewSegment(1.0, 2.0, "world", None, cluster_id)
    )
    batch_id = registry.create_speaker_batch(meeting_id)
    registry.add_batch_item(batch_id, cluster_id)
    registry.update_job(job_id, JobState.DONE)
    return pipeline.MeetingResult(
        meeting_id=meeting_id,
        job_id=job_id,
        title=title,
        audio_path=str(path),
        language="en",
        segments=(
            pipeline.SegmentResult(first, 0.0, 1.0, "hello", "unknown", cluster_id),
            pipeline.SegmentResult(second, 1.0, 2.0, "world", "unknown", cluster_id),
        ),
        unknown_clusters=(
            pipeline.UnknownCluster(
                cluster_id,
                "SPEAKER_00",
                "unknown",
                "no voiceprints enrolled",
                2,
                0.0,
                2.0,
            ),
        ),
        match_threshold=0.641,
        batch_id=batch_id,
    )


def fake_loader(path: Path) -> tuple[np.ndarray, int]:
    """One second of positive mono audio at the model sample rate."""
    return np.full(32_000, 0.5, dtype=np.float32), 16_000


class FakeSummaryBackend:
    """A deterministic summarizer; no Ollama needed."""

    def generate(self, *, system: str, user: str) -> str:
        return '{"tldr":"fake summary","decisions":[],"action_items":[],"chapters":[]}'


@pytest.fixture
def service_token() -> str:
    return TOKEN


@pytest.fixture
def origin() -> str:
    return ORIGIN


@pytest.fixture
def auth(service_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {service_token}"}


@pytest.fixture
def embedder_class() -> type[FakeEmbedder]:
    return FakeEmbedder


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def make_app(
    db_path: Path, tmp_path: Path, service_token: str
) -> Callable[..., FastAPI]:
    """Build the app with fake seams; override any seam by keyword."""

    def build(**overrides: Any) -> FastAPI:
        settings = overrides.pop("settings", Settings(data_dir=tmp_path, token=None))
        return create_app(
            settings=settings,
            registry_factory=overrides.pop(
                "registry_factory", lambda: open_registry(db_path)
            ),
            runner=overrides.pop("runner", fake_runner),
            embedder_factory=overrides.pop("embedder_factory", lambda: FakeEmbedder()),
            audio_loader=overrides.pop("audio_loader", fake_loader),
            read_token=overrides.pop("read_token", lambda: service_token),
            summarizer=overrides.pop("summarizer", FakeSummaryBackend()),
            **overrides,
        )

    return build


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> TestClient:
    return TestClient(make_app())


@pytest.fixture
def create_speaker(auth: dict[str, str]) -> Callable[..., dict[str, Any]]:
    def create(
        client: TestClient,
        name: str,
        organization: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        response = client.post(
            "/speakers",
            headers=auth,
            json={"name": name, "organization": organization, "notes": notes},
        )
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
def upload_meeting(auth: dict[str, str]) -> Callable[..., dict[str, Any]]:
    def upload(
        client: TestClient, name: str = "meeting.wav", title: str = "Meeting"
    ) -> dict[str, Any]:
        response = client.post(
            "/meetings",
            headers=auth,
            files={"file": (name, b"RIFF-fake-meeting-audio", "audio/wav")},
            data={"title": title},
        )
        assert response.status_code == 201, response.text
        return response.json()

    return upload
