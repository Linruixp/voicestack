"""HTTP routes: health, the UI root, meetings, jobs and identification.

Reads accept the service bearer token or the UI session cookie; mutations
additionally require the loopback ``Origin`` check when authenticated by
cookie (see :mod:`api_auth`). ``GET /health`` is deliberately unauthenticated:
it is the readiness probe used by the launchers and the UI keepalive.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as FormUploadFile

from api_auth import SESSION_COOKIE, require_mutation_auth, require_read_auth
from api_operations import split_cluster
from api_schemas import IdentifyRequest, SplitRequest
from api_service import (
    ServiceDeps,
    identify_audio,
    job_payload,
    meeting_detail,
    meeting_payload,
    run_meeting,
    save_upload,
)

router = APIRouter()

UI_SESSION_MAX_AGE = 86_400

# Task 25 replaces this placeholder with the five-screen UI. It must never
# embed the service token: the page authenticates via the session cookie only.
_UI_PLACEHOLDER = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>VoiceStudio Speaker Service</title></head>
<body>
<h1>VoiceStudio Speaker Service</h1>
<p>The web UI is built in task 25. This page exists to mint the UI session
cookie; the service token is never served to the browser.</p>
</body>
</html>
"""


def _deps(request: Request) -> ServiceDeps:
    return request.app.state.deps


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
def ui_root(request: Request) -> HTMLResponse:
    """First page load: mint the server-side session cookie for the UI."""
    response = HTMLResponse(_UI_PLACEHOLDER)
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.sessions.mint(),
        max_age=UI_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/meetings", dependencies=[Depends(require_read_auth)])
def list_meetings(request: Request) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return {
            "meetings": [meeting_payload(item) for item in registry.list_meetings()]
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
    deps = _deps(request)
    path = save_upload(deps.settings.data_dir / "uploads", file.filename, content)
    with request.app.state.registry_factory() as registry:
        return run_meeting(registry, deps, path, title or path.stem).to_dict()


@router.get("/meetings/{meeting_id}", dependencies=[Depends(require_read_auth)])
def get_meeting(request: Request, meeting_id: int) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return meeting_detail(registry, meeting_id)


@router.post(
    "/meetings/{meeting_id}/clusters/{cluster_id}/split",
    dependencies=[Depends(require_mutation_auth)],
)
def split_meeting_cluster(
    request: Request, meeting_id: int, cluster_id: int, body: SplitRequest
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return split_cluster(registry, meeting_id, cluster_id, body.at)


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
