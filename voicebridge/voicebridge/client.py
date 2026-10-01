"""VoiceStudio HTTP client + orchestration for the two voicebridge tools.

Before *every* proxy call the bridge runs ``vs-ensure-up.sh`` so VoiceStudio can
stay on-demand. All failures become :class:`BridgeError`, which serialises to a
structured JSON payload — the bridge never hangs and never raises past the tool
boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .config import BridgeConfig

OFFLINE_PROVIDERS = frozenset({"argos", "nllb"})
ALLOWED_FORMATS = frozenset({"m4b", "mp3"})


class BridgeError(Exception):
    """Structured, serialisable failure surfaced to the MCP client."""

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


def probe_media(path: Path) -> dict[str, Any] | None:
    """Best-effort ffprobe summary; returns None when ffprobe is unavailable."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-print_format",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:500]}
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    fmt = raw.get("format", {})
    streams = raw.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "format_name": fmt.get("format_name"),
        "duration_s": _as_float(fmt.get("duration")),
        "size_bytes": _as_int(fmt.get("size")),
        "bit_rate": _as_int(fmt.get("bit_rate")),
        "audio_codec": audio.get("codec_name"),
        "sample_rate": _as_int(audio.get("sample_rate")),
        "channels": audio.get("channels"),
        "chapter_count": len(raw.get("chapters", [])),
    }


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class VoiceStudioClient:
    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config

    # ── transport ────────────────────────────────────────────────────────────

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            with httpx.Client(
                base_url=self.cfg.base_url,
                timeout=httpx.Timeout(timeout, connect=10.0),
                follow_redirects=True,
            ) as client:
                resp = client.request(method, path, json=json_body, files=files)
        except httpx.HTTPError as exc:
            raise BridgeError(
                "vs_unreachable",
                f"VoiceStudio is not reachable at {self.cfg.base_url} ({path}): {exc}",
                {"base_url": self.cfg.base_url, "path": path},
            ) from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:2000]}
        return resp.status_code, payload

    # ── on-demand bring-up ───────────────────────────────────────────────────

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
            # Prefer SIGTERM so the script's EXIT trap removes its lock, then
            # clear whatever it left behind before surfacing a structured error.
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

    # ── profiles / voices ────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict[str, Any]]:
        status, body = self._request_json("GET", "/profiles", timeout=30.0)
        if status >= 400 or not isinstance(body, list):
            raise BridgeError(
                "profiles_unavailable",
                f"could not list VoiceStudio profiles (HTTP {status})",
                {"status": status},
            )
        return body

    def resolve_voice(self, voice: str) -> str | None:
        """Accept a profile id or name; returns the id (or the raw value)."""
        value = (voice or "").strip()
        if not value:
            return None
        try:
            profiles = self.list_profiles()
        except BridgeError:
            return value
        for profile in profiles:
            if profile.get("id") == value:
                return value
        lowered = value.lower()
        for profile in profiles:
            if str(profile.get("name", "")).lower() == lowered:
                return profile.get("id")
        return value

    # ── audiobook ────────────────────────────────────────────────────────────

    def import_document(self, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            files = {"file": (path.name, handle, "application/octet-stream")}
            status, body = self._request_json(
                "POST",
                "/audiobook/import",
                timeout=self.cfg.import_timeout_s,
                files=files,
            )
        if status >= 400:
            raise BridgeError(
                "import_failed",
                f"/audiobook/import rejected {path.name} (HTTP {status})",
                {"status": status, "body": body},
            )
        return body

    def render_audiobook(
        self, text: str, voice: str | None, language: str, fmt: str
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "format": fmt}
        if voice:
            payload["default_voice"] = voice
        if language:
            payload["language"] = language
        if self.cfg.loudness:
            payload["loudness"] = self.cfg.loudness

        events: list[dict[str, Any]] = []
        try:
            with httpx.Client(
                base_url=self.cfg.base_url,
                timeout=httpx.Timeout(self.cfg.render_timeout_s, connect=15.0),
            ) as client:
                with client.stream("POST", "/audiobook", json=payload) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", "replace")[:2000]
                        raise BridgeError(
                            "render_failed",
                            f"/audiobook returned HTTP {resp.status_code}",
                            {"status": resp.status_code, "body": body},
                        )
                    for line in resp.iter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw = line[len("data:") :].strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        events.append(event)
                        etype = event.get("type")
                        if etype == "error":
                            raise BridgeError(
                                "render_error",
                                str(
                                    event.get("message")
                                    or event.get("error")
                                    or "render error"
                                ),
                                {"event": event},
                            )
                        if etype == "done":
                            return event
        except BridgeError:
            raise
        except httpx.HTTPError as exc:
            raise BridgeError(
                "render_unreachable",
                f"audiobook stream failed: {exc}",
                {"events_seen": len(events)},
            ) from exc
        raise BridgeError(
            "render_incomplete",
            "audiobook stream ended without a 'done' event",
            {"events_seen": len(events), "last_events": events[-5:]},
        )

    def generate_audiobook(
        self, import_path: str, voice: str = "", language: str = "", fmt: str = "m4b"
    ) -> dict[str, Any]:
        path = Path(import_path).expanduser()
        if not path.is_file():
            raise BridgeError(
                "import_not_found", f"document not found: {path}", {"path": str(path)}
            )
        chosen = (fmt or "m4b").lower()
        if chosen not in ALLOWED_FORMATS:
            raise BridgeError(
                "bad_format",
                f"unsupported format '{fmt}' (expected one of {sorted(ALLOWED_FORMATS)})",
                {"format": fmt},
            )

        self.ensure_up()

        imported = self.import_document(path)
        text = str(imported.get("text") or "")
        if not text.strip():
            raise BridgeError(
                "empty_document",
                f"{path.name} produced no extractable text",
                {"chapters": imported.get("chapters")},
            )

        voice_id = self.resolve_voice(voice)
        done = self.render_audiobook(text, voice_id, language, chosen)

        filename = done.get("output")
        if not filename:
            raise BridgeError(
                "no_output",
                "render finished without an output filename",
                {"done": done},
            )
        out_path = self.cfg.outputs_dir / filename
        if not out_path.is_file():
            raise BridgeError(
                "output_missing",
                f"render reported '{filename}' but it is absent under the outputs dir",
                {
                    "expected_path": str(out_path),
                    "outputs_dir": str(self.cfg.outputs_dir),
                },
            )

        return {
            "ok": True,
            "output_path": str(out_path),
            "filename": filename,
            "size_bytes": out_path.stat().st_size,
            "chapters": done.get("chapters"),
            "duration_s": done.get("duration_s"),
            "cached_chapters": done.get("cached_chapters"),
            "failed_chapters": done.get("failed_chapters"),
            "title": done.get("title"),
            "voice": voice_id,
            "language": language or None,
            "format": chosen,
            "ffprobe": probe_media(out_path),
        }

    # ── translation ──────────────────────────────────────────────────────────

    def translation_engines(self) -> dict[str, Any]:
        status, body = self._request_json("GET", "/engines/translation", timeout=20.0)
        if status >= 400:
            raise BridgeError(
                "engines_unavailable",
                f"could not read translation engines (HTTP {status})",
                {"status": status, "body": body},
            )
        return body

    def argos_pack_status(
        self, source: str, targets: list[str]
    ) -> list[dict[str, Any]]:
        status, body = self._request_json(
            "POST",
            "/engines/translation/argos/packs/status",
            timeout=20.0,
            json_body={"source_lang": source, "target_langs": targets},
        )
        if status >= 400:
            raise BridgeError(
                "argos_status_failed",
                f"argos pack status failed (HTTP {status})",
                {"status": status, "body": body},
            )
        return body.get("pairs", [])

    def install_argos_pack(self, source: str, targets: list[str]) -> dict[str, Any]:
        timeout = max(self.cfg.translate_timeout_s, 300.0)
        status, body = self._request_json(
            "POST",
            "/engines/translation/argos/packs/install",
            timeout=timeout,
            json_body={"source_lang": source, "target_langs": targets},
        )
        if status >= 400:
            raise BridgeError(
                "argos_install_failed",
                f"argos pack install failed (HTTP {status})",
                {"status": status, "body": body},
            )
        return body

    def _ensure_argos_pack(self, source: str, target: str) -> None:
        if not source or not target:
            raise BridgeError(
                "argos_langs_required",
                "argos needs explicit source and target languages",
                {"source_lang": source, "target_lang": target},
            )
        pairs = self.argos_pack_status(source, [target])
        missing = [p["target_lang"] for p in pairs if not p.get("installed")]
        if not missing:
            return
        self.install_argos_pack(source, missing)
        pairs = self.argos_pack_status(source, [target])
        still = [p["target_lang"] for p in pairs if not p.get("installed")]
        if still:
            raise BridgeError(
                "argos_pack_unavailable",
                f"argos pack {source} → {target} could not be installed",
                {"missing": still},
            )

    def translate_batch(
        self, texts: list[str], source: str, target: str, provider: str
    ) -> dict[str, Any]:
        segments = [{"id": str(i), "text": t} for i, t in enumerate(texts)]
        payload: dict[str, Any] = {
            "segments": segments,
            "target_lang": target,
            "provider": provider,
            "quality": "fast",
        }
        if source:
            payload["source_lang"] = source
        status, body = self._request_json(
            "POST",
            "/dub/translate",
            timeout=self.cfg.translate_timeout_s,
            json_body=payload,
        )
        if status >= 400 or "error" in body:
            raise BridgeError(
                str(body.get("code") or "translate_http_error"),
                str(body.get("error") or f"translate failed (HTTP {status})"),
                {"status": status, "body": body},
            )
        return body

    def _provider_candidates(self, status: dict[str, Any]) -> list[str]:
        engines = {e.get("id"): e for e in status.get("engines", [])}
        if self.cfg.provider_override:
            return [self.cfg.provider_override]
        candidates: list[str] = []
        argos = engines.get("argos")
        if argos and argos.get("installed") and argos.get("ready"):
            candidates.append("argos")
        for engine_id, engine in engines.items():
            if engine_id in candidates:
                continue
            if engine.get("installed") and engine.get("ready"):
                candidates.append(engine_id)
        return candidates

    def translate_text(self, text: str, source: str, target: str) -> dict[str, Any]:
        text = text or ""
        if not text.strip():
            raise BridgeError("empty_text", "text to translate is empty", {})
        if source and target and source.lower() == target.lower():
            return {
                "ok": True,
                "provider": "none",
                "offline": True,
                "source_lang": source,
                "target_lang": target,
                "translated": text,
                "note": "source and target are identical; returned input unchanged",
            }

        self.ensure_up()
        engines = self.translation_engines()
        attempts: list[dict[str, Any]] = []
        for provider in self._provider_candidates(engines):
            try:
                if provider == "argos":
                    self._ensure_argos_pack(source, target)
                result = self.translate_batch([text], source, target, provider)
                translated = result.get("translated") or []
                segment = translated[0] if translated else {}
                if segment.get("error"):
                    attempts.append({"provider": provider, "error": segment["error"]})
                    continue
                return {
                    "ok": True,
                    "provider": provider,
                    "offline": provider in OFFLINE_PROVIDERS,
                    "source_lang": result.get("source_lang"),
                    "target_lang": result.get("target_lang"),
                    "quality_used": result.get("quality_used"),
                    "translated": segment.get("text", ""),
                    "attempts": attempts,
                }
            except BridgeError as exc:
                attempts.append(
                    {"provider": provider, "code": exc.code, "error": exc.message}
                )
                continue
        raise BridgeError(
            "translate_failed",
            "no translation provider succeeded",
            {"source_lang": source, "target_lang": target, "attempts": attempts},
        )
