"""shell_run / shell_output on the real MCP server: working folder, output, timeout, truncation with offset, writing files."""
import sys, time, pathlib, tempfile
from core import db, shell
from core.mcp_manager import load_config
from model_view import McpManager  # errors with the prefix, as the model sees them
from core.tools import TOOL_TIMEOUTS

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "shell.db"
root = tmp / "proj"; (root / "sub").mkdir(parents=True)
outside = tmp / "outside"; outside.mkdir()
db.add_root(str(root))
m = McpManager(); m.connect_all(load_config(tmp / "mcp.json", db.DB_PATH))
def run(command, cwd=None):
    args = {"command": command}
    if cwd is not None: args["cwd"] = str(cwd)
    return m.call("toolbox", "shell_run", args, 90)[0]
def out(run_id, offset=0): return m.call("toolbox", "shell_output", {"run_id": run_id, "offset": offset})[0]

# ---- permissions ----
tools = {t.name: t for t in m.all_tools()}
check("shell_run is not read-only (it will ask), shell_output is read-only",
      not tools["shell_run"].read_only and tools["shell_output"].read_only)
check("shell_run has a longer call timeout", TOOL_TIMEOUTS["toolbox.shell_run"] > shell.TIMEOUT)

# ---- a normal run ----
t = run("Write-Output 'Grüße ✓'; (Get-Location).Path")
check("output in UTF-8, code 0", "exit code 0" in t and "Grüße ✓" in t, t)
check("by default cwd = the first allowed folder", str(root.resolve()) in t, t)
t = run("(Get-Location).Path", root / "sub"); check("cwd inside the root", str((root / "sub").resolve()) in t, t)
t = run("exit 3"); check("the exit code is passed on", "exit code 3" in t, t)
t = run("Write-Error 'boom'; 'after'"); check("stderr in the output", "boom" in t and "after" in t, t)
check("errors come as text, not CLIXML", "CLIXML" not in t and "<Objs" not in t, t)
t = run("   "); check("an empty command", t.startswith("ERROR") and "empty" in t, t)
t = run("'quotes \"double\" and ''single'''"); check("quotes do not break the command", 'double' in t and 'single' in t, t)

# ---- environment: the server gets the full environment, not mcp's trimmed one (otherwise modules like Hyper-V do not work) ----
t = run("'win=' + $env:windir; 'pd=' + $env:ProgramData; 'psm=' + [bool]$env:PSModulePath; 'pc=' + $env:COMPUTERNAME")
check("shell_run has windir, ProgramData, PSModulePath, COMPUTERNAME",
      "win=C:" in t.replace("c:", "C:") and "pd=C:" in t.replace("c:", "C:") and "psm=True" in t and "pc=" in t and "pc=\n" not in t, t)

t = run("'Test2' | powershell -NoProfile -Command '$s = Read-Host; $s.Length'")
check("a pipe into an external program without a BOM", "\n5\n" in t, t)

# ---- any working folder, not only inside the roots ----
t = run("'x' | Set-Content here.txt; (Get-Location).Path", outside)
check("cwd outside the roots is allowed (access to the whole system)", (outside / "here.txt").exists() and str(outside.resolve()) in t, t)
t = run("(Get-Location).Path", root / ".." / "outside"); check("cwd through .. works too", str(outside.resolve()) in t, t)
t = run("Copy-Item '%s' '%s'" % (outside / "here.txt", root / "copied.txt"))
check("copying between folders by absolute paths", (root / "copied.txt").exists(), t)
t = run("(Get-Location).Path", "sub"); check("a relative cwd starts from the default folder", str((root / "sub").resolve()) in t, t)
t = run("'x'", outside / "no_such"); check("a missing cwd: an error", t.startswith("ERROR") and "does not exist" in t, t)
f = root / "file.txt"; f.write_text("a", encoding="utf-8"); t = run("'x'", f); check("a cwd that is a file instead of a folder", t.startswith("ERROR"), t)

# ---- without allowed folders: the service folder ----
db.DB_PATH = tmp / "empty.db"
cfg0 = load_config(tmp / "mcp0.json", db.DB_PATH); cfg0["mcpServers"]["toolbox"]["env"]["SAI_SHELL_DIR"] = str(tmp / "scratch")
m0 = McpManager(); m0.connect_all(cfg0)
t = m0.call("toolbox", "shell_run", {"command": "(Get-Location).Path; hostname"}, 90)[0]
check("without roots the command runs in the service folder", "exit code 0" in t and str((tmp / "scratch").resolve()) in t, t)
m0.shutdown()

# ---- writing files (what it was made for) ----
t = run("Set-Content -Path notes.txt -Value 'created by the model' -Encoding UTF8; Get-Content notes.txt -Encoding UTF8")
check("shell_run creates a file in the root", (root / "notes.txt").exists() and "created by the model" in (root / "notes.txt").read_text(encoding="utf-8-sig") and "created by the model" in t, t)
r = m.call("toolbox", "fs_read", {"path": str(root / "notes.txt")})[0]
check("the created file is read through fs_read", "created by the model" in r, r)
t = run("New-Item -ItemType Directory -Force -Path newdir | Out-Null; 'print(1)' | Set-Content newdir\\a.py; Get-ChildItem newdir -Name")
check("a folder with a file is created", (root / "newdir" / "a.py").exists() and "a.py" in t, t)

# ---- long output: truncation and offset without running again ----
t = run("1..3000 | ForEach-Object { 'line{0:D5}' -f $_ }")
check("the output is truncated with a shell_output hint", "[truncated:" in t and "shell_output" in t and len(t) <= 10_800, len(t))
rid = int(t.split("run_id=")[1].split("]")[0].split(";")[0]); nxt = int(t.rsplit("offset=", 1)[1].rstrip("]"))
t2 = out(rid, nxt); check("shell_output continues from offset", "line00001" not in t2 and "line010" in t2, t2[:60])
parts, off, guard = [], 0, 0
while guard < 20:
    guard += 1
    chunk = out(rid, off)
    body = chunk.split("\n[")[0] if "\n[" in chunk and chunk.rstrip().endswith("]") else chunk
    parts.append(body)
    if "to continue:" not in chunk: break
    off = int(chunk.rsplit("offset=", 1)[1].rstrip("]"))
joined = "\n".join(parts)
check("in chunks the whole output is read without loss", "line00001" in joined and "line03000" in joined, joined[-80:])
check("a missing run_id", out(9999).startswith("ERROR"))
check("offset past the end", out(rid, 10_000_000).startswith("ERROR"))
t = run("'short'"); check("short output without truncation", "[truncated" not in t and "short" in t, t)
t = run("$null"); check("empty output is marked", "(no output)" in t, t)

# ---- timeout and killing the process tree (directly in core.shell, so as not to wait 60 s) ----
marker = tmp / "alive.txt"
t0 = time.time()
code, text, timed = shell.run(f"'start'; Start-Sleep 2; 'x' | Set-Content '{marker}'; 'end'", str(root), timeout=0.8)
check("timeout: killed, code None, partial output", timed and code is None and time.time() - t0 < 6, (code, timed, time.time() - t0))
time.sleep(2.5); check("after the timeout the process did not write the file", not marker.exists())
code, text, timed = shell.run("exit 7", str(root)); check("core.shell.run: exit code", (code, timed) == (7, False), (code, text))

m.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
