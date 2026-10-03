"""HTTP adapters for the enrollment, merge and split domain logic.

The domain logic lives in :mod:`enrollment`; this module only maps its typed
errors onto HTTP status codes, so the routes stay thin. ``update_speaker``
stays here: patching a speaker's own columns is a single-aggregate edit with
no enrollment semantics.
"""

from __future__ import annotations

import base64
import io
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import HTTPException

import enrollment
from api_schemas import BatchResolveItem
from api_service import ServiceDeps, meeting_payload, speaker_payload
from config import get_settings
from registry import Registry
from registry_models import (
    BatchState,
    ClusterState,
    MeetingNotFoundError,
    RegistryError,
    SpeakerNotFoundError,
)

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
    return speaker_payload(
        speaker, count, consent=registry.latest_consent_for_speaker(speaker_id)
    )


def _record_consent(
    registry: Registry,
    speaker_id: int,
    *,
    purpose: str,
    source_batch_id: int | None = None,
) -> None:
    """Record consent for holding this speaker's voiceprint until the term ends."""
    days = get_settings().consent_retention_days
    until = (datetime.now(UTC) + timedelta(days=days)).isoformat()
    registry.add_consent(
        speaker_id,
        purpose=purpose,
        retention_until=until,
        source_batch_id=source_batch_id,
    )


def enroll_new_speaker(
    registry: Registry,
    deps: ServiceDeps,
    *,
    name: str,
    organization: str | None,
    notes: str | None,
    title: str | None = None,
    cluster_id: int,
    source_batch_id: int | None = None,
) -> dict[str, Any]:
    """Create a NEW speaker from a cluster, resolving its meeting by cluster id.

    Delegates to the atomic ``enrollment.enroll_speaker`` so the voiceprint guard
    runs before any write: a refused enrollment (too little voiced audio, missing
    audio) leaves no orphan speaker row.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(400, "speaker name must not be empty")
    meeting_id = resolve_cluster_meeting(registry, cluster_id)
    try:
        result = enrollment.enroll_speaker(
            registry,
            deps.embedder,
            deps.audio_loader,
            name=cleaned,
            meeting_id=meeting_id,
            cluster_id=cluster_id,
            organization=organization,
            notes=notes,
            title=title,
        )
    except RegistryError as exc:
        raise _translate(exc) from exc
    _record_consent(
        registry,
        result.speaker_id,
        purpose="enrollment",
        source_batch_id=source_batch_id,
    )
    return {
        "speaker_id": result.speaker_id,
        "voiceprint_id": result.voiceprint_id,
        "cluster_id": result.cluster_id,
        "name": result.name,
    }


def update_meeting(
    registry: Registry, meeting_id: int, changes: dict[str, Any]
) -> dict[str, Any]:
    if not changes:
        raise HTTPException(400, "no fields to update")
    if "title" in changes:
        changes["title"] = (changes["title"] or "").strip()
        if not changes["title"]:
            raise HTTPException(400, "meeting title must not be empty")
    if not registry.update_meeting(
        meeting_id,
        title=changes.get("title"),
        topic=changes.get("topic"),
        location=changes.get("location"),
    ):
        raise HTTPException(404, f"meeting {meeting_id} does not exist")
    meeting = registry.get_meeting(meeting_id)
    assert meeting is not None
    return meeting_payload(meeting)


def resolve_cluster_meeting(registry: Registry, cluster_id: int) -> int:
    """The meeting a globally-unique cluster belongs to (404 when absent)."""
    cluster = registry.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(404, f"cluster {cluster_id} does not exist")
    return cluster.meeting_id


def attach_voiceprint(
    registry: Registry,
    deps: ServiceDeps,
    speaker_id: int,
    meeting_id: int,
    cluster_id: int,
    *,
    source_batch_id: int | None = None,
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
    _record_consent(
        registry, speaker_id, purpose="attach", source_batch_id=source_batch_id
    )
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


def resolve_batch(
    registry: Registry,
    deps: ServiceDeps,
    batch_id: int,
    items: list[BatchResolveItem],
) -> dict[str, Any]:
    """Apply the wizard's per-cluster decisions, then close the batch."""
    batch = registry.get_speaker_batch(batch_id)
    if batch is None:
        raise HTTPException(404, f"speaker batch {batch_id} does not exist")
    if batch.state is not BatchState.OPEN:
        raise HTTPException(
            409, f"speaker batch {batch_id} is already {batch.state.value}"
        )
    known = {record.cluster_id: record for record in registry.batch_items(batch_id)}
    for item in items:
        if item.cluster_id not in known:
            raise HTTPException(
                400, f"cluster {item.cluster_id} is not part of batch {batch_id}"
            )
    resolved = 0
    handled: set[int] = set()
    for item in items:
        record = known[item.cluster_id]
        if record.resolution is not None or item.cluster_id in handled:
            continue
        handled.add(item.cluster_id)
        meeting_id = resolve_cluster_meeting(registry, item.cluster_id)
        if item.action == "skip":
            registry.mark_batch_item(batch_id, item.cluster_id, "skipped")
        elif item.action == "enroll":
            name = (item.name or "").strip()
            if not name:
                raise HTTPException(400, "enroll requires a name")
            if item.remember:
                outcome = enroll_new_speaker(
                    registry,
                    deps,
                    name=name,
                    organization=item.organization,
                    notes=None,
                    title=item.title,
                    cluster_id=item.cluster_id,
                    source_batch_id=batch_id,
                )
                speaker_id = int(outcome["speaker_id"])
            else:
                speaker_id = registry.add_speaker(
                    name, item.organization, None, item.title
                )
                try:
                    enrollment.label_cluster(
                        registry,
                        meeting_id=meeting_id,
                        cluster_id=item.cluster_id,
                        speaker_id=speaker_id,
                    )
                except RegistryError as exc:
                    raise _translate(exc) from exc
            registry.mark_batch_item(batch_id, item.cluster_id, "enrolled", speaker_id)
        else:
            if item.speaker_id is None:
                raise HTTPException(400, "attach requires speaker_id")
            if item.remember:
                attach_voiceprint(
                    registry,
                    deps,
                    item.speaker_id,
                    meeting_id,
                    item.cluster_id,
                    source_batch_id=batch_id,
                )
            else:
                try:
                    enrollment.label_cluster(
                        registry,
                        meeting_id=meeting_id,
                        cluster_id=item.cluster_id,
                        speaker_id=item.speaker_id,
                        state=ClusterState.ATTACHED,
                    )
                except RegistryError as exc:
                    raise _translate(exc) from exc
            registry.mark_batch_item(
                batch_id, item.cluster_id, "attached", item.speaker_id
            )
        resolved += 1
    remaining = sum(
        1 for record in registry.batch_items(batch_id) if record.resolution is None
    )
    if remaining == 0:
        registry.close_batch(batch_id)
    current = registry.get_speaker_batch(batch_id)
    assert current is not None
    return {
        "resolved": resolved,
        "remaining": remaining,
        "batch": {
            "id": current.id,
            "meeting_id": current.meeting_id,
            "state": current.state.value,
        },
    }


