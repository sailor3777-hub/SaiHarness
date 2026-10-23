"""Offline tests for the host-delivery pieces (Approach A):
- installer.build_package: the slim host zip (no core/docs.py / pypdf),
- installer.render: embeds the package base64 and fills all placeholders,
- core.files imports without pulling in core.docs (slim host dependency),
- core.agent.agent_host: enroll-then-connect and reconnect-with-cert over loopback.
No external network."""
import asyncio
import base64
import io
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from core.agent import agent_host, ca, enroll, identity, installer
from core.agent.manager import AgentManager
from core.agent.registry import Registry

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


class SuperviseThread:
    def __init__(self, install_dir, token):
        self.args = (str(install_dir), token)
        self.loop = asyncio.new_event_loop()
        self.task = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(
            agent_host.supervise(self.args[0], token=self.args[1], backoff=0.3))
        try:
            self.loop.run_until_complete(self.task)
        except (asyncio.CancelledError, Exception):
            pass

    def stop(self):
        if self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout=5)


# --- package contents ------------------------------------------------------
pkg = installer.build_package()
names = set(zipfile.ZipFile(io.BytesIO(pkg)).namelist())
check("package has the wrapper and agent_host", {"core/agent/wrapper.py", "core/agent/agent_host.py"} <= names)
check("package has slim core modules", {"core/files.py", "core/shell.py", "core/markers.py"} <= names)
check("package excludes core/docs.py (pypdf/python-docx)", "core/docs.py" not in names)
files_src = zipfile.ZipFile(io.BytesIO(pkg)).read("core/files.py").decode("utf-8")
check("shipped files.py has no top-level docs import", "\nfrom core import docs" not in files_src)

# --- render embeds the package and fills placeholders ----------------------
ca_key, ca_cert = ca.generate_ca()
script = installer.render("192.168.1.10:8765", ca_cert, "TOKEN-xyz", "alpha", package=pkg)
check("render carries address/token/name/CA",
      "192.168.1.10:8765" in script and "TOKEN-xyz" in script and "alpha" in script and "BEGIN CERTIFICATE" in script)
check("render leaves no placeholders", "{{" not in script and "}}" not in script)
b64 = next((ln.split("'")[1] for ln in script.splitlines() if ln.startswith("$PackageB64")), "")
check("embedded package decodes to the same zip", base64.b64decode(b64) == pkg)

# --- core.files does not import core.docs at import time -------------------
proc = subprocess.run([sys.executable, "-c", "import core.files, sys; print('core.docs' in sys.modules)"],
                      capture_output=True, text=True)
check("importing core.files does not import core.docs", proc.stdout.strip() == "False", proc.stdout.strip() + proc.stderr)

# --- agent_host over loopback ---------------------------------------------
db_path = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
tmp = Path(tempfile.mkdtemp(prefix="sai_delivery_"))
agent_mgr = client1 = client2 = None
try:
    info = identity.ensure_identity(tmp / "server")
    reg = Registry(sqlite3.connect(db_path))
    _, token = enroll.create_agent(reg, "alpha")
    reg.conn.close()

    agent_mgr = AgentManager.from_identity(info, db_path)
    agent_mgr.start("127.0.0.1", 0)
    port = agent_mgr.port()

    work = tmp / "work"; work.mkdir()
    (work / "hello.txt").write_text("delivered-ok", encoding="utf-8")

    install = tmp / "install"; install.mkdir()
    (install / "ca_cert.pem").write_bytes(info["ca_cert_pem"])
    (install / "agent.json").write_text(
        f'{{"name": "alpha", "address": "127.0.0.1:{port}", "roots": ["{work.as_posix()}"]}}', encoding="utf-8")
    (install / "enroll_token.txt").write_text(token, encoding="utf-8")

    # phase 1: no certificate yet -> the supervisor enrolls with the token, then connects
    client1 = SuperviseThread(install, None)
    client1.start()
    check("agent_host enrolls and connects", wait_until(lambda: "alpha" in agent_mgr.connected_names(), 8))
    res = asyncio.run_coroutine_threadsafe(
        agent_mgr._bridge.call("alpha", "fs_read", {"path": str(work / "hello.txt")}, 30),
        agent_mgr._loop).result(35)
    check("a tool call reaches the delivered agent", "delivered-ok" in res["output"] and not res["is_error"])
    check("certificate files were created on the host", (install / "client_cert.pem").exists() and (install / "client_key.pem").exists())
    check("the one-time token file was removed after enrollment", not (install / "enroll_token.txt").exists())

    client1.stop()
    check("agent disconnects when the supervisor stops",
          wait_until(lambda: "alpha" not in agent_mgr.connected_names(), 5))

    # phase 2: certificate exists, no token -> reconnects without re-enrolling
    client2 = SuperviseThread(install, None)
    client2.start()
    check("agent_host reconnects using the saved certificate",
          wait_until(lambda: "alpha" in agent_mgr.connected_names(), 8))
    client2.stop()
finally:
    if client1 is not None:
        client1.stop()
    if client2 is not None:
        client2.stop()
    if agent_mgr is not None:
        agent_mgr.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
