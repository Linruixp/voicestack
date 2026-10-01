"""Speaker directory routes: CRUD, voiceprint attachment and merge."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from api_auth import require_mutation_auth, require_read_auth
from api_operations import attach_voiceprint, merge_speakers, update_speaker
from api_schemas import (
    MergeRequest,
    SpeakerCreate,
    SpeakerPatch,
    VoiceprintAttach,
)
from api_service import speaker_payload
from registry_models import RegistryError

router = APIRouter(prefix="/speakers")


@router.get("", dependencies=[Depends(require_read_auth)])
def list_speakers(request: Request) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return {
            "speakers": [
                speaker_payload(
                    speaker, len(registry.voiceprints_for_speaker(speaker.id))
                )
                for speaker in registry.list_speakers()
            ]
        }


@router.post("", status_code=201, dependencies=[Depends(require_mutation_auth)])
def create_speaker(request: Request, body: SpeakerCreate) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        try:
            speaker_id = registry.add_speaker(body.name, body.organization, body.notes)
        except RegistryError as exc:
            raise HTTPException(400, str(exc)) from exc
        speaker = registry.get_speaker(speaker_id)
        assert speaker is not None
        return speaker_payload(speaker, 0)


@router.patch("/{speaker_id}", dependencies=[Depends(require_mutation_auth)])
def patch_speaker(
    request: Request, speaker_id: int, body: SpeakerPatch
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return update_speaker(registry, speaker_id, body.model_dump(exclude_unset=True))


@router.delete("/{speaker_id}", dependencies=[Depends(require_mutation_auth)])
def delete_speaker(request: Request, speaker_id: int) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        if not registry.delete_speaker(speaker_id):
            raise HTTPException(404, f"speaker {speaker_id} does not exist")
    return {"deleted": True, "speaker_id": speaker_id}


@router.post(
    "/{speaker_id}/voiceprints",
    status_code=201,
    dependencies=[Depends(require_mutation_auth)],
)
def attach_cluster_voiceprint(
    request: Request, speaker_id: int, body: VoiceprintAttach
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return attach_voiceprint(
            registry,
            request.app.state.deps,
            speaker_id,
            body.meeting_id,
            body.cluster_id,
        )


@router.post("/{speaker_id}/merge", dependencies=[Depends(require_mutation_auth)])
def merge_speaker(
    request: Request, speaker_id: int, body: MergeRequest
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return merge_speakers(registry, speaker_id, body.source_speaker_id)
