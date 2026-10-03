"""Bearer-authenticated HTTP client for the speaker-service API.

Maps the seven MCP operations onto the service routes and converts every
transport/HTTP failure into a structured :class:`mcp_proxy.BridgeError`.
"""

from __future__ import annotations

import mimetypes
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from mcp_proxy import BridgeError, ProxyConfig, ensure_up, read_token

KEYCHAIN_HINT = "voicestack-service/service"


class ServiceClient:
    """Bearer-authenticated HTTP client for the speaker-service API."""

    def __init__(self, config: ProxyConfig) -> None:
        self.cfg = config
        self._token: str | None = None

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            self._token = read_token()
        if not self._token:
            raise BridgeError(
                "missing_token",
                "service token not found: set VASTACK_TOKEN or add Keychain item "
                f"{KEYCHAIN_HINT}",
            )
        return {"Authorization": f"Bearer {self._token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        files: Any = None,
        data: Any = None,
        timeout: float | None = None,
        ensure: bool = True,
    ) -> Any:
        if ensure:
            ensure_up(self.cfg)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(
                    timeout or self.cfg.request_timeout_s, connect=5.0
                )
            ) as client:
                resp = client.request(
                    method,
                    f"{self.cfg.base_url}{path}",
                    headers=self._auth(),
                    json=json_body,
                    files=files,
                    data=data,
                )
        except httpx.HTTPError as exc:
            raise BridgeError(
                "service_unreachable",
                f"{exc.__class__.__name__}: {exc}",
                {"base_url": self.cfg.base_url, "path": path},
            ) from exc
        if resp.status_code >= 400:
            raise BridgeError(
                "service_error",
                _detail(resp),
                {"status": resp.status_code, "path": path},
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise BridgeError(
                "bad_response",
                f"service returned non-JSON: {resp.text[:200]}",
                {"status": resp.status_code, "path": path},
            ) from exc

    def transcribe_meeting(self, file_path: str) -> Any:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise BridgeError(
                "file_not_found", f"audio file not found: {path}", {"path": str(path)}
            )
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        created = self._request(
            "POST",
            "/meetings",
            files={"file": (path.name, path.read_bytes(), mime)},
            data={"title": path.stem},
            timeout=self.cfg.transcribe_timeout_s,
        )
        job_id = created.get("job_id")
        if job_id is not None:
            self._wait_job(int(job_id))
        meeting_id = created.get("meeting_id")
        if meeting_id is None:
            raise BridgeError("bad_response", "meeting response missing meeting_id")
        return self._request("GET", f"/meetings/{meeting_id}", ensure=False)

    def _wait_job(self, job_id: int) -> None:
        deadline = time.monotonic() + self.cfg.transcribe_timeout_s
        while True:
            job = self._request("GET", f"/jobs/{job_id}", ensure=False)
            state = job.get("state")
            if state == "done":
                return
            if state == "failed":
                raise BridgeError(
                    "job_failed",
                    job.get("error") or "transcription job failed",
                    {"job_id": job_id},
                )
            if time.monotonic() >= deadline:
                raise BridgeError(
                    "job_timeout",
                    f"job {job_id} did not finish within "
                    f"{self.cfg.transcribe_timeout_s}s",
                    {"job_id": job_id, "state": state},
                )
            time.sleep(self.cfg.poll_interval_s)

    def list_speakers(self) -> Any:
        return self._request("GET", "/speakers")

    def enroll_speaker(
        self,
        name: str,
        organization: str | None,
        notes: str | None,
        cluster_id: int,
    ) -> Any:
        return self._request(
            "POST",
            "/speakers/enroll",
            json_body={
                "name": name,
                "cluster_id": cluster_id,
                "organization": organization,
                "notes": notes,
            },
        )

    def attach_to_speaker(self, speaker_id: int, cluster_id: int) -> Any:
        return self._request(
            "POST",
            f"/speakers/{speaker_id}/voiceprints",
            json_body={"cluster_id": cluster_id},
        )

    def identify_speaker(self, audio_path: str) -> Any:
        path = Path(audio_path).expanduser()
        if not path.is_file():
            raise BridgeError(
                "file_not_found", f"audio file not found: {path}", {"path": str(path)}
            )
        return self._request("POST", "/identify", json_body={"audio_path": str(path)})

    def get_meeting(self, meeting_id: int) -> Any:
        return self._request("GET", f"/meetings/{meeting_id}")

    def rename_speaker(self, speaker_id: int, name: str) -> Any:
        return self._request(
            "PATCH", f"/speakers/{speaker_id}", json_body={"name": name}
        )

    def rename_meeting(self, meeting_id: int, title: str) -> Any:
        return self._request(
            "PATCH", f"/meetings/{meeting_id}", json_body={"title": title}
        )

    def open_speaker_ui(self, meeting_id: int) -> Any:
        """Open the meeting's speaker-naming deep link in the default browser."""
        detail = self._request("GET", f"/meetings/{meeting_id}")
        url = detail.get("ui_url") or f"{self.cfg.base_url}/"
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return {"opened": url}


def _detail(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:500] or f"HTTP {resp.status_code}"
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return str(payload)[:500]
