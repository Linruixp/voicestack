"""Live-derived tool catalog used as the ``tools/list`` fallback.

When VoiceStudio is already up, ``tools/list`` is forwarded live and the result
is cached here. When it is down, the cached catalog (or the packaged snapshot
captured from a live server) answers ``tools/list`` — so listing never
cold-starts the app, and the surface always mirrors what the server returned.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import BridgeError

_PACKAGED_CATALOG = Path(__file__).with_name("catalog.json")


class ToolCatalog:
    """Persisted snapshot of VoiceStudio's ``tools/list`` result."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        for candidate in (self.path, _PACKAGED_CATALOG):
            data = self._read(candidate)
            if data is not None:
                tools = data.get("tools")
                if isinstance(tools, list) and tools:
                    return tools
        raise BridgeError(
            "catalog_unavailable",
            "no VoiceStudio tool catalog available and the server is down",
            {"path": str(self.path), "packaged": str(_PACKAGED_CATALOG)},
        )

    def save(self, tools: list[dict[str, Any]]) -> None:
        """Best-effort atomic cache write; never fails the caller."""
        payload = {
            "generated_by": "vsbridge",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "live tools/list",
            "count": len(tools),
            "tools": tools,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                prefix=".catalog-",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            )
            with handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(handle.name, self.path)
        except OSError:
            return

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
