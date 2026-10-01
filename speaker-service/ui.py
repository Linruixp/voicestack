"""The minimal local Web UI served by the service.

A single static bundle (``ui_static/``) rendered as five views: upload,
transcript, assign/merge/split, speaker directory, and meeting history. There is
no login page, no theming, and no client-side router: the views are toggled in
one document.

Auth: ``GET /`` mints the same-origin session cookie from :mod:`api_auth`. The
service bearer token is never embedded in any served byte — the page
authenticates with that cookie, and mutations carry the browser's ``Origin``
header for the server's CSRF check. The UI contains no business logic; every
action calls an existing API route.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from api_auth import SESSION_COOKIE

router = APIRouter()

UI_SESSION_MAX_AGE = 86_400
STATIC_DIR = Path(__file__).resolve().parent / "ui_static"


@router.get("/", response_class=HTMLResponse)
def ui_root(request: Request) -> HTMLResponse:
    """Serve the five-view page and mint the UI session cookie."""
    response = HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.sessions.mint(),
        max_age=UI_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response
