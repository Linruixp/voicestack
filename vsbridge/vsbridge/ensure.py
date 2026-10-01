"""On-demand VoiceStudio bring-up.

Mirrors the voicebridge (task 13) launcher discipline: run
``vs-ensure-up.sh`` before a proxy call, self-heal a stale lock left by a
SIGKILLed launcher, then confirm ``/health`` before handing control to the MCP
client. Every failure is a :class:`BridgeError` — never a hang.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .config import BridgeConfig
from .errors import BridgeError


class EnsureUp:
    """Runs the launcher and confirms the backend is serving on its port."""

    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config

    def ensure_up(self) -> None:
        """Run the ensure-up launcher (unless disabled) then confirm /health."""
        if self.cfg.ensure_up_enabled:
            self._run_ensure_up()
        self._wait_health()

    def _run_ensure_up(self) -> None:
        script = self.cfg.ensure_up_script
        if not script.is_file():
            raise BridgeError(
                "ensure_up_missing",
                f"ensure-up script not found: {script}",
                {"path": str(script)},
            )
        self._clear_stale_lock()
        env = dict(os.environ)
        env["VS_PORT"] = str(self.cfg.port)
        env["VS_TIMEOUT"] = str(int(self.cfg.ensure_timeout_s))
        try:
            proc = subprocess.Popen(
                [str(script)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise BridgeError(
                "ensure_up_exec_failed",
                f"could not execute ensure-up script: {exc}",
                {"path": str(script)},
            ) from exc
        try:
            stdout, stderr = proc.communicate(timeout=self.cfg.ensure_timeout_s)
        except subprocess.TimeoutExpired as exc:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            self._clear_stale_lock()
            raise BridgeError(
                "ensure_up_timeout",
                f"ensure-up did not finish within {self.cfg.ensure_timeout_s}s",
                {"path": str(script)},
            ) from exc
        if proc.returncode != 0:
            raise BridgeError(
                "ensure_up_failed",
                f"ensure-up exited with code {proc.returncode}",
                {"stderr": (stderr or "")[-2000:], "stdout": (stdout or "")[-1000:]},
            )

    def _clear_stale_lock(self) -> None:
        """Remove the ensure-up lock when no launcher is alive to own it.

        The script cleans its lock via an EXIT trap, but a SIGKILLed run leaves
        the directory behind; every later run then waits out its full timeout.
        Only clear it when no ``vs-ensure-up.sh`` process is running.
        """
        lock = Path(f"/tmp/vs-ensure-up.lock.{self.cfg.port}")
        if not lock.exists():
            return
        try:
            running = (
                subprocess.run(
                    ["pgrep", "-f", "vs-ensure-up.sh"], capture_output=True
                ).returncode
                == 0
            )
        except OSError:
            running = False
        if not running:
            shutil.rmtree(lock, ignore_errors=True)

    def _wait_health(self) -> None:
        deadline = time.monotonic() + self.cfg.health_timeout_s
        last_error = "no attempt"
        while True:
            try:
                with httpx.Client(
                    base_url=self.cfg.base_url,
                    timeout=httpx.Timeout(5.0, connect=3.0),
                ) as client:
                    resp = client.get("/health")
                if resp.status_code == 200:
                    return
                last_error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise BridgeError(
                    "vs_unreachable",
                    f"VoiceStudio not healthy at {self.cfg.base_url} within "
                    f"{self.cfg.health_timeout_s}s",
                    {"base_url": self.cfg.base_url, "last_error": last_error},
                )
            time.sleep(0.5)
