"""Unit tests for config, error serialisation and stale-lock self-healing."""

from __future__ import annotations

from pathlib import Path

from voicebridge.client import BridgeError, VoiceStudioClient
from voicebridge.config import BridgeConfig


def test_config_defaults() -> None:
    cfg = BridgeConfig.from_env()
    assert cfg.port == 3900
    assert cfg.base_url == "http://127.0.0.1:3900"
    assert cfg.ensure_up_script.name == "vs-ensure-up.sh"


def test_bridge_error_serialises_structured_payload() -> None:
    payload = BridgeError("dead", "no host", {"port": 3999}).to_dict()
    assert payload == {
        "ok": False,
        "error": {"code": "dead", "message": "no host", "details": {"port": 3999}},
    }


def test_clear_stale_lock_removes_orphaned_lock(monkeypatch) -> None:
    monkeypatch.setenv("VS_BASE_URL", "http://127.0.0.1:3955")
    cfg = BridgeConfig.from_env()
    lock = Path(f"/tmp/vs-ensure-up.lock.{cfg.port}")
    lock.mkdir(exist_ok=True)
    try:
        VoiceStudioClient(cfg)._clear_stale_lock()
        assert not lock.exists()
    finally:
        if lock.exists():
            lock.rmdir()
