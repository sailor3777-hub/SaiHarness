"""The thin executor that runs on the host (no Qt). [Phase 2]

No model, no inference connection, exactly one peer (the main harness). It
reuses the existing local-tool logic (``core/shell.py`` for shell_run,
``core/files.py`` for fs_read/list/search) so remote behaviour matches local
behaviour exactly, dials out to the main harness over CA-pinned mTLS, and
executes the tool calls it receives.

Not implemented yet — see AGENT_MODULE_SPEC.md section 7 and 11 (Phase 2).
"""
from __future__ import annotations
