import os, subprocess, sys, tempfile, pathlib, time
from core import diag
ROOT = str(pathlib.Path(__file__).resolve().parents[1])  # this checkout, not a hardcoded copy

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
# a dead pid: run a short process and wait for it to end
p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait(); DEAD = p.pid
ALIVE = os.getpid()
check("pid_alive: our own process is alive", diag.pid_alive(ALIVE))
check("pid_alive: a finished process is dead", not diag.pid_alive(DEAD))
check("pid_alive: a nonexistent number", not diag.pid_alive(4_000_000))

text = "\n".join([
    "=== start 2026-10-13T18:05:17",                       # the old format without pid: ignored
    "=== start 2026-10-14T10:00:00 pid=111", "=== exit 2026-10-14T10:05:00 pid=111",
    "=== start 2026-10-14T11:00:00 pid=222",              # no exit
    "=== start 2026-10-14T11:01:00 pid=333",              # a parallel run
    "=== exit 2026-10-14T11:30:00 pid=333",
    "=== start 2026-10-14T12:00:00 pid=444", "=== lost 2026-10-14T12:30:00 pid=444  (...)",
    "[qt] QFont::setPointSize: Point size <= 0",
])
check("unclean_runs: only starts without a close, parallel and lost are taken into account", diag.unclean_runs(text) == [(222, "2026-10-14T11:00:00")], diag.unclean_runs(text))
check("unclean_runs: empty", diag.unclean_runs("") == [])

CODE = ("import sys; from pathlib import Path; from core import diag; diag.install(Path(sys.argv[1])); "
        "import faulthandler; (faulthandler._sigsegv() if len(sys.argv) > 2 and sys.argv[2] == 'crash' else None)")
def run(log, *extra):
    return subprocess.run([sys.executable, "-c", CODE, str(log), *extra], cwd=ROOT, capture_output=True,
                          env={**os.environ, "PYTHONPATH": ROOT, "QT_QPA_PLATFORM": "offscreen"})

log = tmp / "a.log"
log.write_text(f"=== start 2026-10-14T11:00:00 pid={DEAD}\n=== start 2026-10-14T11:05:00 pid={ALIVE}\n=== start 2026-10-13T18:05:17\n", encoding="utf-8")
r = run(log); out = log.read_text(encoding="utf-8")
check("a dead unfinished run is marked lost", f"=== lost" in out and f"pid={DEAD}" in out and "did not end normally" in out, out)
check("a live parallel run is not marked", f"=== lost" in out and f"pid={ALIVE}  (" not in out)
check("a new run writes start and exit with one pid", out.count("=== start") == 4 and out.rstrip().splitlines()[-1].startswith("=== exit") and
      out.rstrip().splitlines()[-1].split("pid=")[1] == out.split("=== start")[-1].split("pid=")[1].split()[0], out[-200:])
r = run(log); out2 = log.read_text(encoding="utf-8")
check("a repeated run does not mark the same run a second time", out2.count("=== lost") == 1, out2.count("=== lost"))
check("normal runs (start+exit) are not counted as lost", out2.count("=== lost") == 1)

# a crash: there is a trace, no matching exit, the next run marks it lost
crash = tmp / "c.log"; run(crash, "crash"); t = crash.read_text(encoding="utf-8")
crashed_pid = int(t.split("=== start")[-1].split("pid=")[1].split()[0])
check("on a crash: start recorded, no exit, there is a trace", "=== start" in t and "=== exit" not in t and ("Windows fatal exception" in t or "Segmentation fault" in t), t[-250:])
run(crash); t2 = crash.read_text(encoding="utf-8")
check("the next run marked the crashed one by its pid", f"=== lost" in t2 and f"pid={crashed_pid}" in t2, t2[:400])

# rotation
big = tmp / "big.log"; big.write_text("".join(f"line {i} " + "x" * 90 + "\n" for i in range(8000)), encoding="utf-8")
size_before = big.stat().st_size; run(big); size_after = big.stat().st_size
tail = big.read_text(encoding="utf-8").splitlines()
check("a large log is cut to its tail", size_before > diag.MAX_LOG_BYTES and size_after < diag.KEEP_TAIL_BYTES + 2000, (size_before, size_after))
check("the cut starts at a whole line and the tail is kept", tail[0].startswith("line ") and any(l.startswith("line 7999") for l in tail), tail[0][:30])
small = tmp / "small.log"; small.write_text("start\n", encoding="utf-8"); run(small)
check("a small log is not cut", small.read_text(encoding="utf-8").startswith("start\n"))
print("ALL OK" if ok_all else "SOME FAILED")
