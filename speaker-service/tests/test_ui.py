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


def test_ui_serves_intro_header_and_title_fields(client: TestClient) -> None:
    html = client.get("/").text
    assert 'data-testid="transcript-intro"' in html
    assert 'data-testid="transcript-title"' in html
    assert 'data-testid="enroll-title"' in html


def test_ui_has_wizard_view_without_extra_nav_tab(client: TestClient) -> None:
    html = client.get("/").text
    assert 'data-testid="view-wizard"' in html
    assert html.count("data-view=") == 5  # the wizard is a view, NOT a sixth nav tab


def test_ui_wizard_assets_declare_required_controls(client: TestClient) -> None:
    # Given: the served vanilla bundle
    js = client.get("/static/app.js").text
    # Then: every wizard control the e2e drives is declared
    for testid in (
        "wizard-sample",
        "wizard-action-attach",
        "wizard-action-enroll",
        "wizard-action-skip",
        "wizard-new-name",
        "wizard-new-org",
        "wizard-new-title",
        "wizard-remember",
        "wizard-prev",
        "wizard-next",
        "wizard-submit",
        "wizard-similarity",
    ):
        assert f'data-testid="{testid}"' in js, testid
    # And: the consent checkbox defaults OFF and the submit payload carries the
    # decision through as a boolean (unchecked => remember:false => label-only)
    assert "remember: false" in js
    assert "remember: Boolean(" in js


def test_ui_has_visible_status_and_keyboard_affordances(client: TestClient) -> None:
    # Given: the served bundle
    html = client.get("/").text
    js = client.get("/static/app.js").text
    # Then: rename feedback has visible, live status regions
    assert 'data-testid="meeting-status"' in html
    assert 'aria-live="polite"' in html
    assert 'data-testid="transcript-status"' in js
    # And: inline title editing is keyboard reachable
    assert 'role="button"' in js
    assert 'tabindex="0"' in js


def test_ui_transcript_playback_and_edit(client: TestClient) -> None:
    # Given: the served bundle
    html = client.get("/").text
    js = client.get("/static/app.js").text
    # Then: the transcript has a synced player, chapter seek and inline edit
    assert 'data-testid="transcript-audio"' in html
    assert "/audio" in js
    assert "/segments/" in js
    assert "data-seek" in js


def test_ui_summary_controls(client: TestClient) -> None:
    # Given: the served bundle
    js = client.get("/static/app.js").text
    # Then: the intro can render a summary or offer to generate one
    assert 'data-testid="generate-summary"' in js
    assert 'data-testid="summary-tldr"' in js
    assert "/summary" in js


def test_ui_history_search_and_filters(client: TestClient) -> None:
    # Given: the served bundle
    html = client.get("/").text
    js = client.get("/static/app.js").text
    # Then: the history view exposes search + filters and richer rows
    assert 'data-testid="meeting-search"' in html
    assert 'data-testid="meeting-filter-unresolved"' in html
    assert 'data-testid="meeting-filter-participant"' in html
    assert 'data-testid="meeting-filter-from"' in html
    assert 'data-testid="meeting-filter-to"' in html
    assert "unknown_count" in js
    assert "group-row" in js


def test_ui_speaker_governance_controls(client: TestClient) -> None:
    # Given: the served bundle
    js = client.get("/static/app.js").text
    # Then: the directory exposes consent/retention plus export and re-enroll
    assert 'data-testid="speaker-consent-${s.id}"' in js or "speaker-consent-" in js
    assert "speaker-retention-" in js
    assert "data-export=" in js
    assert "data-reenroll=" in js
