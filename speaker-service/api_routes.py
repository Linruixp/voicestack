"""HTTP routes: health, the UI root, meetings, jobs and identification.

Reads accept the service bearer token or the UI session cookie; mutations
additionally require the loopback ``Origin`` check when authenticated by
cookie (see :mod:`api_auth`). ``GET /health`` is deliberately unauthenticated:
it is the readiness probe used by the launchers and the UI keepalive.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from starlette.datastructures import UploadFile as FormUploadFile
from starlette.responses import FileResponse

import asr
from api_auth import require_mutation_auth, require_read_auth
from api_operations import (
    cluster_sample,
    split_cluster,
    update_meeting,
    update_segment,
)
from api_schemas import IdentifyRequest, MeetingPatch, SegmentPatch, SplitRequest
from registry_models import MeetingNotFoundError
from summarizer import summarize_meeting
from api_service import (
    ServiceDeps,
    identify_audio,
    job_payload,
    list_meeting_rows,
    meeting_detail,
    meeting_result_payload,
    run_meeting,
    save_upload,
)

router = APIRouter()

# Extensions the pipeline is expected to decode; the UI advertises the common
# four. An upload whose name and declared type both say "not audio" is refused
# before a byte is written (415) or any meeting/job row is created.
_AUDIO_SUFFIXES = frozenset(
    {
        ".3gp",
        ".aac",
        ".aif",
        ".aifc",
        ".aiff",
        ".amr",
        ".caf",
        ".flac",
        ".m4a",
        ".m4b",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wave",
        ".webm",
        ".wma",
    }
)


def _ensure_audio_upload(upload: UploadFile) -> None:
    """Reject an upload that is clearly not audio (typed 415, nothing written)."""
    suffix = Path(upload.filename or "").suffix.lower()
    media_type = (upload.content_type or "").split(";")[0].strip().lower()
    if suffix in _AUDIO_SUFFIXES:
        return
    if media_type.startswith(("audio/", "video/")):
        return
    # Unknown binary types go to the decoder, which reports undecodable input
    # as a typed 422; known non-media types (text/*, application/pdf, ...) are
    # refused here.
    if media_type == "application/octet-stream":
        return
    raise HTTPException(
        415,
        f"uploaded file {upload.filename!r} is not audio: use an audio file "
        "extension (.wav, .mp3, .m4a, .flac, ...) or an audio/* content type",
    )


def _deps(request: Request) -> ServiceDeps:
    return request.app.state.deps


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/meetings", dependencies=[Depends(require_read_auth)])
def list_meetings(
    request: Request,
    q: str | None = None,
    unresolved: bool = False,
    participant_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return {
            "meetings": list_meeting_rows(
                registry,
                q=q,
                unresolved=unresolved,
                participant_id=participant_id,
                date_from=date_from,
                date_to=date_to,
            )
        }


@router.post(
    "/meetings", status_code=201, dependencies=[Depends(require_mutation_auth)]
)
def upload_meeting(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),
) -> dict[str, object]:
    """Accept a meeting recording, run the pipeline, return the meeting result."""
    content = file.file.read()
    if not content:
        raise HTTPException(400, "uploaded audio is empty")
    _ensure_audio_upload(file)
    deps = _deps(request)
    path = save_upload(deps.settings.data_dir / "uploads", file.filename, content)
    try:
        with request.app.state.registry_factory() as registry:
            result = run_meeting(registry, deps, path, title or path.stem)
        return meeting_result_payload(result, deps.settings)
    except asr.InvalidAudioError as exc:
        # Pre-transcription failure: the pipeline rolled back its meeting and
        # job, so only the upload is left to remove.
        path.unlink(missing_ok=True)
        raise HTTPException(422, "uploaded file is not decodable audio") from exc


@router.get("/meetings/{meeting_id}", dependencies=[Depends(require_read_auth)])
def get_meeting(request: Request, meeting_id: int) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        detail = meeting_detail(registry, meeting_id)
    batch_id = detail["handoff"]["batch_id"]
    detail["handoff"]["threshold"] = request.app.state.settings.handoff_threshold
    if batch_id is not None:
        base = request.app.state.settings.resolved_ui_base_url
        detail["ui_url"] = (
            f"{base}/?meeting={meeting_id}&task=speakers&batch={batch_id}"
        )
    else:
        detail["ui_url"] = None
    return detail


@router.patch("/meetings/{meeting_id}", dependencies=[Depends(require_mutation_auth)])
def patch_meeting(
    request: Request, meeting_id: int, body: MeetingPatch
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return update_meeting(registry, meeting_id, body.model_dump(exclude_unset=True))


@router.post(
    "/meetings/{meeting_id}/summary",
    dependencies=[Depends(require_mutation_auth)],
)
async def generate_summary(request: Request, meeting_id: int) -> dict[str, object]:
    deps = _deps(request)
    if deps.summarizer is None:
        raise HTTPException(503, "summarizer is not configured")

    def work() -> dict[str, object]:
        with request.app.state.registry_factory() as registry:
            return summarize_meeting(registry, meeting_id, deps.summarizer)

    try:
        return await run_in_threadpool(work)
    except MeetingNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(502, f"summarizer failed: {exc}") from exc


@router.post(
    "/meetings/{meeting_id}/clusters/{cluster_id}/split",
    dependencies=[Depends(require_mutation_auth)],
)
def split_meeting_cluster(
    request: Request, meeting_id: int, cluster_id: int, body: SplitRequest
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return split_cluster(registry, meeting_id, cluster_id, body.at)


@router.get("/clusters/{cluster_id}/sample", dependencies=[Depends(require_read_auth)])
def cluster_audio_sample(request: Request, cluster_id: int) -> Response:
    with request.app.state.registry_factory() as registry:
        data = cluster_sample(registry, _deps(request), cluster_id)
    return Response(content=data, media_type="audio/wav")


@router.get("/meetings/{meeting_id}/audio", dependencies=[Depends(require_read_auth)])
def stream_meeting_audio(request: Request, meeting_id: int) -> FileResponse:
    with request.app.state.registry_factory() as registry:
        meeting = registry.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, f"meeting {meeting_id} does not exist")
    if not meeting.audio_path or not Path(meeting.audio_path).is_file():
        raise HTTPException(404, "meeting audio is unavailable")
    media = mimetypes.guess_type(meeting.audio_path)[0] or "audio/mpeg"
    return FileResponse(meeting.audio_path, media_type=media)


@router.patch(
    "/meetings/{meeting_id}/segments/{segment_id}",
    dependencies=[Depends(require_mutation_auth)],
)
def patch_segment(
    request: Request, meeting_id: int, segment_id: int, body: SegmentPatch
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return update_segment(registry, meeting_id, segment_id, body.text)


@router.delete("/meetings", dependencies=[Depends(require_mutation_auth)])
def delete_all_meetings(request: Request) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        meetings = registry.list_meetings()
        paths = [item.audio_path for item in meetings if item.audio_path]
        for meeting in meetings:
            registry.delete_meeting(meeting.id)
    for path in paths:
        Path(path).unlink(missing_ok=True)
    return {"deleted": len(meetings)}


@router.delete("/meetings/{meeting_id}", dependencies=[Depends(require_mutation_auth)])
def delete_meeting(request: Request, meeting_id: int) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        meeting = registry.get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(404, f"meeting {meeting_id} does not exist")
        audio_path = meeting.audio_path
        registry.delete_meeting(meeting_id)
    if audio_path:
        Path(audio_path).unlink(missing_ok=True)
    return {"deleted": True, "meeting_id": meeting_id}


@router.get("/jobs/{job_id}", dependencies=[Depends(require_read_auth)])
def get_job(request: Request, job_id: int) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        job = registry.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"job {job_id} does not exist")
        return job_payload(job)


@router.post("/identify", dependencies=[Depends(require_mutation_auth)])
async def identify(request: Request) -> dict[str, object]:
    """Identify a clip from a JSON ``audio_path`` or a multipart ``file``."""
    deps = _deps(request)
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    uploaded: Path | None = None
    if media_type == "application/json":
        try:
            payload = IdentifyRequest.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        path = Path(payload.audio_path)
    elif media_type == "multipart/form-data":
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, FormUploadFile):
            raise HTTPException(422, "multipart identify requires a 'file' field")
        path = save_upload(
            deps.settings.data_dir / "uploads", upload.filename, await upload.read()
        )
        uploaded = path
    else:
        raise HTTPException(
            415, "identify accepts application/json or multipart/form-data"
        )

    def work() -> dict[str, object]:
        with request.app.state.registry_factory() as registry:
            return identify_audio(registry, deps, path)

    try:
        return await run_in_threadpool(work)
    finally:
        if uploaded is not None:
            uploaded.unlink(missing_ok=True)
