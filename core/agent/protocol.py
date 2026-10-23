"""Channel frame format over the WebSocket link (no Qt, no I/O). [Phase 2]

Pure (de)serialization of the JSON frames that multiplex one agent connection:
hello / capabilities / tool_call / tool_result / ping-pong, each with a
correlation id. Tool errors follow the repo convention (``core.markers``) so a
file whose content starts with "ERROR" is not mistaken for a tool error.

Not implemented yet — see AGENT_MODULE_SPEC.md section 6 and 11 (Phase 2).
"""
from __future__ import annotations
