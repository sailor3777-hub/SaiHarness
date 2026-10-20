import os, subprocess, sys, tempfile, pathlib, time
from core import diag

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
# мёртвый pid: запускаем короткий процесс и ждём его конца
p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait(); DEAD = p.pid
ALIVE = os.getpid()
check("pid_alive: свой процесс жив", diag.pid_alive(ALIVE))
check("pid_alive: завершённый процесс мёртв", not diag.pid_alive(DEAD))
check("pid_alive: несуществующий номер", not diag.pid_alive(4_000_000))

text = "\n".join([
    "=== start 2026-10-13T18:05:17",                       # старый формат без pid: игнорируется
    "=== start 2026-10-14T10:00:00 pid=111", "=== exit 2026-10-14T10:05:00 pid=111",
    "=== start 2026-10-14T11:00:00 pid=222",              # без выхода
    "=== start 2026-10-14T11:01:00 pid=333",              # параллельный запуск
    "=== exit 2026-10-14T11:30:00 pid=333",
    "=== start 2026-10-14T12:00:00 pid=444", "=== lost 2026-10-14T12:30:00 pid=444  (...)",
    "[qt] QFont::setPointSize: Point size <= 0",
])
check("unclean_runs: только старты без закрытия, parallel и lost учтены", diag.unclean_runs(text) == [(222, "2026-10-14T11:00:00")], diag.unclean_runs(text))
check("unclean_runs: пусто", diag.unclean_runs("") == [])

CODE = ("import sys; from pathlib import Path; from core import diag; diag.install(Path(sys.argv[1])); "
        "import faulthandler; (faulthandler._sigsegv() if len(sys.argv) > 2 and sys.argv[2] == 'crash' else None)")
def run(log, *extra):
    return subprocess.run([sys.executable, "-c", CODE, str(log), *extra], cwd="C:/SaiHarness", capture_output=True,
                          env={**os.environ, "PYTHONPATH": "C:/SaiHarness", "QT_QPA_PLATFORM": "offscreen"})

log = tmp / "a.log"
log.write_text(f"=== start 2026-10-14T11:00:00 pid={DEAD}\n=== start 2026-10-14T11:05:00 pid={ALIVE}\n=== start 2026-10-13T18:05:17\n", encoding="utf-8")
r = run(log); out = log.read_text(encoding="utf-8")
check("мёртвый незавершённый запуск помечен lost", f"=== lost" in out and f"pid={DEAD}" in out and "не завершился штатно" in out, out)
check("живой параллельный запуск не помечен", f"=== lost" in out and f"pid={ALIVE}  (" not in out)
check("новый запуск пишет start и exit с одним pid", out.count("=== start") == 4 and out.rstrip().splitlines()[-1].startswith("=== exit") and
      out.rstrip().splitlines()[-1].split("pid=")[1] == out.split("=== start")[-1].split("pid=")[1].split()[0], out[-200:])
r = run(log); out2 = log.read_text(encoding="utf-8")
check("повторный запуск не помечает тот же запуск второй раз", out2.count("=== lost") == 1, out2.count("=== lost"))
check("штатные запуски (start+exit) не считаются потерянными", out2.count("=== lost") == 1)

# падение: трасса есть, парного exit нет, следующий запуск помечает его lost
crash = tmp / "c.log"; run(crash, "crash"); t = crash.read_text(encoding="utf-8")
crashed_pid = int(t.split("=== start")[-1].split("pid=")[1].split()[0])
check("при падении: старт записан, выхода нет, есть трасса", "=== start" in t and "=== exit" not in t and ("Windows fatal exception" in t or "Segmentation fault" in t), t[-250:])
run(crash); t2 = crash.read_text(encoding="utf-8")
check("следующий запуск пометил упавший по его pid", f"=== lost" in t2 and f"pid={crashed_pid}" in t2, t2[:400])

# ротация
big = tmp / "big.log"; big.write_text("".join(f"строка {i} " + "x" * 90 + "\n" for i in range(8000)), encoding="utf-8")
size_before = big.stat().st_size; run(big); size_after = big.stat().st_size
tail = big.read_text(encoding="utf-8").splitlines()
check("большой лог усечён до хвоста", size_before > diag.MAX_LOG_BYTES and size_after < diag.KEEP_TAIL_BYTES + 2000, (size_before, size_after))
check("усечение начинается с целой строки и хвост сохранён", tail[0].startswith("строка ") and any(l.startswith("строка 7999") for l in tail), tail[0][:30])
small = tmp / "small.log"; small.write_text("начало\n", encoding="utf-8"); run(small)
check("маленький лог не усекается", small.read_text(encoding="utf-8").startswith("начало\n"))
print("ALL OK" if ok_all else "SOME FAILED")
