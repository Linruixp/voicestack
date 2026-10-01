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

from config import Settings

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
