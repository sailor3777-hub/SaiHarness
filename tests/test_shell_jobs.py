"""Background commands: core.shell.Jobs / render, shell_run / shell_output / shell_stop on the MCP server, and the same tools on a host (ToolHost)."""
import sys, time, pathlib, tempfile
from core import db, shell
from core.agent.wrapper import ToolHost
from core.mcp_manager import load_config
from model_view import McpManager

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp()); root = tmp / "proj"; root.mkdir()

# ---- core: a job outlives the wait, output is followed by offset ----
jobs = shell.Jobs()
job, err = jobs.start("'one'; Start-Sleep 3; 'two'", str(root))
check("a job starts", job is not None and not err, err)
check("wait times out while the command is running", job.wait(0.5) is False and job.state == "running")
# PowerShell may take more than 0.5 s to print anything on a slow machine: wait for the first line
t0 = time.time()
while "one" not in job.snapshot()["text"] and time.time() - t0 < 2.5:
    time.sleep(0.05)
text, is_err = shell.render(job, 0)
check("render of a running job says so, with run_id and a way to follow it",
      "still running" in text and f"run_id={job.id}" in text and "shell_stop" in text and "one" in text and not is_err, text)
end = int(text.split("offset=")[1].split(";")[0].split("]")[0])
check("polling with the offset gives no repeat of earlier output", "one" not in shell.render(job, end)[0].split("]\n", 1)[1].split("\n[")[0])
check("the command finishes", job.wait(10) and job.state == "done" and job.code == 0)
t2, _ = shell.render(job, end)
check("the new part appears after finishing, with the exit code", "two" in t2 and "exit code 0" in t2 and "one" not in t2.split("]\n", 1)[1], t2)
check("offset equal to the end of a finished job is not an error", shell.render(job, 10_000)[1] is True and shell.render(job, len(job.snapshot()["text"]))[1] is False)
check("jobs are found by run_id", jobs.get(job.id) is job and jobs.get(999) is None and jobs.get("x") is None)

# ---- stop kills the process tree ----
marker = tmp / "alive.txt"
job2, _ = jobs.start(f"Start-Sleep 3; 'x' | Set-Content '{marker}'", str(root))
time.sleep(0.5)
check("stop kills a running job", job2.stop() is True and job2.state == "stopped")
check("stopping again says it is not running", job2.stop() is False)
time.sleep(3.5); check("the stopped command did not write its file", not marker.exists())
check("render shows 'stopped'", "stopped by shell_stop" in shell.render(job2, 0)[0])

# ---- lifetime cap ----
short = shell.Jobs(lifetime=1.0)
job3, _ = short.start("'before'; Start-Sleep 30", str(root))
check("a job over its lifetime is killed", job3.wait(8) and job3.state == "timeout", job3.state)
check("render shows TIMEOUT and the partial output", "TIMEOUT" in shell.render(job3, 0)[0] and "before" in shell.render(job3, 0)[0])

# ---- pruning: finished jobs beyond KEEP are dropped, running ones are kept ----
small = shell.Jobs(keep=2)
first, _ = small.start("Start-Sleep 5", str(root))
for _ in range(4): small.start("'x'", str(root))[0].wait(10)
check("a running job is not pruned", small.get(first.id) is first)
check("old finished jobs are pruned", small.get(2) is None)
first.stop()

# ---- the synchronous run keeps its contract ----
code, out, timed = shell.run("exit 7", str(root)); check("run: exit code", (code, timed) == (7, False), (code, out))
code, out, timed = shell.run("'a'; Start-Sleep 5", str(root), timeout=0.8)
check("run: timeout kills, code None, partial output", timed and code is None and "a" in out, (code, out, timed))

# ---- MCP server (SAI_SHELL_WAIT=1: it leaves the command in the background after 1 s) ----
db.DB_PATH = tmp / "jobs.db"; db.add_root(str(root))
cfg = load_config(tmp / "mcp.json", db.DB_PATH); cfg["mcpServers"]["toolbox"]["env"]["SAI_SHELL_WAIT"] = "1"
m = McpManager(); m.connect_all(cfg)
def call(tool, **a): return m.call("toolbox", tool, a, 90)
tools = {t.name: t for t in m.all_tools()}
check("shell_stop asks (not read-only), shell_output does not", not tools["shell_stop"].read_only and tools["shell_output"].read_only)
t = call("shell_run", command="'first'; Start-Sleep 4; 'last'")[0]
check("shell_run returns after the wait with a run_id and 'still running'", "still running" in t and "run_id=1" in t and "first" in t, t)
nxt = int(t.rsplit("offset=", 1)[1].split(";")[0].rstrip("]"))
t = call("shell_output", run_id=1, offset=nxt)[0]
check("shell_output of a running command says so and shows no new output", "still running" in t and "no new output" in t, t)
time.sleep(4)
t = call("shell_output", run_id=1, offset=nxt)[0]
check("shell_output after the end: the rest and the exit code", "last" in t and "exit code 0" in t, t)
t = call("shell_run", command="Start-Sleep 20")[0]
check("a second command gets run_id 2", "run_id=2" in t, t)
t, err = call("shell_stop", run_id=2)
check("shell_stop stops it", "was stopped" in t and not err, t)
t, err = call("shell_stop", run_id=2); check("stopping a stopped command is not an error", "not running" in t and not err, t)
t, err = call("shell_stop", run_id=77); check("an unknown run_id is an error", err and "no command" in t, t)
t, err = call("shell_output", run_id=77); check("shell_output: unknown run_id is an error", err, t)
m.shutdown()

# ---- the same tools on a host ----
host = ToolHost([str(root)])
check("the host announces shell_output and shell_stop", {"shell_run", "shell_output", "shell_stop"} <= set(host.tool_names()))
shell.WAIT = 1.0
out, err = host.run("shell_run", {"command": "'h1'; Start-Sleep 4; 'h2'"})
check("host shell_run: still running with a run_id", not err and "still running" in out and "run_id=1" in out and "h1" in out, out)
time.sleep(4)
out, err = host.run("shell_output", {"run_id": 1})
check("host shell_output: the whole output and the exit code", not err and "h2" in out and "exit code 0" in out, out)
out, err = host.run("shell_run", {"command": "Start-Sleep 20"})
out, err = host.run("shell_stop", {"run_id": 2}); check("host shell_stop", not err and "was stopped" in out, out)
out, err = host.run("shell_output", {"run_id": 99}); check("host: unknown run_id is an error", err, out)
out, err = host.run("shell_run", {"command": "'quick'"}); check("host: a quick command returns at once with its exit code", not err and "exit code 0" in out and "quick" in out, out)
out, err = host.run("shell_run", {"command": "'outer'", "cwd": str(tmp)}); check("host: cwd outside the roots is allowed (no folder restriction on hosts)", not err and "outer" in out and str(tmp) in out, out)

print("ALL OK" if ok_all else "SOME FAILED")
