"""Speaker diarization (segmentation + clustering) via pyannote community-1.

Wraps ``pyannote/speaker-diarization-community-1`` to turn an audio file into
speaker turns ``[{start, end, speaker}]``. The model is pinned to an exact
revision and loaded from the pre-fetched Hugging Face hub cache in offline
mode, so diarization never touches the network.

Device policy: the device is EXPLICITLY configured in ``config.py``
(``VASTACK_DIARIZE_DEVICE``, pinned default ``"mps"``) and validated at load
time. There is no silent fallback: an unavailable device raises instead of
quietly switching. The ``"mps"`` default is the outcome of a CPU-vs-MPS test
on this host (``scripts/probe_device.py``): both devices returned identical
turns on a 2-speaker clip; MPS was ~5x faster (74 s clip: 10.5 s vs 53.6 s).

This module is used ONLY for segmentation/clustering. Identity embeddings are
a separate concern (``embed.py``); the per-speaker embeddings that community-1
returns alongside the turns are deliberately ignored here.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

# --- Model-cache reuse -----------------------------------------------------
# Must run before any Hugging Face / pyannote import so the shared hub is used
# instead of a fresh per-service cache (same defaults as config.py).
_HF_HOME = Path.home() / ".cache" / "huggingface"
os.environ.setdefault("HF_HOME", str(_HF_HOME))
os.environ.setdefault("HF_HUB_CACHE", str(_HF_HOME / "hub"))
# The pinned revision is pre-fetched; diarization must never reach the network.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from config import get_settings  # noqa: E402

if TYPE_CHECKING:
    from pyannote.audio import Pipeline

MODEL_REPO_ID = "pyannote/speaker-diarization-community-1"
MODEL_REVISION = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO_ID}"

KEYCHAIN_HF_SERVICE = "voicestack-hf"
KEYCHAIN_HF_ACCOUNT = "voicestack"

SUPPORTED_DEVICES = ("cpu", "mps")


class DiarizationError(RuntimeError):
    """Base class for diarization failures."""


class DeviceUnavailableError(DiarizationError):
    """The explicitly configured device cannot be used on this host."""


class ModelUnavailableError(DiarizationError):
    """The pinned model is missing/unlicensed; the message says how to install it."""


class SpeakerTurn(TypedDict):
    """One diarized speaker turn on the wire schema."""

    start: float
    end: float
    speaker: str


def read_hf_token() -> str | None:
    """Return the HF token: ``VASTACK_HF_TOKEN`` override first, else Keychain."""
    override = os.environ.get("VASTACK_HF_TOKEN")
    if override:
        return override
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_HF_SERVICE,
                "-a",
                KEYCHAIN_HF_ACCOUNT,
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


def model_is_cached() -> bool:
    """Return True when every pinned community-1 file is already in the hub cache."""
    snapshot = (
        Path(os.environ["HF_HUB_CACHE"])
        / f"models--{MODEL_REPO_ID.replace('/', '--')}"
        / "snapshots"
        / MODEL_REVISION
    )
    required = (
        snapshot / "config.yaml",
        snapshot / "segmentation" / "pytorch_model.bin",
        snapshot / "embedding" / "pytorch_model.bin",
        snapshot / "plda" / "plda.npz",
    )
    return all(path.is_file() for path in required)


def _model_install_hint() -> str:
    return (
        f"the pinned diarization model {MODEL_REPO_ID}@{MODEL_REVISION} is not "
        f"available offline. Accept the license at {MODEL_URL} (signed in with "
        "the same account as the Keychain token) and run "
        "`uv run --no-sync python scripts/fetch_models.py` to cache it."
    )


def _mps_available() -> bool:
    import torch

    return torch.backends.mps.is_available()


def resolve_device(device: str | None = None) -> str:
    """Return the validated diarization device name (``"cpu"`` or ``"mps"``).

    Falls back to ``VASTACK_DIARIZE_DEVICE`` only when no explicit value is
    given. An unusable device raises :class:`DeviceUnavailableError` - never
    silently assume MPS and never silently switch to CPU.
    """
    name = (device or get_settings().diarize_device).strip().lower()
    if name not in SUPPORTED_DEVICES:
        raise DeviceUnavailableError(
            f"unsupported diarization device {name!r}; set VASTACK_DIARIZE_DEVICE "
            f"to one of: {', '.join(SUPPORTED_DEVICES)}"
        )
    if name == "mps" and not _mps_available():
        raise DeviceUnavailableError(
            "VASTACK_DIARIZE_DEVICE=mps was configured but MPS is unavailable on "
            "this host; set VASTACK_DIARIZE_DEVICE=cpu explicitly (no silent "
            "fallback)"
        )
    return name


def _from_pretrained(token: str) -> Pipeline:
    """Thin seam around the hub call, kept patchable for error-path tests."""
    from pyannote.audio import Pipeline

    return Pipeline.from_pretrained(MODEL_REPO_ID, revision=MODEL_REVISION, token=token)


@lru_cache(maxsize=2)
def load_pipeline(device: str | None = None) -> Pipeline:
    """Load (once per device) the pinned pipeline on the configured device.

    Raises :class:`ModelUnavailableError` when the model is missing or its
    license has not been accepted, and :class:`DeviceUnavailableError` when the
    explicitly configured device is unusable.
    """
    name = resolve_device(device)
    token = read_hf_token()
    if not token:
        raise ModelUnavailableError(
            "no Hugging Face token found; store one with: "
            f"security add-generic-password -U -s {KEYCHAIN_HF_SERVICE} "
            f"-a {KEYCHAIN_HF_ACCOUNT} -w <hf_token>"
        )
    try:
        pipeline = _from_pretrained(token)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ModelUnavailableError(f"{_model_install_hint()} ({exc})") from exc
    if pipeline is None:
        raise ModelUnavailableError(_model_install_hint())

    import torch

    pipeline.to(torch.device(name))
    return pipeline


def diarize(
    audio_path: str | os.PathLike[str],
    *,
    pipeline: Pipeline | None = None,
    device: str | None = None,
) -> list[SpeakerTurn]:
    """Diarize ``audio_path`` into speaker turns ``[{start, end, speaker}]``.

    Turns come from segmentation + clustering only. Pass ``pipeline`` to reuse
    an already-loaded model, otherwise it is loaded (and cached) for ``device``.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(path)

    pipe = pipeline if pipeline is not None else load_pipeline(device)
    output = pipe(str(path))
    annotation = output.speaker_diarization
    return [
        SpeakerTurn(start=round(turn.start, 3), end=round(turn.end, 3), speaker=speaker)
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]
