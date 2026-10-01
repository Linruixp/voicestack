"""Structured, serialisable failures surfaced to the MCP client.

Every bridge failure becomes a JSON object — never a hang and never an unhandled
exception past the tool boundary.
"""

from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    """A failure with a stable machine-readable ``code`` and ``details``."""

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
