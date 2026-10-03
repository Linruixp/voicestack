"""Acceptance tests for the token-guarded, loopback-only service API.

Given: the speaker-service app on a private registry with deterministic fake
stages (no models) and a stubbed service token.
When: clients call the API with no credentials, a bearer token, or the UI
session cookie; upload a meeting; and probe the network bind directly.
Then: mutating routes demand auth (401), the bearer token and the UI session
cookie authorize, CORS stays disabled, the served page never contains the
token, uploaded audio is deleted when configured, and the LAN address cannot
reach the loopback-only listener.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

import asr
from config import Settings
from registry import open_registry

TOKEN_HEADER = "Authorization"


def test_health_is_an_unauthenticated_readiness_probe(client: TestClient) -> None:
    # When: a launcher probes readiness without credentials
    response = client.get("/health")
    # Then: it succeeds with the minimal contract the launchers expect
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mutating_routes_reject_missing_and_wrong_tokens(client: TestClient) -> None:
    # When: mutations arrive with no credentials, a wrong token, or no auth
    missing = client.post("/speakers", json={"name": "No Auth"})
    wrong = client.post(
        "/speakers",
        headers={TOKEN_HEADER: "Bearer nope"},
        json={"name": "Wrong Auth"},
    )
    # Then: every one is refused before touching the registry
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert client.get("/speakers").status_code == 401


def test_bearer_token_authorizes_mutations_and_reads(
    client: TestClient, auth: dict[str, str]
) -> None:
    # When: the same calls carry the service bearer token
    created = client.post("/speakers", headers=auth, json={"name": "Ada"})
    listing = client.get("/speakers", headers=auth)
    # Then: they succeed and the registry reflects the write
    assert created.status_code == 201
    assert created.json()["name"] == "Ada"
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["speakers"]] == ["Ada"]
    assert listing.json()["speakers"][0]["voiceprint_count"] == 0


def test_cors_stays_disabled_on_every_probe(client: TestClient) -> None:
    # When: a foreign origin probes the API (simple request and preflight)
    simple = client.get("/health", headers={"Origin": "http://evil.example"})
    preflight = client.options(
        "/speakers",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    # Then: no CORS header is ever emitted
    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers


def test_ui_session_cookie_authorizes_mutation_with_loopback_origin(
    client: TestClient, service_token: str, origin: str
) -> None:
    # Given: the UI root is loaded once (first page load mints a session)
    home = client.get("/")
    assert home.status_code == 200
    cookie = home.headers["set-cookie"].lower()
    assert "vs_session=" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie
    # And: the bearer token is never embedded in the served page
    assert service_token not in home.text

    # When: a cookie-authenticated mutation has no Origin, or a foreign one
    no_origin = client.post("/speakers", json={"name": "Cookie A"})
    evil = client.post(
        "/speakers",
        headers={"Origin": "http://evil.example"},
        json={"name": "Cookie B"},
    )
    # Then: both are refused (CSRF guard) and nothing was written
    assert no_origin.status_code == 401
    assert evil.status_code == 401
    assert client.get("/speakers").json()["speakers"] == []

    # When: the cookie is presented with the loopback Origin
    allowed = client.post(
        "/speakers", headers={"Origin": origin}, json={"name": "Cookie C"}
    )
    # Then: the session cookie authorizes the mutation
    assert allowed.status_code == 201
    assert [item["name"] for item in client.get("/speakers").json()["speakers"]] == [
        "Cookie C"
    ]


def test_upload_creates_meeting_job_and_transcript_view(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # When: a meeting recording is uploaded
    result = upload_meeting(client, title="Standup")

    # Then: the response carries the meeting, its job, transcript and clusters
    meeting_id, job_id = result["meeting_id"], result["job_id"]
    assert result["title"] == "Standup"
    assert [segment["text"] for segment in result["segments"]] == ["hello", "world"]

    # And: the meeting list and transcript view expose the persisted meeting
    assert [
        item["id"] for item in client.get("/meetings", headers=auth).json()["meetings"]
    ] == [meeting_id]
    detail = client.get(f"/meetings/{meeting_id}", headers=auth).json()
    assert detail["meeting"]["title"] == "Standup"
    assert [segment["text"] for segment in detail["segments"]] == ["hello", "world"]
    assert detail["segments"][0]["speaker_name"] is None
    assert (
        detail["unknown_clusters"][0]["cluster_id"]
        == (result["unknown_clusters"][0]["cluster_id"])
    )
    assert detail["unknown_clusters"][0]["segment_count"] == 2

    # And: the job reports done; unknown ids are 404
    assert client.get(f"/jobs/{job_id}", headers=auth).json()["state"] == "done"
    assert client.get("/meetings/999", headers=auth).status_code == 404
    assert client.get("/jobs/999", headers=auth).status_code == 404


def test_patch_meeting_edits_title_and_metadata(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: a persisted meeting whose title defaulted from the upload
    meeting_id = upload_meeting(client, title="Standup")["meeting_id"]

    # When: the title and metadata are patched
    response = client.patch(
        f"/meetings/{meeting_id}",
        headers=auth,
        json={"title": "Weekly sync", "location": "Room 3", "topic": "Roadmap"},
    )

    # Then: the payload reflects the edits and retains the original title
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Weekly sync"
    assert body["location"] == "Room 3"
    assert body["topic"] == "Roadmap"
    assert body["original_title"] == "Standup"

    # And: unknown ids are 404 and a blank title is refused
    assert (
        client.patch("/meetings/999", headers=auth, json={"title": "x"}).status_code
        == 404
    )
    assert (
        client.patch(
            f"/meetings/{meeting_id}", headers=auth, json={"title": "  "}
        ).status_code
        == 400
    )


@pytest.mark.parametrize("delete_after", [True, False], ids=["switch-on", "switch-off"])
def test_uploaded_audio_is_deleted_after_job_only_when_enabled(
    make_app: Callable[..., FastAPI],
    service_token: str,
    tmp_path: Path,
    delete_after: bool,
) -> None:
    # Given: the delete-after-transcribe switch is on (or off, the default)
    app = make_app(
        settings=Settings(data_dir=tmp_path, delete_audio_after_transcribe=delete_after)
    )
    client = TestClient(app)

    # When: a meeting is uploaded and its job completes
    response = client.post(
        "/meetings",
        headers={TOKEN_HEADER: f"Bearer {service_token}"},
        files={"file": ("privacy.wav", b"RIFF-fake-audio", "audio/wav")},
        data={"title": "Private"},
    )

    # Then: the uploaded copy is gone only when the switch is enabled
    assert response.status_code == 201
    audio = Path(response.json()["audio_path"])
    assert audio.exists() is not delete_after
    # And: the audio route 404s once the file is gone
    stream = client.get(
        f"/meetings/{response.json()['meeting_id']}/audio",
        headers={TOKEN_HEADER: f"Bearer {service_token}"},
    )
    assert stream.status_code == (404 if delete_after else 200)


def test_non_audio_upload_gets_4xx_with_no_meeting_job_or_retained_file(
    client: TestClient, auth: dict[str, str], db_path: Path, tmp_path: Path
) -> None:
    # When: a text file is posted to the meeting upload endpoint
    response = client.post(
        "/meetings",
        headers=auth,
        files={"file": ("meeting-notes.txt", b"not audio", "text/plain")},
        data={"title": "Junk"},
    )

    # Then: it is refused with a typed 4xx and an actionable detail - not a 500
    assert response.status_code == 415
    assert "audio" in response.json()["detail"]

    # And: exactly zero meetings, zero jobs and no upload file were left behind
    with open_registry(db_path) as registry:
        assert registry.list_meetings() == []
        assert registry.get_job(1) is None
    assert list((tmp_path / "uploads").glob("*")) == []


def test_undecodable_upload_returns_422_and_deletes_the_stored_file(
    make_app: Callable[..., FastAPI], service_token: str, tmp_path: Path
) -> None:
    # Given: a runner that reports the upload is not decodable audio
    def runner(path: Path, title: str, registry: object) -> None:
        raise asr.InvalidAudioError(f"{path} is not decodable audio")

    client = TestClient(make_app(runner=runner))

    # When: an audio-named upload reaches the pipeline
    response = client.post(
        "/meetings",
        headers={TOKEN_HEADER: f"Bearer {service_token}"},
        files={"file": ("broken.wav", b"RIFF-broken", "audio/wav")},
        data={"title": "Broken"},
    )

    # Then: it is a typed 422 and the temporarily stored upload is gone
    assert response.status_code == 422
    assert "decodable audio" in response.json()["detail"]
    assert list((tmp_path / "uploads").glob("*")) == []


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _lan_address() -> str | None:
    """The host's LAN IPv4 via a UDP route lookup (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1
        except OSError:
            return None
        return str(sock.getsockname()[0])


