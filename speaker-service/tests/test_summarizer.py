"""Unit tests for the pluggable summarizer (no model required).

Given: a meeting with a transcript and a fake summary backend.
When: ``summarize_meeting`` runs.
Then: the structured result is parsed, persisted with raw Chinese, and the
backend saw the transcript text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import summarizer
from registry import NewSegment, open_registry


class FakeBackend:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.seen: str | None = None

    def generate(self, *, system: str, user: str) -> str:
        self.seen = user
        return self.payload


def test_summarize_meeting_parses_and_persists(db_path: Path) -> None:
    payload = (
        '{"tldr":"讨论了排期","decisions":["下周上线"],'
        '"action_items":[{"text":"写测试","owner":"张三","due":null}],'
        '"chapters":[{"title":"排期","start":0.0}]}'
    )
    with open_registry(db_path) as registry:
        meeting_id = registry.create_meeting("规划会")
        registry.add_segment(
            meeting_id, NewSegment(0.0, 2.0, "我们讨论排期", None, None)
        )
        backend = FakeBackend(payload)
        # When: the meeting is summarized
        result = summarizer.summarize_meeting(registry, meeting_id, backend)
        # Then: the structured fields round-trip
        assert result["tldr"] == "讨论了排期"
        assert result["action_items"][0]["owner"] == "张三"
        # And: raw Chinese is stored unescaped so P2b search can match it
        stored = registry.get_meeting(meeting_id).summary_json
        assert "讨论了排期" in stored
        assert "\\u" not in stored
        # And: the backend saw the transcript
        assert "我们讨论排期" in backend.seen


def test_build_transcript_formats_and_truncates(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        meeting_id = registry.create_meeting("M")
        registry.add_segment(meeting_id, NewSegment(65.0, 66.0, "你好", None, None))
        segments = registry.segments_for_meeting(meeting_id)
        # Then: [mm:ss] + speaker formatting
        assert summarizer.build_transcript(segments, {}) == "[01:05] 未知：你好"
        # And: an over-long transcript is truncated with a marker
        assert "已截断" in summarizer.build_transcript(segments, {}, max_chars=5)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.request = None

    def open(self, request: object, timeout: float | None = None) -> _FakeResponse:
        self.request = request
        return self.response


def test_ollama_backend_builds_constrained_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"message": {"content": '{"tldr":"x"}'}}).encode("utf-8")
    opener = _FakeOpener(_FakeResponse(body))
    monkeypatch.setattr(
        summarizer.urllib.request, "build_opener", lambda handler=None: opener
    )
    backend = summarizer.OllamaBackend("http://localhost:11434", "qwen3.5:9b")
    # Then: the response content is returned
    assert backend.generate(system="s", user="u") == '{"tldr":"x"}'
    # And: the request is constrained and sized for long meetings
    sent = json.loads(opener.request.data.decode("utf-8"))
    assert sent["model"] == "qwen3.5:9b"
    assert sent["stream"] is False
    assert sent["think"] is False
    assert sent["format"] == summarizer.SUMMARY_SCHEMA
    assert sent["options"]["num_ctx"] == 32768


def test_ollama_backend_rejects_missing_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _FakeOpener(_FakeResponse(b'{"error":"boom"}'))
    monkeypatch.setattr(
        summarizer.urllib.request, "build_opener", lambda handler=None: opener
    )
    backend = summarizer.OllamaBackend("http://localhost:11434", "qwen3.5:9b")
    with pytest.raises(ValueError):
        backend.generate(system="s", user="u")
