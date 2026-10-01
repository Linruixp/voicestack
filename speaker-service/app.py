"""Minimal FastAPI application for the speaker-service.

Full API surface (ASR, diarization, alignment, identity, storage) is added in
later tasks; this base exposes only ``GET /health``.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="speaker-service", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
