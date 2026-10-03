"""Batch read + resolve endpoints for the speaker-naming handoff.

Given: the service app with fake seams and a meeting whose fake pipeline left an
unknown cluster and an open speaker batch.
When: the batch is read, then resolved with enroll (remember and not), attach and
skip decisions.
Then: the batch closes, the transcript resolves the cluster, and a
``remember=false`` enrollment stores no voiceprint (label-only for this meeting).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from registry import open_registry


def _batch_id(client: TestClient, auth: dict[str, str], meeting: dict[str, Any]) -> int:
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["handoff"]["needed"] is True
    return detail["handoff"]["batch_id"]


def test_batch_read_lists_items(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    body = client.get(f"/speaker-batches/{batch_id}", headers=auth).json()
    assert body["batch"]["state"] == "open"
    item = body["items"][0]
    assert item["cluster_id"] == meeting["unknown_clusters"][0]["cluster_id"]
    assert item["sample_url"].endswith("/sample")


def test_resolve_enroll_remember_creates_speaker_with_voiceprint(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    response = client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={
            "items": [
                {
                    "cluster_id": cluster_id,
                    "action": "enroll",
                    "name": "张三",
                    "organization": "某某科技",
                    "title": "产品总监",
                    "remember": True,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["resolved"] == 1
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["handoff"]["needed"] is False
    assert {segment["speaker_name"] for segment in detail["segments"]} == {"张三"}
    speakers = client.get("/speakers", headers=auth).json()["speakers"]
    assert speakers[0]["title"] == "产品总监"
    assert speakers[0]["voiceprint_count"] == 1


def test_resolve_enroll_without_remember_stores_no_voiceprint(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    response = client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={
            "items": [{"cluster_id": cluster_id, "action": "enroll", "name": "李四"}]
        },
    )
    assert response.status_code == 200, response.text
    speakers = client.get("/speakers", headers=auth).json()["speakers"]
    assert speakers[0]["name"] == "李四"
    assert speakers[0]["voiceprint_count"] == 0


def test_resolve_skip_closes_batch_and_leaves_cluster_unknown(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={"items": [{"cluster_id": cluster_id, "action": "skip"}]},
    )
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["handoff"]["needed"] is False
    assert detail["unknown_clusters"]


def test_unknown_batch_is_404(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/speaker-batches/999", headers=auth).status_code == 404
    assert (
        client.post(
            "/speaker-batches/999/resolve", headers=auth, json={"items": []}
        ).status_code
        == 404
    )


def test_resolve_rejects_a_cluster_not_in_the_batch(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: two meetings, each with its own unknown cluster
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    other = upload_meeting(client, title="Other")
    other_cluster = other["unknown_clusters"][0]["cluster_id"]
    # When/Then: resolving the batch with a foreign cluster is refused
    response = client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={"items": [{"cluster_id": other_cluster, "action": "skip"}]},
    )
    assert response.status_code == 400


def test_resolve_empty_items_keeps_batch_open(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: an open batch
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    # When: resolve is called with no decisions
    response = client.post(
        f"/speaker-batches/{batch_id}/resolve", headers=auth, json={"items": []}
    )
    # Then: nothing is resolved and the batch stays open
    assert response.status_code == 200
    assert response.json()["resolved"] == 0
    assert response.json()["remaining"] >= 1
    assert response.json()["batch"]["state"] == "open"


def test_resolve_remember_true_records_a_batch_consent(
    client: TestClient,
    auth: dict[str, str],
    upload_meeting: Callable[..., dict],
    db_path,
) -> None:
    # Given: an open batch
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    # When: a cluster is enrolled with remember=true
    client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={
            "items": [
                {
                    "cluster_id": cluster_id,
                    "action": "enroll",
                    "name": "孙八",
                    "remember": True,
                }
            ]
        },
    )
    # Then: exactly one consent row exists, linked to this batch
    speaker = client.get("/speakers", headers=auth).json()["speakers"][0]
    with open_registry(db_path) as registry:
        consents = registry.consents_for_speaker(speaker["id"])
        assert len(consents) == 1
        assert consents[0].source_batch_id == batch_id


def test_resolve_remember_false_records_no_consent(
    client: TestClient,
    auth: dict[str, str],
    upload_meeting: Callable[..., dict],
    db_path,
) -> None:
    # Given: an open batch
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    # When: a cluster is enrolled with remember=false (label-only)
    client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={
            "items": [{"cluster_id": cluster_id, "action": "enroll", "name": "周九"}]
        },
    )
    # Then: no voiceprint and no consent row exist
    speaker = client.get("/speakers", headers=auth).json()["speakers"][0]
    assert speaker["voiceprint_count"] == 0
    with open_registry(db_path) as registry:
        assert registry.consents_for_speaker(speaker["id"]) == []
