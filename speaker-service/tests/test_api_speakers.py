"""Behavior tests for the speaker routes: CRUD, attach and merge.

Given: the service app with fake seams and a registry that gains speakers,
meetings and unknown clusters through the API.
When: speaker routes are exercised over HTTP with a bearer token.
Then: edits only touch provided fields, deletion applies the documented
ON DELETE policy (voiceprints purged, references nulled), attaching a cluster
to an existing speaker never creates a duplicate, and merging moves every
reference to the target before dropping the duplicate.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from registry import ClusterState, open_registry


def test_patch_updates_only_the_provided_fields(
    client: TestClient,
    auth: dict[str, str],
    create_speaker: Callable[..., dict[str, Any]],
) -> None:
    # Given: a speaker with every optional field filled
    speaker = create_speaker(client, "Ada", "ACME", "first")

    # When: only the organization is patched
    response = client.patch(
        f"/speakers/{speaker['id']}", headers=auth, json={"organization": "NewCo"}
    )

    # Then: the other fields are untouched
    assert response.status_code == 200
    assert response.json()["organization"] == "NewCo"
    assert response.json()["name"] == "Ada"
    assert response.json()["notes"] == "first"

    # And: a blank name is refused and unknown speakers are 404
    blank = client.patch(
        f"/speakers/{speaker['id']}", headers=auth, json={"name": "  "}
    )
    assert blank.status_code == 400
    assert (
        client.patch("/speakers/999", headers=auth, json={"name": "X"}).status_code
        == 404
    )


def test_delete_speaker_purges_voiceprints_and_references(
    client: TestClient,
    auth: dict[str, str],
    db_path: Path,
    create_speaker: Callable[..., dict[str, Any]],
    upload_meeting: Callable[..., dict[str, Any]],
) -> None:
    # Given: a speaker with a voiceprint attached to a meeting's cluster
    speaker = create_speaker(client, "Ada")
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    attached = client.post(
        f"/speakers/{speaker['id']}/voiceprints",
        headers=auth,
        json={"meeting_id": meeting["meeting_id"], "cluster_id": cluster_id},
    )
    assert attached.status_code == 201

    # When: the speaker is deleted through the API
    deleted = client.delete(f"/speakers/{speaker['id']}", headers=auth)

    # Then: the biometric voiceprints are purged and references nulled
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "speaker_id": speaker["id"]}
    with open_registry(db_path) as registry:
        assert registry.get_speaker(speaker["id"]) is None
        assert registry.voiceprints_for_speaker(speaker["id"]) == []
        links = registry.meeting_speakers_for_meeting(meeting["meeting_id"])
        assert [link.speaker_id for link in links] == [None]
        segments = registry.segments_for_meeting(meeting["meeting_id"])
        assert [segment.speaker_id for segment in segments] == [None, None]
        clusters = {
            cluster.id: cluster
            for cluster in registry.clusters_for_meeting(meeting["meeting_id"])
        }
        assert clusters[cluster_id].state is ClusterState.UNKNOWN
        assert clusters[cluster_id].label is None

    # And: the directory is empty; deleting a missing speaker is 404
    assert client.get("/speakers", headers=auth).json()["speakers"] == []
    assert client.delete("/speakers/999", headers=auth).status_code == 404


def test_attach_voiceprint_never_creates_a_duplicate_speaker(
    client: TestClient,
    auth: dict[str, str],
    db_path: Path,
    create_speaker: Callable[..., dict[str, Any]],
    upload_meeting: Callable[..., dict[str, Any]],
) -> None:
    # Given: an existing speaker and a meeting whose cluster is unknown
    speaker = create_speaker(client, "Ada")
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]

    # When: the cluster's voiceprint is attached to the existing speaker
    attached = client.post(
        f"/speakers/{speaker['id']}/voiceprints",
        headers=auth,
        json={"meeting_id": meeting["meeting_id"], "cluster_id": cluster_id},
    )

    # Then: the cluster is labelled with the existing name, no duplicate row
    assert attached.status_code == 201
    assert attached.json()["voiceprint_id"] > 0
    assert attached.json()["similarity"] is None
    assert [
        item["name"]
        for item in client.get("/speakers", headers=auth).json()["speakers"]
    ] == ["Ada"]
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["unknown_clusters"] == []
    assert {segment["speaker_name"] for segment in detail["segments"]} == {"Ada"}

    # And: a second cluster attached to the same speaker reports its cosine
    # against the enrolled voiceprint instead of creating a new identity
    second = upload_meeting(client, title="Second")
    again = client.post(
        f"/speakers/{speaker['id']}/voiceprints",
        headers=auth,
        json={
            "meeting_id": second["meeting_id"],
            "cluster_id": second["unknown_clusters"][0]["cluster_id"],
        },
    )
    assert again.status_code == 201
    assert again.json()["similarity"] == pytest.approx(1.0)
    with open_registry(db_path) as registry:
        assert len(registry.voiceprints_for_speaker(speaker["id"])) == 2
    assert len(client.get("/speakers", headers=auth).json()["speakers"]) == 1

    # And: unknown speakers and clusters are 404
    assert (
        client.post(
            "/speakers/999/voiceprints",
            headers=auth,
            json={"meeting_id": meeting["meeting_id"], "cluster_id": cluster_id},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/speakers/{speaker['id']}/voiceprints",
            headers=auth,
            json={"meeting_id": meeting["meeting_id"], "cluster_id": 999},
        ).status_code
        == 404
    )


def test_merge_moves_every_reference_to_the_target(
    client: TestClient,
    auth: dict[str, str],
    db_path: Path,
    create_speaker: Callable[..., dict[str, Any]],
    upload_meeting: Callable[..., dict[str, Any]],
) -> None:
    # Given: two identities where the source owns a voiceprint, a link and
    # the meeting's segments
    target = create_speaker(client, "Ada")
    source = create_speaker(client, "Ada Duplicate")
    meeting = upload_meeting(client)
    client.post(
        f"/speakers/{source['id']}/voiceprints",
        headers=auth,
        json={
            "meeting_id": meeting["meeting_id"],
            "cluster_id": meeting["unknown_clusters"][0]["cluster_id"],
        },
    )

    # When: the duplicate is merged into the target
    response = client.post(
        f"/speakers/{target['id']}/merge",
        headers=auth,
        json={"source_speaker_id": source["id"]},
    )

    # Then: voiceprints, meeting links and segments all now point at the
    # target, and the duplicate row is gone
    assert response.status_code == 200
    assert response.json() == {
        "target_speaker_id": target["id"],
        "source_speaker_id": source["id"],
        "voiceprints_moved": 1,
        "links_moved": 1,
        "segments_moved": 2,
    }
    assert [
        item["name"]
        for item in client.get("/speakers", headers=auth).json()["speakers"]
    ] == ["Ada"]
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert {segment["speaker_name"] for segment in detail["segments"]} == {"Ada"}
    with open_registry(db_path) as registry:
        assert len(registry.voiceprints_for_speaker(target["id"])) == 1
        assert registry.voiceprints_for_speaker(source["id"]) == []
        links = registry.meeting_speakers_for_meeting(meeting["meeting_id"])
        assert [link.speaker_id for link in links] == [target["id"]]
        segments = registry.segments_for_meeting(meeting["meeting_id"])
        assert [segment.speaker_id for segment in segments] == [target["id"]] * 2

    # And: merging a speaker into itself is 400, a missing source is 404
    assert (
        client.post(
            f"/speakers/{target['id']}/merge",
            headers=auth,
            json={"source_speaker_id": target["id"]},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/speakers/{target['id']}/merge",
            headers=auth,
            json={"source_speaker_id": 999},
        ).status_code
        == 404
    )
