"""Offline test for Phase 3: a connected host appears in the catalog as
host_<name>.<tool>, ToolRouter dispatches those calls through the bridge, the
read-only ones run automatically while shell_run asks via the confirm callback,
and CompositeManager merges host tools with a base (MCP-style) manager. Loopback
only; the agent client runs on its own thread, the bridge on the manager's own
thread, the router on the main thread -- the same split as in the app."""
import asyncio
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from core import markers
from core.agent import ca, enroll, hosttools, wrapper
from core.agent.manager import AgentManager, CompositeManager
from core.agent.registry import Registry
from core.mcp_manager import ToolInfo
from core.tools import Catalog, ToolRouter, default_policy

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def wait_until(fn, timeout=8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return bool(fn())


class AgentClientThread:
    """Runs wrapper.run_agent on its own loop/thread (stands in for the remote host)."""

    def __init__(self, args):
        self.args = args
        self.loop = asyncio.new_event_loop()
        self.task = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(wrapper.run_agent(*self.args))
        try:
            self.loop.run_until_complete(self.task)
        except (asyncio.CancelledError, Exception):
            pass

    def stop(self):
        if self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout=5)


class FakeMcp:
    """A minimal stand-in for McpManager: one local tool, records its calls."""

    def __init__(self):
        self.reply = ("mcp-calc-result", False)
        self.calls = []

    def all_tools(self):
        return [ToolInfo("tb", "calc", "Calculates an expression.",
                         {"type": "object", "properties": {"expression": {"type": "string"}},
                          "required": ["expression"]}, read_only=True)]

    def call(self, server, tool, arguments, timeout=60.0):
        self.calls.append((server, tool, arguments))
        return self.reply


def main(db_path: str):
    tmp = Path(tempfile.mkdtemp(prefix="sai_tools_"))
    client = agent_mgr = None
    try:
        ca_key, ca_cert = ca.generate_ca()
        srv_key, srv_cert = ca.generate_server_cert(ca_key, ca_cert, "127.0.0.1")
        ca_cert_path = tmp / "ca_cert.pem"; ca_cert_path.write_bytes(ca_cert)
        srv_cert_path = tmp / "server_cert.pem"; srv_cert_path.write_bytes(srv_cert)
        srv_key_path = tmp / "server_key.pem"; srv_key_path.write_bytes(srv_key)

        work = tmp / "work"; work.mkdir()
        (work / "hello.txt").write_text("marker-77", encoding="utf-8")

        # create + enroll an agent directly (the channel enrollment is covered elsewhere)
        reg = Registry(sqlite3.connect(db_path))
        _, token = enroll.create_agent(reg, "alpha")
        host_key, csr = ca.generate_csr("alpha")
        _, client_cert = enroll.enroll(reg, ca_key, ca_cert, token, csr)
        reg.conn.close()
        ckey = tmp / "client_key.pem"; ckey.write_bytes(host_key)
        ccert = tmp / "client_cert.pem"; ccert.write_bytes(client_cert)

        agent_mgr = AgentManager(ca_key, ca_cert, str(srv_cert_path), str(srv_key_path),
                                 str(ca_cert_path), db_path)
        agent_mgr.start("127.0.0.1", 0)
        url = f"wss://127.0.0.1:{agent_mgr.port()}"

        host = wrapper.ToolHost([str(work)])
        client = AgentClientThread((url, str(ca_cert_path), str(ccert), str(ckey), "alpha", host))
        client.start()

        check("host tools appear once the agent connects",
              wait_until(lambda: any(t.id == "host_alpha.shell_run" for t in agent_mgr.all_tools()), 8))

        tools = agent_mgr.all_tools()
        by_id = {t.id: t for t in tools}
        check("fs_read and shell_run are listed",
              {"host_alpha.fs_read", "host_alpha.shell_run"} <= set(by_id))
        fsread = by_id["host_alpha.fs_read"]
        shell = by_id["host_alpha.shell_run"]
        check("read-only host tool is auto, shell_run asks",
              default_policy(fsread) == "auto" and default_policy(shell) == "ask")
        check("the tool is described as remote", "remote host alpha" in fsread.description)

        cat = Catalog(tools)
        check("catalog resolves the host tool by id", cat.get("host_alpha.fs_read") is fsread)

        # --- context block (Phase 5) ---
        check("empty prompt_block is blank", hosttools.prompt_block([]) == "")
        conn_hosts = agent_mgr.connected()
        check("connected() reports the host and its OS",
              bool(conn_hosts) and conn_hosts[0]["name"] == "alpha" and conn_hosts[0]["os"])
        block = hosttools.prompt_block([(h["name"], h["os"]) for h in conn_hosts])
        check("prompt_block names the host and explains remote tools",
              "alpha" in block and "host_<name>" in block and "Remote hosts" in block)

        asked: list[str] = []

        def confirm_yes(tool_id, args):
            asked.append(tool_id); return True

        def confirm_no(tool_id, args):
            asked.append(tool_id); return False

        router = ToolRouter(agent_mgr, cat, policy_for=default_policy, confirm=confirm_yes)

        out = router.handle("call_tool", {"tool": "host_alpha.fs_read",
                                          "arguments": {"path": str(work / "hello.txt")}})
        check("a read-only host tool runs and returns the content", "marker-77" in out and not markers.is_error(out))
        check("a read-only host tool did not ask", asked == [])

        out2 = router.handle("call_tool", {"tool": "host_alpha.shell_run",
                                           "arguments": {"command": "echo router-ok"}})
        check("shell_run asked and then ran on the host",
              "host_alpha.shell_run" in asked and "router-ok" in out2 and not markers.is_error(out2))

        asked.clear()
        router_no = ToolRouter(agent_mgr, cat, policy_for=default_policy, confirm=confirm_no)
        out3 = router_no.handle("call_tool", {"tool": "host_alpha.shell_run",
                                              "arguments": {"command": "echo should-not-run"}})
        check("a declined shell_run is not executed",
              "declined" in out3.lower() and "should-not-run" not in out3)

        out4 = router.handle("call_tool", {"tool": "host_alpha.fs_read",
                                           "arguments": {"path": str(work / "absent.txt")}})
        check("a missing file comes back as an error", markers.is_error(out4))

        # path outside the host's roots is refused by the host, surfaced as an error
        out5 = router.handle("call_tool", {"tool": "host_alpha.fs_read",
                                           "arguments": {"path": str(ca_cert_path)}})
        check("a path outside the host roots is an error", markers.is_error(out5))

        # --- composite manager (host tools + an MCP-style base) ---
        fake = FakeMcp()
        comp = CompositeManager(fake, agent_mgr)
        comp_ids = {t.id for t in comp.all_tools()}
        check("composite merges base and host tools",
              "tb.calc" in comp_ids and "host_alpha.fs_read" in comp_ids)
        cat2 = Catalog(comp.all_tools())
        rc = ToolRouter(comp, cat2, policy_for=default_policy, confirm=confirm_yes)
        oc1 = rc.handle("call_tool", {"tool": "tb.calc", "arguments": {"expression": "2+2"}})
        check("composite routes a base tool to MCP", "mcp-calc-result" in oc1 and fake.calls)
        oc2 = rc.handle("call_tool", {"tool": "host_alpha.fs_read",
                                      "arguments": {"path": str(work / "hello.txt")}})
        check("composite routes a host tool to the bridge", "marker-77" in oc2)
    finally:
        if client is not None:
            client.stop()
        if agent_mgr is not None:
            agent_mgr.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


main(sys.argv[1] if len(sys.argv) > 1 else ":memory:")
print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
