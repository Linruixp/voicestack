#!/usr/bin/env python3
"""Task 35: pre-fetch ONLY the models not already cached.

Reads the HF token from the HF_TOKEN env var (never written anywhere).
Uses the shared HF cache (HF_HOME=~/.cache/huggingface) so already-cached
models are REUSED in place and never re-downloaded.

For each repo it records: repo id, pinned revision sha, local snapshot path,
size on disk, and whether it was REUSED (already fully cached) or DOWNLOADED.

Note: this cache uses the HF 2.0 shared-blobs layout, where per-model
`blobs/*` entries are symlinks into a content-addressed root `blobs/` store.
Sizes are therefore computed by resolving symlinks and de-duplicating real
paths (plain `du` on a single model dir undercounts; `du -L` double-counts).

Usage:  uv run --no-sync python scripts/fetch_models.py [report.json]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

HUB = Path(
    os.environ.get("HF_HUB_CACHE", os.path.expanduser("~/.cache/huggingface/hub"))
)
# Exact revisions the runtime pins (asr.MODEL_REVISION, diarize.MODEL_REVISION,
# embed.MODEL_REVISION). Fetching a ref instead would cache whatever main is
# that day, which the service would then refuse to load.
PINS = {
    "mlx-community/whisper-large-v3-mlx": "49e6aa286ad60c14352c404340ded53710378a11",
    "pyannote/speaker-diarization-community-1": (
        "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
    ),
    "pyannote/wespeaker-voxceleb-resnet34-LM": (
        "837717ddb9ff5507820346191109dc79c958d614"
    ),
}


def _df_avail() -> str:
    out = subprocess.run(
        ["df", "-h", "/System/Volumes/Data"], capture_output=True, text=True
    ).stdout
    return out.strip().splitlines()[-1]


def _hub_size() -> int:
    """Disk usage of the whole hub (du -sk on the hub root is accurate)."""
    out = subprocess.run(["du", "-sk", str(HUB)], capture_output=True, text=True).stdout
    return int(out.split()[0]) * 1024 if out.strip() else 0


def _cache_dir(repo_id: str) -> Path:
    return HUB / ("models--" + repo_id.replace("/", "--"))


def _model_size(cache_dir: Path) -> int:
    """Real on-disk bytes for one model, dereferencing the shared-blob symlinks."""
    seen: set[str] = set()
    total = 0
    blobs = cache_dir / "blobs"
    if not blobs.is_dir():
        return 0
    for root, _dirs, files in os.walk(blobs):
        for name in files:
            real = os.path.realpath(os.path.join(root, name))
            if real in seen:
                continue
            seen.add(real)
            try:
                total += os.path.getsize(real)
            except OSError:
                pass
    return total


def _is_complete(cache_dir: Path, revision: str, filenames: list[str]) -> bool:
    """True only when EVERY repo file exists in the local snapshot for `revision`."""
    snap = cache_dir / "snapshots" / revision
    for name in filenames:
        if not (snap / name).exists():  # follows symlinks; dangling -> False
            return False
    return True


def main() -> int:
    token = os.environ.get("HF_TOKEN")
    if not token:
        print(
            "ERROR: HF_TOKEN env var is not set (read it from Keychain).",
            file=sys.stderr,
        )
        return 2

    api = HfApi()
    hub_before = _hub_size()
    df_before = _df_avail()

    report: dict = {
        "hub": str(HUB),
        "hub_size_bytes_before": hub_before,
        "df_before": df_before,
        "models": [],
    }

    for repo_id, revision in PINS.items():
        info = api.model_info(repo_id, revision=revision, token=token)
        filenames = [s.rfilename for s in info.siblings]
        cdir = _cache_dir(repo_id)
        size_before = _model_size(cdir)

        if _is_complete(cdir, revision, filenames):
            reused = True
            snapshot_path = str(cdir / "snapshots" / revision)
            dl_seconds = 0.0
        else:
            reused = False
            t0 = time.time()
            snapshot_path = snapshot_download(
                repo_id,
                revision=revision,
                token=token,
                cache_dir=str(HUB),
            )
            dl_seconds = round(time.time() - t0, 1)

        report["models"].append(
            {
                "repo_id": repo_id,
                "revision": revision,
                "gated": info.gated,
                "local_path": snapshot_path,
                "files": len(filenames),
                "size_bytes": _model_size(cdir),
                "size_gained_bytes": _model_size(cdir) - size_before,
                "download_seconds": dl_seconds,
                "status": "REUSED" if reused else "DOWNLOADED",
            }
        )

    hub_after = _hub_size()
    report["hub_size_bytes_after"] = hub_after
    report["hub_delta_bytes"] = hub_after - hub_before
    report["df_after"] = _df_avail()

    text = json.dumps(report, indent=2)
    print(text)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
