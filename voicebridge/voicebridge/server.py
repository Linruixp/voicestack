"""voicebridge MCP server: audiobook + translation over VoiceStudio HTTP (stdio)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import BridgeError, VoiceStudioClient
from .config import BridgeConfig

SERVER_NAME = "voicebridge"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "VoiceStudio audiobook + translation bridge. VoiceStudio runs on-demand: each "
    "tool call starts it via vs-ensure-up.sh and waits for :3900. Use "
    "generate_audiobook to render a document to .m4b/.mp3, and translate_text for "
    "offline (Argos) text translation. TTS, voice cloning and transcription live "
    "on VoiceStudio's own MCP, not here."
)


def _guard(fn: Any) -> Any:
    """Wrap a tool body so every failure becomes a structured JSON payload."""

    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except BridgeError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001 - boundary safety net, must not hang
            return BridgeError(
                "unexpected_error", f"{exc.__class__.__name__}: {exc}"
            ).to_dict()

    return wrapper


def build_server(config: BridgeConfig | None = None) -> MCPServer:
    config = config or BridgeConfig.from_env()
    client = VoiceStudioClient(config)
    server: MCPServer = MCPServer(
        name=SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS
    )

    @server.tool(
        name="generate_audiobook",
        description=(
            "Render a document (.txt/.md/.epub/.pdf) into an audiobook via "
            "VoiceStudio. Cold-starts VoiceStudio if needed. Returns the absolute "
            "path to a valid .m4b/.mp3 plus an ffprobe summary."
        ),
    )
    def generate_audiobook(
        import_path: str, voice: str = "", language: str = "", format: str = "m4b"
    ) -> dict[str, Any]:
        return _guard(client.generate_audiobook)(
            import_path=import_path, voice=voice, language=language, fmt=format
        )

    @server.tool(
        name="translate_text",
        description=(
            "Translate text via VoiceStudio. Cold-starts VoiceStudio if needed. "
            "Prefers the offline Argos engine (installing the language pack when "
            "missing) and falls back to any ready provider. Returns the translated "
            "text and the provider used."
        ),
    )
    def translate_text(text: str, src: str, tgt: str) -> dict[str, Any]:
        return _guard(client.translate_text)(text=text, source=src, target=tgt)

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