def re_enroll(
    registry: Registry, deps: ServiceDeps, speaker_id: int, cluster_id: int
) -> dict[str, Any]:
    """Replace a speaker's voiceprints with one fresh capture (re-enroll)."""
    if registry.get_speaker(speaker_id) is None:
        raise HTTPException(404, f"speaker {speaker_id} does not exist")
    meeting_id = resolve_cluster_meeting(registry, cluster_id)
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
    from embed import MODEL_ID, MODEL_REVISION

    registry.delete_stale_voiceprints(speaker_id, MODEL_ID, MODEL_REVISION)
    _record_consent(registry, speaker_id, purpose="re-enroll")
    return {
        "speaker_id": speaker_id,
        "voiceprint_id": result.voiceprint_id,
        "cluster_id": cluster_id,
    }


def export_speaker(registry: Registry, speaker_id: int) -> dict[str, Any]:
    """Portable JSON export of one speaker's metadata, consents and vectors."""
    speaker = registry.get_speaker(speaker_id)
    if speaker is None:
        raise HTTPException(404, f"speaker {speaker_id} does not exist")
    return {
        "speaker": {
            "id": speaker.id,
            "name": speaker.name,
            "organization": speaker.organization,
            "title": speaker.title,
            "notes": speaker.notes,
            "created_at": speaker.created_at,
        },
        "consents": [
            {
                "id": consent.id,
                "granted_at": consent.granted_at,
                "purpose": consent.purpose,
                "retention_until": consent.retention_until,
                "source_batch_id": consent.source_batch_id,
                "revoked_at": consent.revoked_at,
            }
            for consent in registry.consents_for_speaker(speaker_id)
        ],
        "voiceprints": [
            {
                "id": voiceprint.id,
                "model_id": voiceprint.model_id,
                "revision": voiceprint.revision,
                "dim": voiceprint.dim,
                "created_at": voiceprint.created_at,
                "vector_base64": base64.b64encode(
                    voiceprint.vector.astype("<f4").tobytes()
                ).decode(),
            }
            for voiceprint in registry.voiceprints_for_speaker(speaker_id)
        ],
    }


def cluster_sample(
    registry: Registry,
    deps: ServiceDeps,
    cluster_id: int,
    *,
    max_seconds: float = 20.0,
) -> bytes:
    """A short playable WAV of the cluster's longest segments (for naming)."""
    cluster = registry.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(404, f"cluster {cluster_id} does not exist")
    meeting = registry.get_meeting(cluster.meeting_id)
    if (
        meeting is None
        or not meeting.audio_path
        or not Path(meeting.audio_path).is_file()
    ):
        raise HTTPException(404, "meeting audio is unavailable")
    members = [
        segment
        for segment in registry.segments_for_meeting(cluster.meeting_id)
        if segment.cluster_id == cluster_id
    ]
    if not members:
        raise HTTPException(404, "cluster has no segments")
    members.sort(key=lambda item: item.end - item.start, reverse=True)
    chosen = sorted(members[:3], key=lambda item: item.start)
    samples, sample_rate = deps.audio_loader(Path(meeting.audio_path))
    pieces: list[np.ndarray] = []
    for segment in chosen:
        start = max(0, round(segment.start * sample_rate))
        end = min(len(samples), round(segment.end * sample_rate))
        if end > start:
            pieces.append(samples[start:end])
    if not pieces:
        raise HTTPException(404, "cluster has no voiced audio")
    clip = np.concatenate(pieces)[: int(max_seconds * sample_rate)]
    pcm = np.clip(clip, -1.0, 1.0)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return buffer.getvalue()


def _translate(exc: RegistryError) -> HTTPException:
    status = 404 if isinstance(exc, _NOT_FOUND) else 400
    return HTTPException(status, str(exc))
