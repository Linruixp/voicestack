"""HTTP adapters for the enrollment, merge and split domain logic.

The domain logic lives in :mod:`enrollment`; this module only maps its typed
errors onto HTTP status codes, so the routes stay thin. ``update_speaker``
stays here: patching a speaker's own columns is a single-aggregate edit with
no enrollment semantics.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

import enrollment
from api_service import ServiceDeps, speaker_payload
from registry import Registry
from registry_models import MeetingNotFoundError, RegistryError, SpeakerNotFoundError

_NOT_FOUND: tuple[type[RegistryError], ...] = (
    SpeakerNotFoundError,
    MeetingNotFoundError,
    enrollment.ClusterNotFoundError,
)


def update_speaker(
    registry: Registry, speaker_id: int, changes: dict[str, Any]
) -> dict[str, Any]:
    if not changes:
        raise HTTPException(400, "no fields to update")
    if "name" in changes:
        changes["name"] = (changes["name"] or "").strip()
        if not changes["name"]:
            raise HTTPException(400, "speaker name must not be empty")
    fields = sorted(changes)
    assignments = ", ".join(f"{field} = ?" for field in fields)
    with registry._conn as conn:
        cursor = conn.execute(
            f"UPDATE speakers SET {assignments} WHERE id = ?",
            (*[changes[field] for field in fields], speaker_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, f"speaker {speaker_id} does not exist")
    speaker = registry.get_speaker(speaker_id)
    assert speaker is not None
    count = len(registry.voiceprints_for_speaker(speaker_id))
    return speaker_payload(speaker, count)


def attach_voiceprint(
    registry: Registry,
    deps: ServiceDeps,
    speaker_id: int,
    meeting_id: int,
    cluster_id: int,
) -> dict[str, Any]:
    """Attach a cluster's voiceprint to an EXISTING speaker (no duplicate row)."""
    try:
        result = enrollment.attach_voiceprint(
            registry,
            deps.embedder,
            deps.audio_loader,
            speaker_id=speaker_id,
            meeting_id=meeting_id,
            cluster_id=cluster_id,
        )
    except RegistryError as exc:
        raise _translate(exc) from exc
    return {
        "speaker_id": result.speaker_id,
        "voiceprint_id": result.voiceprint_id,
        "cluster_id": result.cluster_id,
        "similarity": result.similarity,
    }


def merge_speakers(
    registry: Registry, target_id: int, source_id: int
) -> dict[str, Any]:
    """Fold ``source_id`` into ``target_id``: move every reference, drop source."""
    try:
        result = enrollment.merge_speakers(
            registry, target_id=target_id, source_id=source_id
        )
    except RegistryError as exc:
        raise _translate(exc) from exc
    return {
        "target_speaker_id": result.target_speaker_id,
        "source_speaker_id": result.source_speaker_id,
        "voiceprints_moved": result.voiceprints_moved,
        "links_moved": result.links_moved,
        "segments_moved": result.segments_moved,
    }


def split_cluster(
    registry: Registry, meeting_id: int, cluster_id: int, at: float
) -> dict[str, Any]:
    """Move the cluster's segments starting at/after ``at`` into a new cluster."""
    try:
        result = enrollment.split_cluster(
            registry, meeting_id=meeting_id, cluster_id=cluster_id, at=at
        )
    except RegistryError as exc:
        raise _translate(exc) from exc
    return {
        "cluster_id": result.cluster_id,
        "new_cluster_id": result.new_cluster_id,
        "moved_segments": result.moved_segments,
    }


def _translate(exc: RegistryError) -> HTTPException:
    status = 404 if isinstance(exc, _NOT_FOUND) else 400
    return HTTPException(status, str(exc))
