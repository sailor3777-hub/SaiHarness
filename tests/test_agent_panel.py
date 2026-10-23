"""Offscreen test for ui.agent_panel.AgentPanel: create an agent (writes the
installer, records a pending agent), list it, start/stop the listener, revoke.
Runs on the offscreen Qt platform; no real network beyond a loopback bind."""
import sqlite3
import sys
import tempfile
from pathlib import Path

from PySide6.QtWidgets import QApplication

from core import db

db.DB_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp()) / "panel.db"

from core.agent import identity
from core.agent.manager import AgentManager
from core.agent.registry import Registry, RegistryInputError

from ui.agent_panel import AgentPanel

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def raises(exc, fn):
    try:
        fn()
        return False
    except exc:
        return True


app = QApplication.instance() or QApplication([])
tmp = Path(tempfile.mkdtemp(prefix="sai_panel_"))
info = identity.ensure_identity(tmp / "agent")
agent_mgr = AgentManager.from_identity(info, str(db.DB_PATH))
panel = AgentPanel(agent_mgr, info)
panel.address.setText("10.0.0.5:8765")

out = str(tmp / "installer_alpha.ps1")
token = panel.create_agent("alpha", out)
check("create_agent returns a token", bool(token))
script = Path(out).read_text(encoding="utf-8")
check("installer file was written with token, address and CA",
      token in script and "10.0.0.5:8765" in script and "BEGIN CERTIFICATE" in script)

with sqlite3.connect(db.DB_PATH) as conn:
    reg = Registry(conn)
    row = reg.get_by_name("alpha")
    check("a pending agent was recorded", row is not None and row["status"] == "pending")
    agent_id = row["id"]

panel.refresh()
check("the agent shows up in the list", panel.tree.topLevelItemCount() == 1
      and panel.tree.topLevelItem(0).text(0) == "alpha"
      and panel.tree.topLevelItem(0).text(1) == "pending")

check("re-creating a still-pending agent reissues (no error)", bool(panel.create_agent("alpha", out)))

# the installer address gets the port appended when the address has none
panel.address.setText("10.0.0.9")
panel.port.setText("9001")
out_beta = str(tmp / "installer_beta.ps1")
panel.create_agent("beta", out_beta)
check("installer address gets the port appended", "10.0.0.9:9001" in Path(out_beta).read_text(encoding="utf-8"))

# listener lifecycle (bind to an ephemeral port to avoid conflicts)
panel.port.setText("0")
panel.start_listener()
check("listener starts", agent_mgr.is_running())
check("no host tools while nothing is connected", agent_mgr.all_tools() == [])
panel.stop_listener()
check("listener stops", not agent_mgr.is_running())

check("revoke blocks the agent", panel.revoke(agent_id))
with sqlite3.connect(db.DB_PATH) as conn:
    check("registry shows the agent revoked", Registry(conn).get(agent_id)["status"] == "revoked")
check("re-creating a revoked name is refused", raises(RegistryInputError, lambda: panel.create_agent("alpha", out)))

agent_mgr.shutdown()
print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