def test_lan_address_cannot_connect_to_the_loopback_bind(
    make_app: Callable[..., FastAPI],
) -> None:
    # Given: the service is served on 127.0.0.1 only
    port = _free_loopback_port()
    server = uvicorn.Server(
        uvicorn.Config(
            make_app(),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"

        # Then: loopback answers ...
        assert (
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200
        )

        # And: the same port on the LAN address does not accept connections
        lan = _lan_address()
        if lan is None or lan.startswith("127."):
            pytest.skip("no LAN address on this host")
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://{lan}:{port}/health", timeout=2)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_meeting_list_search_and_filters(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: one uploaded meeting titled 项目规划
    meeting = upload_meeting(client, title="项目规划")

    # Then: the list row carries duration/participants/unknown_count
    row = client.get("/meetings", headers=auth).json()["meetings"][0]
    assert row["title"] == "项目规划"
    assert "participants" in row and "unknown_count" in row

    # And: title substring search and the unresolved filter both match it
    assert [
        m["id"] for m in client.get("/meetings?q=项目", headers=auth).json()["meetings"]
    ] == [meeting["meeting_id"]]
    assert [
        m["id"]
        for m in client.get("/meetings?unresolved=1", headers=auth).json()["meetings"]
    ] == [meeting["meeting_id"]]
    assert client.get("/meetings?q=zzz", headers=auth).json()["meetings"] == []


def test_generate_summary_endpoint(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: a meeting uploaded through the fake pipeline
    meeting = upload_meeting(client)

    # When: a summary is generated
    response = client.post(f"/meetings/{meeting['meeting_id']}/summary", headers=auth)

    # Then: the structured summary is returned and exposed on the meeting
    assert response.status_code == 200, response.text
    assert response.json()["tldr"] == "fake summary"
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["summary"]["tldr"] == "fake summary"


class _BadSummaryBackend:
    def generate(self, *, system: str, user: str) -> str:
        return "not json"


class _ChineseSummaryBackend:
    def generate(self, *, system: str, user: str) -> str:
        return '{"tldr":"讨论了排期","decisions":[],"action_items":[],"chapters":[]}'


def test_stream_meeting_audio_and_edit_segment(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: an uploaded meeting
    meeting = upload_meeting(client)

    # Then: its audio streams and one segment's text can be corrected
    audio = client.get(f"/meetings/{meeting['meeting_id']}/audio", headers=auth)
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/")
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    segment_id = detail["segments"][0]["id"]
    patched = client.patch(
        f"/meetings/{meeting['meeting_id']}/segments/{segment_id}",
        headers=auth,
        json={"text": "更正后的文字"},
    )
    assert patched.status_code == 200
    assert patched.json()["text"] == "更正后的文字"
    after = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert after["segments"][0]["text"] == "更正后的文字"

    # And: unknown meeting / segment are 404
    assert client.get("/meetings/999/audio", headers=auth).status_code == 404
    assert (
        client.patch(
            f"/meetings/{meeting['meeting_id']}/segments/999",
            headers=auth,
            json={"text": "x"},
        ).status_code
        == 404
    )

    # And: blank text is 400, unauthenticated audio is 401, Range is 206
    assert (
        client.patch(
            f"/meetings/{meeting['meeting_id']}/segments/{segment_id}",
            headers=auth,
            json={"text": "   "},
        ).status_code
        == 400
    )
    assert client.get(f"/meetings/{meeting['meeting_id']}/audio").status_code == 401
    ranged = client.get(
        f"/meetings/{meeting['meeting_id']}/audio",
        headers={**auth, "Range": "bytes=0-3"},
    )
    assert ranged.status_code == 206
    assert "content-range" in {key.lower() for key in ranged.headers}

    # And: a segment belonging to another meeting is 404 under this meeting
    other = upload_meeting(client, title="Other")
    other_segment = client.get(f"/meetings/{other['meeting_id']}", headers=auth).json()[
        "segments"
    ][0]["id"]
    assert (
        client.patch(
            f"/meetings/{meeting['meeting_id']}/segments/{other_segment}",
            headers=auth,
            json={"text": "x"},
        ).status_code
        == 404
    )


def test_summary_error_paths_and_chinese_search(
    make_app: Callable[..., FastAPI], auth: dict[str, str]
) -> None:
    # 503: explicitly disabled summarizer
    disabled = TestClient(make_app(summarizer=None))
    assert disabled.post("/meetings/1/summary", headers=auth).status_code == 503

    # 404: unknown meeting
    default = TestClient(make_app())
    assert default.post("/meetings/999/summary", headers=auth).status_code == 404

    # 502: the backend returns non-JSON
    bad = TestClient(make_app(summarizer=_BadSummaryBackend()))
    created = bad.post(
        "/meetings",
        headers=auth,
        files={"file": ("m.wav", b"RIFF-fake", "audio/wav")},
        data={"title": "Bad"},
    ).json()
    assert (
        bad.post(f"/meetings/{created['meeting_id']}/summary", headers=auth).status_code
        == 502
    )

    # Chinese summary text is searchable (P2b N5)
    cn = TestClient(make_app(summarizer=_ChineseSummaryBackend()))
    created = cn.post(
        "/meetings",
        headers=auth,
        files={"file": ("m.wav", b"RIFF-fake", "audio/wav")},
        data={"title": "中文会"},
    ).json()
    cn.post(f"/meetings/{created['meeting_id']}/summary", headers=auth)
    rows = cn.get("/meetings?q=讨论了排期", headers=auth).json()["meetings"]
    assert [m["id"] for m in rows] == [created["meeting_id"]]


def test_delete_meeting_and_all(
    client: TestClient,
    auth: dict[str, str],
    upload_meeting: Callable[..., dict],
    db_path: Path,
) -> None:
    # Given: two uploaded meetings
    first = upload_meeting(client, title="A")
    second = upload_meeting(client, title="B")

    # When: one is deleted
    assert (
        client.delete(f"/meetings/{first['meeting_id']}", headers=auth).status_code
        == 200
    )

    # Then: it (and its transcript) is gone; the other survives
    assert [
        m["id"] for m in client.get("/meetings", headers=auth).json()["meetings"]
    ] == [second["meeting_id"]]
    with open_registry(db_path) as registry:
        assert registry.segments_for_meeting(first["meeting_id"]) == []
    assert client.delete("/meetings/999", headers=auth).status_code == 404

    # And: delete-all removes the rest
    assert client.delete("/meetings", headers=auth).json()["deleted"] == 1
    assert client.get("/meetings", headers=auth).json()["meetings"] == []


def test_meeting_list_participant_filter(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: a meeting whose cluster is enrolled as a named speaker
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    enrolled = client.post(
        "/speakers/enroll",
        headers=auth,
        json={"name": "参会人A", "cluster_id": cluster_id},
    ).json()

    # Then: filtering by that participant returns only the meeting
    rows = client.get(
        f"/meetings?participant_id={enrolled['speaker_id']}", headers=auth
    ).json()["meetings"]
    assert [m["id"] for m in rows] == [meeting["meeting_id"]]
    assert (
        client.get("/meetings?participant_id=999", headers=auth).json()["meetings"]
        == []
    )


def test_meeting_read_exposes_handoff_and_ui_url(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: an uploaded meeting whose fake pipeline left one unknown cluster
    result = upload_meeting(client, title="Standup")
    # Then: the upload payload advertises the handoff and a UI deep link
    assert result["handoff"]["needed"] is True
    assert isinstance(result["handoff"]["batch_id"], int)
    assert "task=speakers" in result["ui_url"]
    # And: the read view exposes the same handoff
    detail = client.get(f"/meetings/{result['meeting_id']}", headers=auth).json()
    assert detail["handoff"]["needed"] is True
    assert detail["handoff"]["batch_id"] == result["handoff"]["batch_id"]
    assert detail["handoff"]["unknown_count"] >= 1
    assert "task=speakers" in detail["ui_url"]
