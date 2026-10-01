"""Behavior tests for the enrollment + cluster-only attach routes (task 26).

Given: the service app with fake seams and a meeting whose cluster is unknown.
When: a NEW speaker is enrolled by cluster id, or a cluster is attached to an
existing speaker without passing ``meeting_id``.
Then: enrollment is atomic (speaker + voiceprint + labelled cluster) and the
route resolves the meeting from the globally-unique cluster id.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from registry import open_registry


def test_enroll_route_creates_new_speaker_from_cluster(
    client: TestClient,
    auth: dict[str, str],
    db_path: Path,
    upload_meeting: Callable[..., dict[str, Any]],
) -> None:
    # Given: a meeting with one unknown cluster
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]

    # When: a new speaker is enrolled by cluster id alone
    response = client.post(
        "/speakers/enroll",
        headers=auth,
        json={
            "name": "Ada",
            "organization": "ACME",
            "notes": "lead",
            "cluster_id": cluster_id,
        },
    )

    # Then: a new speaker + voiceprint exist and the cluster is named
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["speaker_id"] > 0
    assert body["voiceprint_id"] > 0
    assert body["cluster_id"] == cluster_id
    assert body["name"] == "Ada"
    assert [
        item["name"]
        for item in client.get("/speakers", headers=auth).json()["speakers"]
    ] == ["Ada"]
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["unknown_clusters"] == []
    assert {segment["speaker_name"] for segment in detail["segments"]} == {"Ada"}
    with open_registry(db_path) as registry:
        assert len(registry.voiceprints_for_speaker(body["speaker_id"])) == 1

    # And: a blank name is 400 and an unknown cluster is 404
    assert (
        client.post(
            "/speakers/enroll",
            headers=auth,
            json={"name": "  ", "cluster_id": cluster_id},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/speakers/enroll",
            headers=auth,
            json={"name": "Ada", "cluster_id": 999},
        ).status_code
        == 404
    )


def test_attach_accepts_cluster_without_meeting_id(
    client: TestClient,
    auth: dict[str, str],
    create_speaker: Callable[..., dict[str, Any]],
    upload_meeting: Callable[..., dict[str, Any]],
) -> None:
    # Given: an existing speaker and a meeting with an unknown cluster
    speaker = create_speaker(client, "Ada")
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]

    # When: the voiceprint is attached with only the cluster id
    response = client.post(
        f"/speakers/{speaker['id']}/voiceprints",
        headers=auth,
        json={"cluster_id": cluster_id},
    )

    # Then: the meeting was resolved from the cluster and the attach succeeded
    assert response.status_code == 201, response.text
    assert response.json()["cluster_id"] == cluster_id
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert {segment["speaker_name"] for segment in detail["segments"]} == {"Ada"}

    # And: an unknown cluster is 404, not a silent no-op
    assert (
        client.post(
            f"/speakers/{speaker['id']}/voiceprints",
            headers=auth,
            json={"cluster_id": 999},
        ).status_code
        == 404
    )
