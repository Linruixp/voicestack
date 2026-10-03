"""Speaker-batch routes: read a pending naming batch and resolve it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from api_auth import require_mutation_auth, require_read_auth
from api_operations import resolve_batch
from api_schemas import BatchResolveRequest

router = APIRouter(prefix="/speaker-batches")


@router.get("/{batch_id}", dependencies=[Depends(require_read_auth)])
def get_batch(request: Request, batch_id: int) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        batch = registry.get_speaker_batch(batch_id)
        if batch is None:
            raise HTTPException(404, f"speaker batch {batch_id} does not exist")
        items = []
        for item in registry.batch_items(batch_id):
            cluster = registry.get_cluster(item.cluster_id)
            members = [
                segment
                for segment in registry.segments_for_meeting(batch.meeting_id)
                if segment.cluster_id == item.cluster_id
            ]
            items.append(
                {
                    "cluster_id": item.cluster_id,
                    "label": cluster.label if cluster is not None else None,
                    "segment_count": len(members),
                    "start": min((s.start for s in members), default=0.0),
                    "end": max((s.end for s in members), default=0.0),
                    "suggested_speaker_id": item.suggested_speaker_id,
                    "similarity": item.similarity,
                    "resolution": item.resolution,
                    "sample_url": f"/clusters/{item.cluster_id}/sample",
                }
            )
        return {
            "batch": {
                "id": batch.id,
                "meeting_id": batch.meeting_id,
                "state": batch.state.value,
                "created_at": batch.created_at,
                "resolved_at": batch.resolved_at,
            },
            "items": items,
        }


@router.post("/{batch_id}/resolve", dependencies=[Depends(require_mutation_auth)])
def resolve(
    request: Request, batch_id: int, body: BatchResolveRequest
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return resolve_batch(registry, request.app.state.deps, batch_id, body.items)
