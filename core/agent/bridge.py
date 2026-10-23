"""In-process agent bridge: TLS listener, live registry, tool dispatch. [Phase 2-3]

Unlike the stdio MCP servers in ``core/mcp_manager.py``, the bridge holds live
network state (one socket per connected host) and must drive the GUI permission
dialogs, so it lives in-process in the main app. It owns the agent-facing TLS
listener (mTLS, CA-pinned), tracks connected agents, and contributes dynamic
tools to the catalog as ``host_<name>.<tool>`` which ``ToolRouter`` dispatches
over the matching socket.

Not implemented yet — see AGENT_MODULE_SPEC.md section 5 and 11 (Phase 2-3).
"""
from __future__ import annotations
