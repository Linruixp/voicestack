"""Minimal Streamable-HTTP MCP client for VoiceStudio's ``/mcp/`` endpoint.

Implements just enough of the protocol to *proxy*: ``initialize`` (capturing the
``Mcp-Session-Id``), ``notifications/initialized``, ``tools/list`` and
``tools/call``. Responses are SSE-framed (``event: message`` / ``data: {...}``),
so replies are parsed from the ``data:`` line. The session id is reused across
calls; an expired session is re-established once and the call retried.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx

from . import __version__
from .config import BridgeConfig
from .errors import BridgeError

_JSONRPC = "2.0"
_PROTOCOL_VERSION = "2024-11-05"
_ACCEPT = "application/json, text/event-stream"


class VoiceStudioMcpClient:
    """Forwards JSON-RPC to VoiceStudio's MCP and returns the raw results."""

    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config
        self._session_id: str | None = None
        self._ids = itertools.count(1)

    # ── reachability ─────────────────────────────────────────────────────────

    def probe(self) -> bool:
        """Fast, non-blocking check that the backend is already serving.

        Does **not** cold-start anything — this is what lets ``tools/list``
        mirror a live server when present and fall back to the catalog when down.
        """
        try:
            with httpx.Client(
                base_url=self.cfg.base_url,
                timeout=httpx.Timeout(
                    self.cfg.probe_timeout_s, connect=self.cfg.probe_timeout_s
                ),
            ) as client:
                resp = client.get("/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ── transport ────────────────────────────────────────────────────────────

    def _post(
        self, payload: dict[str, Any], *, timeout: float, expect_reply: bool = True
    ) -> Any:
        headers = {"Content-Type": "application/json", "Accept": _ACCEPT}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        method = payload.get("method", "?")
        try:
            with httpx.Client(
                base_url=self.cfg.base_url,
                timeout=httpx.Timeout(timeout, connect=10.0),
            ) as client:
                resp = client.post(self.cfg.mcp_path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise BridgeError(
                "vs_unreachable",
                f"VoiceStudio MCP unreachable at {self.cfg.mcp_url}: {exc}",
                {"base_url": self.cfg.base_url, "mcp_path": self.cfg.mcp_path},
            ) from exc

        session = resp.headers.get("mcp-session-id")
        if session:
            self._session_id = session

        is_expiry = resp.status_code == 404 and self._looks_expired(resp)
        if resp.status_code >= 400:
            raise BridgeError(
                "vs_mcp_session_expired" if is_expiry else "vs_mcp_http_error",
                f"MCP {method} returned HTTP {resp.status_code}",
                {"status": resp.status_code, "body": resp.text[:2000]},
            )
        if not expect_reply:
            return None

        reply = self._parse_reply(resp)
        error = reply.get("error")
        if error:
            raise BridgeError(
                str(error.get("code") or "vs_mcp_error"),
                str(error.get("message") or "VoiceStudio MCP returned an error"),
                {"rpc_error": error},
            )
        return reply.get("result")

    @staticmethod
    def _looks_expired(resp: httpx.Response) -> bool:
        return "session" in resp.text.lower() and (
            "not found" in resp.text.lower() or "expired" in resp.text.lower()
        )

    @staticmethod
    def _parse_reply(resp: httpx.Response) -> dict[str, Any]:
        ctype = resp.headers.get("content-type", "")
        body = resp.text
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                raw = stripped[len("data:") :].strip()
                if not raw:
                    continue
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    continue
            raise BridgeError(
                "vs_mcp_bad_reply",
                "MCP SSE response carried no JSON data line",
                {"body": body[:1000]},
            )
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise BridgeError(
                "vs_mcp_bad_reply",
                "MCP response was not JSON",
                {"body": body[:1000]},
            ) from exc
        if not isinstance(parsed, dict):
            raise BridgeError(
                "vs_mcp_bad_reply",
                "MCP response JSON was not an object",
                {"type": type(parsed).__name__},
            )
        return parsed

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        """Send a request, re-initialising once if the session has expired."""
        self._ensure_session()
        payload = {
            "jsonrpc": _JSONRPC,
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        try:
            return self._post(payload, timeout=self.cfg.mcp_timeout_s)
        except BridgeError as exc:
            if exc.code != "vs_mcp_session_expired":
                raise
            self._session_id = None
            self._initialize()
            payload["id"] = next(self._ids)
            return self._post(payload, timeout=self.cfg.mcp_timeout_s)

    # ── handshake ────────────────────────────────────────────────────────────

    def _ensure_session(self) -> None:
        if not self._session_id:
            self._initialize()

    def _initialize(self) -> None:
        self._post(
            {
                "jsonrpc": _JSONRPC,
                "id": next(self._ids),
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "vsbridge", "version": __version__},
                },
            },
            timeout=self.cfg.mcp_timeout_s,
        )
        if not self._session_id:
            raise BridgeError(
                "vs_mcp_no_session",
                "VoiceStudio MCP did not return an Mcp-Session-Id header",
                {"base_url": self.cfg.base_url},
            )
        self._post(
            {"jsonrpc": _JSONRPC, "method": "notifications/initialized"},
            timeout=self.cfg.mcp_timeout_s,
            expect_reply=False,
        )

    # ── proxied operations ───────────────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {})
        tools = (result or {}).get("tools")
        if not isinstance(tools, list):
            raise BridgeError(
                "vs_mcp_bad_reply",
                "tools/list did not return a tools array",
                {"result": result},
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise BridgeError(
                "vs_mcp_bad_reply",
                "tools/call did not return an object result",
                {"result": result},
            )
        return result
