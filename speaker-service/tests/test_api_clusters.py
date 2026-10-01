"""Cluster split tests.

Given: a meeting uploaded through the API whose cluster holds the segments
``[0,1)`` and ``[1,2)``.
When: the split route moves the tail into a new cluster.
Then: both sides stay non-empty, the new cluster is unknown, and split points
that would leave one side empty are rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_split_moves_the_tail_into_a_new_unknown_cluster(
    client: TestClient,
    auth: dict[str, str],
    upload_meeting: Callable[..., dict[str, Any]],
) -> None:
    # Given: a meeting whose cluster holds segments [0,1) and [1,2)
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    route = f"/meetings/{meeting['meeting_id']}/clusters/{cluster_id}/split"

    # When: the cluster is split at the boundary before the second segment
    response = client.post(route, headers=auth, json={"at": 1.0})

    # Then: each side holds one segment; the new cluster is unknown
    assert response.status_code == 200
    new_cluster = response.json()["new_cluster_id"]
    assert new_cluster != cluster_id
    assert response.json()["moved_segments"] == 1
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    by_id = {cluster["cluster_id"]: cluster for cluster in detail["unknown_clusters"]}
    assert by_id[cluster_id]["segment_count"] == 1
    assert by_id[new_cluster]["segment_count"] == 1
    assert by_id[new_cluster]["start"] == pytest.approx(1.0)
    assert by_id[new_cluster]["speaker_id"] is None

    # And: split points that leave one side empty, or unknown clusters, fail
    assert client.post(route, headers=auth, json={"at": 5.0}).status_code == 400
    assert client.post(route, headers=auth, json={"at": 0.0}).status_code == 400
    assert (
        client.post(
            f"/meetings/999/clusters/{cluster_id}/split",
            headers=auth,
            json={"at": 1.0},
        ).status_code
        == 404
    )
