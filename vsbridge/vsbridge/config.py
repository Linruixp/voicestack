"""Runtime configuration for the vsbridge MCP server.

Everything is env-overridable so the bridge can be pointed at a dead port
(failure test), a different VoiceStudio install, or a relocated launcher without
code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://127.0.0.1:3900"
DEFAULT_MCP_PATH = "/mcp/"
DEFAULT_ENSURE_UP = "~/voicestack/bin/vs-ensure-up.sh"
DEFAULT_CATALOG = "~/voicestack/vsbridge/vsbridge/catalog.json"


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Immutable snapshot of the bridge's environment."""

    base_url: str
    mcp_path: str
    ensure_up_script: Path
    ensure_up_enabled: bool
    ensure_timeout_s: float
    health_timeout_s: float
    mcp_timeout_s: float
    probe_timeout_s: float
    catalog_path: Path

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls(
            base_url=_str("VS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            mcp_path=_str("VS_MCP_PATH", DEFAULT_MCP_PATH),
            ensure_up_script=Path(
                os.path.expanduser(_str("VS_ENSURE_UP", DEFAULT_ENSURE_UP))
            ),
            ensure_up_enabled=_flag("VS_ENSURE_UP_ENABLED", True),
            ensure_timeout_s=_float("VS_ENSURE_TIMEOUT", 120.0),
            health_timeout_s=_float("VS_HEALTH_TIMEOUT", 20.0),
            mcp_timeout_s=_float("VS_MCP_TIMEOUT", 300.0),
            probe_timeout_s=_float("VS_PROBE_TIMEOUT", 0.5),
            catalog_path=Path(os.path.expanduser(_str("VS_CATALOG", DEFAULT_CATALOG))),
        )

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        parsed = urlparse(self.base_url)
        return parsed.port or 3900

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}{self.mcp_path}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"
