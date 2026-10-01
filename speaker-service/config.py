"""Configuration for the speaker-service.

Runtime settings come from the environment (optionally a local ``.env`` file);
the service bearer token is read from the macOS Keychain. No secret is ever
stored in the repository.

Model-cache reuse: ``HF_HOME`` / ``HF_HUB_CACHE`` are pointed at the existing
Hugging Face hub so already-downloaded model weights are reused in place and
never re-fetched.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

# Must run before any Hugging Face / transformers / pyannote import so the
# shared hub is used instead of a fresh per-service cache.
_HF_HOME = Path.home() / ".cache" / "huggingface"
os.environ.setdefault("HF_HOME", str(_HF_HOME))
os.environ.setdefault("HF_HUB_CACHE", str(_HF_HOME / "hub"))

from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

KEYCHAIN_SERVICE = "voicestack-service"
KEYCHAIN_ACCOUNT = "service"


class Settings(BaseSettings):
    """Environment-backed settings (``VASTACK_*``)."""

    model_config = SettingsConfigDict(
        env_prefix="VASTACK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = 3910
    bind: str = "127.0.0.1"
    idle_exit_s: int = 600
    # Optional env override (VASTACK_TOKEN); normally the token lives in Keychain.
    token: str | None = None
    # App-data root for the durable registry (owner-only 700 on creation).
    data_dir: Path = (
        Path.home() / "Library" / "Application Support" / "VoiceStudioStack"
    )
    # Diarization device, EXPLICITLY pinned after a CPU-vs-MPS test on this host
    # (2026-10-01): both devices returned identical turns on a 2-speaker clip;
    # MPS was ~5x faster (74 s clip: 10.5 s vs 53.6 s). There is no silent
    # fallback - if MPS is unavailable, diarization raises instead of switching.
    diarize_device: str = "mps"
    # Cosine acceptance threshold for voiceprint matching. Calibrated
    # 2026-10-01 on fixtures/meetings (enroll rec1, probe rec2, plus the unseen
    # ZH voice as an impostor): worst genuine 0.9763, best impostor 0.3067, so
    # the maximum-margin midpoint is 0.641; at it precision 1.0, FAR 0.0,
    # FRR 0.0 (2 genuine + 4 impostor trials). Reproduce with
    # scripts/calibrate_match.py; see evidence task-20-match.json. Re-calibrate
    # when more speakers are available. Override for experiments with
    # VASTACK_MATCH_THRESHOLD.
    match_threshold: float = 0.641
    # When enabled, the uploaded copy of a meeting recording is deleted as
    # soon as its transcription job reaches a terminal state (done or failed).
    # Off by default so a failed job can be re-inspected.
    delete_audio_after_transcribe: bool = False

    @property
    def hf_home(self) -> str:
        return os.environ["HF_HOME"]

    @property
    def hf_hub_cache(self) -> str:
        return os.environ["HF_HUB_CACHE"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "voicestack.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def _keychain_token() -> str | None:
    """Read the service bearer token from the macOS Keychain.

    Cached: ``security`` is spawned at most once per process, so an
    authenticated request never forks a subprocess. Token rotation therefore
    takes effect on the next service restart (the service idle-exits anyway).
    """
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
    token = result.stdout.strip()
    return token or None


def read_service_token() -> str | None:
    """Return the service token: ``VASTACK_TOKEN`` override first, else Keychain."""
    override = get_settings().token
    if override:
        return override
    return _keychain_token()


def require_service_token() -> str:
    token = read_service_token()
    if not token:
        raise RuntimeError(
            "service token not found; mint one with: "
            f"security add-generic-password -U -s {KEYCHAIN_SERVICE} "
            f"-a {KEYCHAIN_ACCOUNT} -w <token>"
        )
    return token
