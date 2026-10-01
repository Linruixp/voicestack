"""Identification tests.

Given: Ada enrolled through an attached meeting cluster and probe clips.
When: ``POST /identify`` receives a clip path and a multipart upload.
Then: the enrolled name is returned, a clip matching nobody is ``unknown``
(never a false name), a missing file is 404, and the route demands auth.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_identify_returns_best_match_or_unknown(
    client: TestClient,
    auth: dict[str, str],
    create_speaker: Callable[..., dict[str, Any]],
    upload_meeting: Callable[..., dict[str, Any]],
    embedder_class: type,
    make_app: Callable[..., Any],
    service_token: str,
    tmp_path: Path,
) -> None:
    # Given: Ada enrolled through an attached meeting cluster
    speaker = create_speaker(client, "Ada")
    meeting = upload_meeting(client)
    client.post(
        f"/speakers/{speaker['id']}/voiceprints",
        headers=auth,
        json={
            "meeting_id": meeting["meeting_id"],
            "cluster_id": meeting["unknown_clusters"][0]["cluster_id"],
        },
    )
    probe = tmp_path / "probe.wav"
    probe.write_bytes(b"RIFF-probe-audio")

    # When: a matching clip arrives as a path and as an uploaded clip
    by_path = client.post("/identify", headers=auth, json={"audio_path": str(probe)})
    by_upload = client.post(
        "/identify",
        headers=auth,
        files={"file": ("probe.wav", b"RIFF-probe-audio", "audio/wav")},
    )

    # Then: the enrolled speaker is the best match
    assert by_path.status_code == 200
    assert by_path.json()["status"] == "known"
    assert by_path.json()["name"] == "Ada"
    assert by_path.json()["similarity"] == pytest.approx(1.0)
    assert by_upload.status_code == 200
    assert by_upload.json()["name"] == "Ada"

    # And: a clip matching nobody is unknown, never a false name
    stranger_app = make_app(embedder_factory=lambda: embedder_class(file_axis=3))
    stranger = TestClient(stranger_app).post(
        "/identify",
        headers={"Authorization": f"Bearer {service_token}"},
        json={"audio_path": str(probe)},
    )
    assert stranger.status_code == 200
    assert stranger.json()["status"] == "unknown"
    assert stranger.json()["name"] is None

    # And: a missing file is 404 and the route demands auth
    missing = client.post(
        "/identify", headers=auth, json={"audio_path": str(tmp_path / "nope.wav")}
    )
    assert missing.status_code == 404
    assert client.post("/identify", json={"audio_path": str(probe)}).status_code == 401
