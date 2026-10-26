"""Expose connected agents to the tool layer (no Qt).

``AgentManager`` runs the bridge on its own asyncio loop in a background thread --
like ``core.mcp_manager.McpManager`` -- and presents the manager interface the
tool layer already expects: ``all_tools()`` and ``call(server, tool, args,
timeout) -> (text, is_error)``. Connected hosts appear as ``host_<name>.<tool>``
tools (see ``core.agent.hosttools``), so the existing ``Catalog``, ``ToolRouter``
and permission flow drive them with no changes.

``CompositeManager`` merges this with the MCP manager: host tools route to the
bridge, everything else to MCP. The main window builds the catalog and the router
from a CompositeManager (Phase 4 wiring).
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading

from core.agent import hosttools
from core.agent.bridge import Bridge, TOOL_TIMEOUT
from core.mcp_manager import ToolInfo


class AgentManager:
    def __init__(self, ca_key_pem: bytes, ca_cert_pem: bytes,
                 server_cert_path: str, server_key_path: str, ca_cert_path: str, db_path: str):
        self._bridge = Bridge(ca_key_pem, ca_cert_pem, server_cert_path, server_key_path,
                              ca_cert_path, db_path)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

    @classmethod
    def from_identity(cls, identity: dict, db_path: str) -> "AgentManager":
        """Build from core.agent.identity.ensure_identity() output."""
        return cls(identity["ca_key_pem"], identity["ca_cert_pem"],
                   identity["server_cert_path"], identity["server_key_path"],
                   identity["ca_cert_path"], db_path)

    # --- loop thread --------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run():
                    asyncio.set_event_loop(loop)
                    ready.set()
                    loop.run_forever()

                self._thread = threading.Thread(target=run, name="agent-bridge-loop", daemon=True)
                self._thread.start()
                ready.wait()
                self._loop = loop
            return self._loop

    def _run(self, coro, timeout: float | None):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    # --- lifecycle ----------------------------------------------------------

    def start(self, host: str = "127.0.0.1", port: int = 0):
        """Start the listener. No-op if already running. Raises OSError on a bad bind."""
        if self._running:
            return
        self._run(self._bridge.start(host, port), 15)
        self._running = True

    def is_running(self) -> bool:
        return self._running

    def port(self) -> int:
        return self._bridge.port()

    def stop(self):
        """Stop the listener but keep the loop thread, so it can be started again."""
        if self._loop is None or not self._running:
            return
        self._run(self._bridge.stop(), 15)
        self._running = False

    def disconnect(self, name: str) -> bool:
        """Drop a connected agent's socket. True if it was connected."""
        if self._loop is None or not self._running:
            return False
        try:
            return self._run(self._bridge.disconnect(name), 5)
        except Exception:
            return False

    def shutdown(self):
        if self._loop is None:
            return
        try:
            if self._running:
                self._run(self._bridge.stop(), 15)
                self._running = False
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # --- manager interface --------------------------------------------------

    def _snapshot(self) -> list[dict]:
        if self._loop is None:
            return []
        try:
            return self._run(self._bridge.snapshot(), 5)
        except Exception:
            return []

    def connected_names(self) -> list[str]:
        return sorted(a["name"] for a in self._snapshot())

    def connected(self) -> list[dict]:
        """Full view of the connected agents: {name, os, tools, roots}."""
        return self._snapshot()

    def all_tools(self) -> list[ToolInfo]:
        infos: list[ToolInfo] = []
        for agent in self._snapshot():
            infos += hosttools.tool_infos(agent["name"], agent["os"], agent["tools"])
        return infos

    def handles(self, server: str) -> bool:
        """Whether this manager owns a tool server (a connected host)."""
        name = hosttools.host_name_from_server(server)
        return name is not None and name in {a["name"] for a in self._snapshot()}

    def call(self, server: str, tool: str, arguments: dict | None,
             timeout: float = TOOL_TIMEOUT) -> tuple[str, bool]:
        name = hosttools.host_name_from_server(server)
        if name is None:
            return (f"not a host server: {server}", True)
        try:
            res = self._run(self._bridge.call(name, tool, arguments or {}, timeout), timeout + 5)
        except (TimeoutError, asyncio.TimeoutError):
            return (f"host {name} did not answer within {timeout:g} s", True)
        except ConnectionError as e:
            return (str(e), True)
        except Exception as e:  # never raise into the router; errors are text
            return (f"call to host {name} failed: {e.__class__.__name__}: {e}", True)
        return (res["output"], res["is_error"])


class CompositeManager:
    """Routes host_* tool servers to the agent manager and everything else to MCP.

    Exposes the same ``all_tools`` / ``call`` interface as each underlying manager,
    so ``Catalog`` and ``ToolRouter`` treat local and remote tools uniformly.
    """

    def __init__(self, base, agent: AgentManager):
        self.base = base
        self.agent = agent

    def all_tools(self) -> list[ToolInfo]:
        return list(self.base.all_tools()) + list(self.agent.all_tools())

    def call(self, server: str, tool: str, arguments: dict | None,
             timeout: float = TOOL_TIMEOUT) -> tuple[str, bool]:
        if self.agent.handles(server):
            return self.agent.call(server, tool, arguments, timeout)
        return self.base.call(server, tool, arguments, timeout)


def open_registry(db_path: str):
    """A Registry on the given database (convenience for the UI / enrollment)."""
    from core.agent.registry import Registry
    return Registry(sqlite3.connect(db_path))
