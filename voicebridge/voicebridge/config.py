"""Runtime configuration for the voicebridge MCP server.

Everything is env-overridable so the bridge can be pointed at a dead port
(failure test) or a different VoiceStudio install without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://127.0.0.1:3900"
DEFAULT_ENSURE_UP = "~/voicestack/bin/vs-ensure-up.sh"
DEFAULT_OUTPUTS_DIR = "~/Library/Application Support/OmniVoice/outputs"


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
    ensure_up_script: Path
    ensure_up_enabled: bool
    ensure_timeout_s: float
    health_timeout_s: float
    import_timeout_s: float
    render_timeout_s: float
    translate_timeout_s: float
    outputs_dir: Path
    provider_override: str | None
    loudness: str | None

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls(
            base_url=_str("VS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            ensure_up_script=Path(
                os.path.expanduser(_str("VS_ENSURE_UP", DEFAULT_ENSURE_UP))
            ),
            ensure_up_enabled=_flag("VS_ENSURE_UP_ENABLED", True),
            ensure_timeout_s=_float("VS_ENSURE_TIMEOUT", 120.0),
            health_timeout_s=_float("VS_HEALTH_TIMEOUT", 20.0),
            import_timeout_s=_float("VS_IMPORT_TIMEOUT", 120.0),
            render_timeout_s=_float("VS_RENDER_TIMEOUT", 1800.0),
            translate_timeout_s=_float("VS_TRANSLATE_TIMEOUT", 180.0),
            outputs_dir=Path(
                os.path.expanduser(_str("VS_OUTPUTS_DIR", DEFAULT_OUTPUTS_DIR))
            ),
            provider_override=os.environ.get("VS_TRANSLATE_PROVIDER") or None,
            loudness=os.environ.get("VS_AUDIOBOOK_LOUDNESS") or None,
        )

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        parsed = urlparse(self.base_url)
        return parsed.port or 3900
