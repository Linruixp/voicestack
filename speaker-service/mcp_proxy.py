"""Bearer credential, environment config and on-demand bring-up for the proxy.

The MCP server is a *thin proxy*: it never opens the SQLite registry, so the
HTTP service on ``127.0.0.1:3910`` stays the single writer (WAL). Before every
top-level tool call it runs ``vs-speaker-up.sh`` (the service may have
idle-exited) and waits for ``/health``. Failures are structured
:class:`BridgeError` values - never a hang.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

KEYCHAIN_SERVICE = "voicestack-service"
KEYCHAIN_ACCOUNT = "service"
DEFAULT_BASE_URL = "http://127.0.0.1:3910"
DEFAULT_ENSURE_UP = "~/voicestack/bin/vs-speaker-up.sh"


class BridgeError(Exception):
    """A failure with a stable machine-readable ``code`` and ``details``."""

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


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
class ProxyConfig:
    """Immutable snapshot of the proxy's environment."""

    base_url: str
    ensure_up_script: Path
    ensure_up_enabled: bool
    ensure_timeout_s: float
    health_timeout_s: float
    request_timeout_s: float
    transcribe_timeout_s: float
    poll_interval_s: float

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        return cls(
            base_url=_str("VASTACK_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            ensure_up_script=Path(
                os.path.expanduser(_str("VASTACK_ENSURE_UP", DEFAULT_ENSURE_UP))
            ),
            ensure_up_enabled=_flag("VASTACK_ENSURE_UP_ENABLED", True),
            ensure_timeout_s=_float("VASTACK_ENSURE_TIMEOUT", 90.0),
            health_timeout_s=_float("VASTACK_HEALTH_TIMEOUT", 20.0),
            request_timeout_s=_float("VASTACK_REQUEST_TIMEOUT", 30.0),
            transcribe_timeout_s=_float("VASTACK_TRANSCRIBE_TIMEOUT", 900.0),
            poll_interval_s=_float("VASTACK_POLL_INTERVAL", 1.0),
        )

    @property
    def port(self) -> int:
        return httpx.URL(self.base_url).port or 3910


def read_token() -> str | None:
    """Service token: ``VASTACK_TOKEN`` override first, else the Keychain."""
    override = os.environ.get("VASTACK_TOKEN")
    if override:
        return override
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def ensure_up(config: ProxyConfig) -> None:
    """Run the launcher (unless disabled) then confirm ``/health``."""
    if config.ensure_up_enabled:
        _run_launcher(config)
    _wait_health(config)


def _run_launcher(config: ProxyConfig) -> None:
    script = config.ensure_up_script
    if not script.is_file():
        raise BridgeError(
            "launcher_missing",
            f"launcher script not found: {script}",
            {"path": str(script)},
        )
    _clear_stale_lock(config)
    env = dict(os.environ)
    env["VS_SPEAKER_PORT"] = str(config.port)
    env["VS_SPEAKER_TIMEOUT"] = str(int(config.ensure_timeout_s))
    try:
        proc = subprocess.run(
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=config.ensure_timeout_s + 15.0,
        )
    except subprocess.TimeoutExpired as exc:
        _clear_stale_lock(config)
        raise BridgeError(
            "launcher_timeout",
            f"launcher did not finish within {config.ensure_timeout_s}s",
            {"path": str(script)},
        ) from exc
    except OSError as exc:
        raise BridgeError(
            "launcher_failed",
            f"could not execute launcher: {exc}",
            {"path": str(script)},
        ) from exc
    if proc.returncode != 0:
        raise BridgeError(
            "launcher_failed",
            f"launcher exited with code {proc.returncode}",
            {
                "stdout": (proc.stdout or "")[-1000:],
                "stderr": (proc.stderr or "")[-2000:],
            },
        )


def _clear_stale_lock(config: ProxyConfig) -> None:
    """Remove the launcher lock when no launcher process is alive to own it."""
    lock = Path(f"/tmp/vs-speaker-up.lock.{config.port}")
    if not lock.exists():
        return
    try:
        running = (
            subprocess.run(
                ["pgrep", "-f", "vs-speaker-up.sh"], capture_output=True
            ).returncode
            == 0
        )
    except OSError:
        running = False
    if not running:
        shutil.rmtree(lock, ignore_errors=True)


def _wait_health(config: ProxyConfig) -> None:
    deadline = time.monotonic() + config.health_timeout_s
    last_error = "no attempt"
    while True:
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                resp = client.get(f"{config.base_url}/health")
            if resp.status_code == 200:
                return
            last_error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
        if time.monotonic() >= deadline:
            raise BridgeError(
                "service_unreachable",
                f"service not healthy at {config.base_url} within "
                f"{config.health_timeout_s}s",
                {"base_url": config.base_url, "last_error": last_error},
            )
        time.sleep(0.5)
