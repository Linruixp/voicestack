"""Local (Ollama) Chinese meeting summarization behind a pluggable backend.

The backend is injectable so tests run without a model. The Ollama backend uses
JSON-schema-constrained decoding (``format``), and the structured result is
persisted to ``meetings.summary_json`` with ``ensure_ascii=False`` so Chinese
stays searchable. Only stdlib + Ollama are used (no extra runtime dependency).
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from registry import Registry
from registry_models import MeetingNotFoundError, Segment

__all__ = [
    "ActionItem",
    "Chapter",
    "OllamaBackend",
    "SummaryBackend",
    "SummaryPayload",
    "build_transcript",
    "summarize_meeting",
]


class ActionItem(BaseModel):
    text: str
    owner: str | None = None
    due: str | None = None


class Chapter(BaseModel):
    title: str
    start: float


class SummaryPayload(BaseModel):
    tldr: str
    decisions: list[str] = []
    action_items: list[ActionItem] = []
    chapters: list[Chapter] = []


SUMMARY_SCHEMA = SummaryPayload.model_json_schema()

SYSTEM_PROMPT = (
    "你是中文会议摘要助手。只输出符合 JSON schema 的结果，不要输出多余文字。"
    "字段：tldr（1-2 句中文摘要）、decisions（关键决策字符串数组）、"
    "action_items（每项 text/owner/due，未明确的负责人或截止用 null）、"
    "chapters（每项 title 与以秒为单位的 start）。只依据转写内容，不要编造。"
)


class SummaryBackend(Protocol):
    def generate(self, *, system: str, user: str) -> str: ...


class OllamaBackend:
    """Chat against a local Ollama server with JSON-schema-constrained output."""

    def __init__(self, base_url: str, model: str, timeout_s: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def generate(self, *, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "think": False,
                "format": SUMMARY_SCHEMA,
                "keep_alive": "30m",
                "options": {"temperature": 0.2, "num_ctx": 32768},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        # Bypass any HTTP_PROXY env so a loopback call is never proxied.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ValueError("ollama response missing message.content")
        return content


MAX_TRANSCRIPT_CHARS = 20_000


def build_transcript(
    segments: Sequence[Segment],
    names: dict[int, str],
    *,
    max_chars: int = MAX_TRANSCRIPT_CHARS,
) -> str:
    lines: list[str] = []
    total = 0
    for segment in segments:
        who = names.get(segment.speaker_id) or "未知"
        minutes, seconds = divmod(int(segment.start), 60)
        line = f"[{minutes:02d}:{seconds:02d}] {who}：{segment.text}"
        lines.append(line)
        total += len(line) + 1
        if total >= max_chars:
            lines.append("…（转写过长，已截断）")
            break
    return "\n".join(lines)


def summarize_meeting(
    registry: Registry, meeting_id: int, backend: SummaryBackend
) -> dict[str, object]:
    meeting = registry.get_meeting(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(meeting_id)
    segments = registry.segments_for_meeting(meeting_id)
    names = {speaker.id: speaker.name for speaker in registry.list_speakers()}
    raw = backend.generate(system=SYSTEM_PROMPT, user=build_transcript(segments, names))
    payload = SummaryPayload.model_validate_json(raw)
    registry.update_meeting(
        meeting_id, summary_json=json.dumps(payload.model_dump(), ensure_ascii=False)
    )
    return payload.model_dump()
