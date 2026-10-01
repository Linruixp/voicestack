"""Authentication for the localhost HTTP API.

Two credentials are accepted:

* the service **bearer token** (Keychain ``voicestack-service`` or
  ``VASTACK_TOKEN``) for CLI/MCP callers, and
* a server-minted **loopback session cookie** for the web UI, so the token is
  never embedded in served HTML/JS.

The session store is in-memory: a service restart invalidates every UI
session (the browser simply mints a new one on the next page load). Mutating
requests authenticated by cookie additionally require an ``Origin`` header
matching a loopback origin - ``SameSite=Lax`` plus this check is the CSRF
boundary. Bearer-authenticated requests are exempt from the Origin check
because a cross-origin page cannot attach an ``Authorization`` header without
a CORS preflight, and no CORS middleware is installed.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Final

from fastapi import HTTPException, Request

SESSION_COOKIE: Final = "vs_session"
_SESSION_TTL_SECONDS: Final = 86_400
_MAX_SESSIONS: Final = 64
_UNAUTHORIZED: Final = HTTPException(
    status_code=401,
    detail="service authentication required",
    headers={"WWW-Authenticate": "Bearer"},
)


class SessionStore:
    """Random opaque session ids minted server-side and validated in memory."""

    def __init__(
        self, ttl_seconds: int = _SESSION_TTL_SECONDS, max_sessions: int = _MAX_SESSIONS
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def mint(self) -> str:
        """Create a session and return its cookie value."""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._sessions = {
                key: expiry for key, expiry in self._sessions.items() if expiry > now
            }
            while len(self._sessions) >= self._max:
                self._sessions.pop(next(iter(self._sessions)))
            self._sessions[token] = now + self._ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry <= now:
                del self._sessions[token]
                return False
        return True


def allowed_origins(port: int) -> frozenset[str]:
    """The only origins from which a cookie-authenticated mutation may come."""
    return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    return header[len(prefix) :] or None


def _bearer_authorized(request: Request) -> bool:
    presented = _bearer_token(request)
    expected = request.app.state.read_token()
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def _cookie_session(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def _is_loopback_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return origin in request.app.state.allowed_origins


def _cookie_authorized(request: Request) -> bool:
    return request.app.state.sessions.valid(_cookie_session(request))


def require_read_auth(request: Request) -> str:
    """Authorize a safe read via bearer token or UI session cookie."""
    if _bearer_authorized(request):
        return "bearer"
    if _cookie_authorized(request):
        return "cookie"
    raise _UNAUTHORIZED


def require_mutation_auth(request: Request) -> str:
    """Authorize a mutation; cookie auth additionally passes the CSRF guard."""
    if _bearer_authorized(request):
        return "bearer"
    if _cookie_authorized(request) and _is_loopback_origin(request):
        return "cookie"
    raise _UNAUTHORIZED
