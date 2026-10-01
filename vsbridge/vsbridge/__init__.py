"""vsbridge package: a thin stdio MCP proxy over VoiceStudio's own MCP endpoint.

On-demand: each ``tools/call`` runs ``vs-ensure-up.sh`` first, then forwards the
call to VoiceStudio's Streamable-HTTP MCP at ``/mcp/``. ``tools/list`` is
forwarded when VoiceStudio is already up and falls back to a live-derived
catalog when it is down (so listing never cold-starts the app).
"""

__version__ = "0.1.0"
