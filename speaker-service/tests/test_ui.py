"""Tests for the minimal five-view Web UI (task 25).

The UI is a static bundle served by the service. These tests pin the contract
that matters for security and scope: the page mints the session cookie, the
five views are present, the static assets are served, and the service bearer
token appears in NO served byte.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

VIEW_IDS = (
    "view-upload",
    "view-transcript",
    "view-assign",
    "view-speakers",
    "view-meetings",
)


def test_ui_root_serves_five_views_and_mints_cookie(
    client: TestClient, service_token: str
) -> None:
    # Given/When: the UI root is loaded
    home = client.get("/")
    # Then: it is the five-view page and mints the HttpOnly session cookie
    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    for view in VIEW_IDS:
        assert view in home.text
    cookie = home.headers["set-cookie"].lower()
    assert "vs_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    # And: no login page and no token in the served page
    assert "login" not in home.text.lower()
    assert service_token not in home.text


def test_ui_static_assets_are_served_without_the_token(
    client: TestClient, service_token: str
) -> None:
    # When: the bundle assets are fetched
    js = client.get("/static/app.js")
    css = client.get("/static/app.css")
    # Then: they are served and never contain the bearer token
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert css.status_code == 200 and "css" in css.headers["content-type"]
    assert service_token not in js.text
    assert service_token not in css.text
    # And: the page authenticates via the cookie, never an Authorization header
    assert "Authorization" not in js.text
    assert "credentials" in js.text


def test_ui_keepalive_targets_health(client: TestClient) -> None:
    # When: the bundle is fetched
    js = client.get("/static/app.js").text
    # Then: it polls the unauthenticated /health endpoint on a low-rate timer
    assert 'fetch("/health"' in js
    assert "setInterval(keepalive" in js
