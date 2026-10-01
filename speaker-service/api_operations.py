"""Cross-aggregate mutations behind the speaker and cluster routes.

Speaker merge, cluster split and segment relabel are not exposed by the frozen
registry API, so they execute here through the registry's own SQLite
connection, each inside a single transaction. ``update_speaker`` and
``attach_voiceprint`` follow the same rule for the fields no registry method
covers. The registry's documented ON DELETE policy remains the only deletion
path (merge deletes a speaker row after reassigning every reference).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

import pipeline
from api_service import ServiceDeps, speaker_payload
from registry import ClusterLink, ClusterState, Registry


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
    speaker = registry.get_speaker(speaker_id)
    if speaker is None:
        raise HTTPException(404, f"speaker {speaker_id} does not exist")
    meeting = registry.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, f"meeting {meeting_id} does not exist")
    cluster = next(
        (
            item
            for item in registry.clusters_for_meeting(meeting_id)
            if item.id == cluster_id
        ),
        None,
    )
    if cluster is None:
        raise HTTPException(
            404, f"cluster {cluster_id} does not belong to meeting {meeting_id}"
        )
    if not meeting.audio_path or not Path(meeting.audio_path).is_file():
        raise HTTPException(400, f"meeting {meeting_id} has no available audio")
    segments = [
        item
        for item in registry.segments_for_meeting(meeting_id)
        if item.cluster_id == cluster_id
    ]
    samples, sample_rate = deps.audio_loader(Path(meeting.audio_path))
    members = []
    for segment in segments:
        start = max(0, round(segment.start * sample_rate))
        end = min(len(samples), round(segment.end * sample_rate))
        if end - start >= int(pipeline.MIN_EMBED_SECONDS * sample_rate):
            members.append(
                deps.embedder.embed_waveform(samples[start:end], sample_rate)
            )
    if not members:
        raise HTTPException(400, f"cluster {cluster_id} has no embeddable segments")
    embedding = deps.embedder.embed_cluster(members)
    prior = registry.voiceprints_for_speaker(speaker_id)
    similarity = None
    if prior:
        from embed import cosine_similarity

        similarity = max(
            cosine_similarity(embedding.vector, item.vector) for item in prior
        )
    voiceprint_id = registry.add_voiceprint(speaker_id, embedding)
    registry.update_cluster(cluster_id, ClusterState.ATTACHED, speaker.name)
    registry.add_meeting_speaker(
        ClusterLink(
            meeting_id=meeting_id,
            cluster_id=cluster_id,
            speaker_id=speaker_id,
            confidence=similarity,
        )
    )
    with registry._conn as conn:
        conn.execute(
            "UPDATE segments SET speaker_id = ? WHERE cluster_id = ?",
            (speaker_id, cluster_id),
        )
    return {
        "speaker_id": speaker_id,
        "voiceprint_id": voiceprint_id,
        "cluster_id": cluster_id,
        "similarity": similarity,
    }


def merge_speakers(
    registry: Registry, target_id: int, source_id: int
) -> dict[str, Any]:
    """Fold ``source_id`` into ``target_id``: move every reference, drop source."""
    target = registry.get_speaker(target_id)
    if target is None:
        raise HTTPException(404, f"speaker {target_id} does not exist")
    if registry.get_speaker(source_id) is None:
        raise HTTPException(404, f"speaker {source_id} does not exist")
    if target_id == source_id:
        raise HTTPException(400, "cannot merge a speaker into itself")
    with registry._conn as conn:
        voiceprints = conn.execute(
            "UPDATE voiceprints SET speaker_id = ? WHERE speaker_id = ?",
            (target_id, source_id),
        ).rowcount
        links = conn.execute(
            "UPDATE meeting_speakers SET speaker_id = ? WHERE speaker_id = ?",
            (target_id, source_id),
        ).rowcount
        segments = conn.execute(
            "UPDATE segments SET speaker_id = ? WHERE speaker_id = ?",
            (target_id, source_id),
        ).rowcount
        conn.execute(
            "UPDATE clusters SET label = ? WHERE id IN"
            " (SELECT cluster_id FROM meeting_speakers WHERE speaker_id = ?)",
            (target.name, target_id),
        )
        conn.execute("DELETE FROM speakers WHERE id = ?", (source_id,))
    return {
        "target_speaker_id": target_id,
        "source_speaker_id": source_id,
        "voiceprints_moved": voiceprints,
        "links_moved": links,
        "segments_moved": segments,
    }


def split_cluster(
    registry: Registry, meeting_id: int, cluster_id: int, at: float
) -> dict[str, Any]:
    """Move the cluster's segments starting at/after ``at`` into a new cluster."""
    if registry.get_meeting(meeting_id) is None:
        raise HTTPException(404, f"meeting {meeting_id} does not exist")
    cluster = next(
        (
            item
            for item in registry.clusters_for_meeting(meeting_id)
            if item.id == cluster_id
        ),
        None,
    )
    if cluster is None:
        raise HTTPException(
            404, f"cluster {cluster_id} does not belong to meeting {meeting_id}"
        )
    segments = [
        item
        for item in registry.segments_for_meeting(meeting_id)
        if item.cluster_id == cluster_id
    ]
    moved = [item for item in segments if item.start >= at]
    if not moved or len(moved) == len(segments):
        raise HTTPException(400, "split point must fall between two segments")
    new_cluster_id = registry.add_cluster(meeting_id, state=ClusterState.UNKNOWN)
    registry.add_meeting_speaker(
        ClusterLink(meeting_id=meeting_id, cluster_id=new_cluster_id)
    )
    placeholders = ", ".join("?" * len(moved))
    with registry._conn as conn:
        conn.execute(
            "UPDATE segments SET cluster_id = ?, speaker_id = NULL"
            f" WHERE id IN ({placeholders})",
            (new_cluster_id, *[item.id for item in moved]),
        )
    return {
        "cluster_id": cluster_id,
        "new_cluster_id": new_cluster_id,
        "moved_segments": len(moved),
    }
