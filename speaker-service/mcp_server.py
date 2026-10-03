"""stdio MCP server that proxies the speaker-service HTTP API (:3910).

Thin proxy only: it never opens the SQLite registry, so the HTTP service stays
the single writer. Each tool call first runs ``vs-speaker-up.sh`` (the service
may have idle-exited) and waits for ``/health`` before forwarding over HTTP.
Failures are structured JSON payloads with ``is_error=True`` - never a hang.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from functools import partial
from typing import Any

from anyio import Event, create_task_group, move_on_after, to_thread
from mcp import types
from mcp.server.mcpserver import Context, MCPServer

from mcp_proxy import BridgeError, ProxyConfig
from service_client import ServiceClient

SERVER_NAME = "vs-speaker"
SERVER_VERSION = "0.1.0"

PROGRESS_INTERVAL_S = float(os.environ.get("VASTACK_MCP_PROGRESS_INTERVAL", "20"))

INSTRUCTIONS = (
    "VoiceStudio speaker-identity service (on-demand, loopback HTTP :3910). Each "
    "call starts the service via vs-speaker-up.sh when it is down and waits for "
    "/health before proxying. Use transcribe_meeting to transcribe a recording "
    "into speaker-labelled segments; list_speakers / enroll_speaker / "
    "attach_to_speaker / rename_speaker to manage the speaker directory; "
    "rename_meeting to edit a meeting title; open_speaker_ui to open the naming "
    "UI for a meeting; "
    "identify_speaker to match a clip; get_meeting to re-read a transcript."
)


def _ok(payload: Any) -> types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def _error(exc: BridgeError) -> types.CallToolResult:
    text = json.dumps(exc.to_dict(), ensure_ascii=False)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=True
    )


async def _dispatch(call: Any, /, **kwargs: Any) -> types.CallToolResult:
    """Run a blocking client call off the event loop; never raise past the boundary."""
    try:
        payload = await to_thread.run_sync(partial(call, **kwargs))
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - boundary safety net must not hang
        return _error(BridgeError("unexpected_error", f"{type(exc).__name__}: {exc}"))
    return _ok(payload)


async def _run_with_progress(
    call: Callable[[], Any], ctx: Context, interval: float = PROGRESS_INTERVAL_S
) -> types.CallToolResult:
    """Run ``call`` in a worker thread, emitting progress heartbeats meanwhile.

    Long transcriptions otherwise trip the client's 60s request timeout; each
    heartbeat resets it (``resetTimeoutOnProgress``). Never raises, never hangs.
    """
    outcome: dict[str, Any] = {}
    done = Event()

    async def work() -> None:
        try:
            outcome["payload"] = await to_thread.run_sync(call)
        except BridgeError as exc:
            outcome["error"] = exc
        except Exception as exc:  # noqa: BLE001 - boundary safety net must not hang
            outcome["error"] = BridgeError(
                "unexpected_error", f"{type(exc).__name__}: {exc}"
            )
        finally:
            done.set()

    async with create_task_group() as tg:
        tg.start_soon(work)
        elapsed = 0.0
        while True:
            with move_on_after(interval):
                await done.wait()
            if done.is_set():
                break
            elapsed += interval
            try:
                await ctx.report_progress(
                    elapsed, None, f"transcribing… {elapsed:.0f}s elapsed"
                )
            except Exception:  # noqa: BLE001 - progress is best-effort; never fail
                pass

    if "error" in outcome:
        return _error(outcome["error"])
    return _ok(outcome["payload"])


def build_server(client: ServiceClient | None = None) -> MCPServer:
    proxy = client or ServiceClient(ProxyConfig.from_env())
    server = MCPServer(
        name=SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS
    )

    @server.tool(
        description="Transcribe a meeting recording (audio file path) and return "
        "its speaker-labelled segments. Cold-starts the service if needed."
    )
    async def transcribe_meeting(file_path: str, ctx: Context) -> types.CallToolResult:
        return await _run_with_progress(
            partial(proxy.transcribe_meeting, file_path=file_path), ctx
        )

    @server.tool(
        description="List the enrolled speakers (id, name, organization, notes, "
        "voiceprint count)."
    )
    async def list_speakers() -> types.CallToolResult:
        return await _dispatch(proxy.list_speakers)

    @server.tool(
        description="Create a NEW speaker from a diarized cluster id and enroll "
        "its voiceprint atomically."
    )
    async def enroll_speaker(
        name: str,
        cluster_id: int,
        organization: str | None = None,
        notes: str | None = None,
    ) -> types.CallToolResult:
        return await _dispatch(
            proxy.enroll_speaker,
            name=name,
            organization=organization,
            notes=notes,
            cluster_id=cluster_id,
        )

    @server.tool(
        description="Attach a diarized cluster's voiceprint to an EXISTING speaker "
        "(never creates a new speaker)."
    )
    async def attach_to_speaker(
        speaker_id: int, cluster_id: int
    ) -> types.CallToolResult:
        return await _dispatch(
            proxy.attach_to_speaker, speaker_id=speaker_id, cluster_id=cluster_id
        )

    @server.tool(
        description="Identify the speaker in an audio file; returns known / "
        "unknown / re_enroll_required with the best match."
    )
    async def identify_speaker(audio_path: str) -> types.CallToolResult:
        return await _dispatch(proxy.identify_speaker, audio_path=audio_path)

    @server.tool(
        description="Fetch a meeting transcript by id: meeting, segments, "
        "speakers, unknown clusters and links."
    )
    async def get_meeting(meeting_id: int) -> types.CallToolResult:
        return await _dispatch(proxy.get_meeting, meeting_id=meeting_id)

    @server.tool(description="Rename an existing speaker.")
    async def rename_speaker(speaker_id: int, name: str) -> types.CallToolResult:
        return await _dispatch(proxy.rename_speaker, speaker_id=speaker_id, name=name)

    @server.tool(description="Rename an existing meeting (edits its title).")
    async def rename_meeting(meeting_id: int, title: str) -> types.CallToolResult:
        return await _dispatch(proxy.rename_meeting, meeting_id=meeting_id, title=title)

    @server.tool(
        description="Open the local speaker Web UI for a meeting (naming wizard)."
    )
    async def open_speaker_ui(meeting_id: int) -> types.CallToolResult:
        return await _dispatch(proxy.open_speaker_ui, meeting_id=meeting_id)

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
