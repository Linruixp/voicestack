"""Unit tests for config, error serialisation and the tool catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from vsbridge.catalog import ToolCatalog
from vsbridge.config import BridgeConfig
from vsbridge.errors import BridgeError

LIVE_TOOLS = {
    "generate_speech",
    "list_voices",
    "list_personalities",
    "list_languages",
    "transcribe",
    "check_health",
    "clone_voice",
}


def test_config_defaults() -> None:
    cfg = BridgeConfig.from_env()
    assert cfg.port == 3900
    assert cfg.base_url == "http://127.0.0.1:3900"
    assert cfg.mcp_url == "http://127.0.0.1:3900/mcp/"
    assert cfg.ensure_up_script.name == "vs-ensure-up.sh"
    assert cfg.ensure_up_enabled is True


def test_bridge_error_serialises_structured_payload() -> None:
    payload = BridgeError("dead", "no host", {"port": 3999}).to_dict()
    assert payload == {
        "ok": False,
        "error": {"code": "dead", "message": "no host", "details": {"port": 3999}},
    }


def test_packaged_catalog_mirrors_live_tool_surface() -> None:
    tools = ToolCatalog(Path("/nonexistent/catalog.json")).load()
    names = {t["name"] for t in tools}
    assert names == LIVE_TOOLS, f"catalog drifted from live surface: {names}"
    # The catalog is live-derived, so every entry carries a real input schema.
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"


def test_catalog_roundtrip(tmp_path: Path) -> None:
    catalog = ToolCatalog(tmp_path / "catalog.json")
    sample = [{"name": "x", "description": "d", "inputSchema": {"type": "object"}}]
    catalog.save(sample)
    assert catalog.load() == sample


def test_catalog_falls_back_to_packaged_when_cache_missing(tmp_path: Path) -> None:
    catalog = ToolCatalog(tmp_path / "missing.json")
    assert {t["name"] for t in catalog.load()} == LIVE_TOOLS


def test_catalog_raises_when_nothing_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vsbridge.catalog as mod

    monkeypatch.setattr(mod, "_PACKAGED_CATALOG", tmp_path / "none.json")
    with pytest.raises(BridgeError) as exc:
        ToolCatalog(tmp_path / "none.json").load()
    assert exc.value.code == "catalog_unavailable"
