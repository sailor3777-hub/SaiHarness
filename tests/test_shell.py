"""shell_run / shell_output на реальном MCP-сервере: рабочая папка, вывод, таймаут, обрезка с offset, запись файлов."""
import sys, time, pathlib, tempfile
from core import db, shell
from core.mcp_manager import McpManager, load_config
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

# ---- разрешения ----
tools = {t.name: t for t in m.all_tools()}
check("shell_run не read-only (спросит), shell_output read-only",
      not tools["shell_run"].read_only and tools["shell_output"].read_only)
check("у shell_run увеличенный таймаут вызова", TOOL_TIMEOUTS["toolbox.shell_run"] > shell.TIMEOUT)

# ---- обычный запуск ----
t = run("Write-Output 'привет'; (Get-Location).Path")
check("вывод в UTF-8, код 0", "код выхода 0" in t and "привет" in t, t)
check("по умолчанию cwd = первая разрешённая папка", str(root.resolve()) in t, t)
t = run("(Get-Location).Path", root / "sub"); check("cwd внутри корня", str((root / "sub").resolve()) in t, t)
t = run("exit 3"); check("код выхода передаётся", "код выхода 3" in t, t)
t = run("Write-Error 'бум'; 'после'"); check("stderr в выводе", "бум" in t and "после" in t, t)
check("ошибки приходят текстом, не CLIXML", "CLIXML" not in t and "<Objs" not in t, t)
t = run("   "); check("пустая команда", t.startswith("ОШИБКА") and "пуст" in t, t)
t = run("'кавычки \"двойные\" и ''одинарные'''"); check("кавычки не ломают команду", 'двойные' in t and 'одинарные' in t, t)

# ---- окружение: сервер получает полное, а не урезанное окружение mcp (иначе не работают модули вроде Hyper-V) ----
t = run("'win=' + $env:windir; 'pd=' + $env:ProgramData; 'psm=' + [bool]$env:PSModulePath; 'pc=' + $env:COMPUTERNAME")
check("в shell_run есть windir, ProgramData, PSModulePath, COMPUTERNAME",
      "win=C:" in t.replace("c:", "C:") and "pd=C:" in t.replace("c:", "C:") and "psm=True" in t and "pc=" in t and "pc=\n" not in t, t)

t = run("'Test2' | powershell -NoProfile -Command '$s = Read-Host; $s.Length'")
check("конвейер во внешнюю программу без BOM", "\n5\n" in t, t)

# ---- рабочая папка любая, не только внутри корней ----
t = run("'x' | Set-Content here.txt; (Get-Location).Path", outside)
check("cwd вне корней разрешён (доступ ко всей системе)", (outside / "here.txt").exists() and str(outside.resolve()) in t, t)
t = run("(Get-Location).Path", root / ".." / "outside"); check("cwd через .. тоже работает", str(outside.resolve()) in t, t)
t = run("Copy-Item '%s' '%s'" % (outside / "here.txt", root / "copied.txt"))
check("копирование между папками по абсолютным путям", (root / "copied.txt").exists(), t)
t = run("(Get-Location).Path", "sub"); check("относительный cwd считается от папки по умолчанию", str((root / "sub").resolve()) in t, t)
t = run("'x'", outside / "нет_такой"); check("несуществующий cwd: ошибка", t.startswith("ОШИБКА") and "не существует" in t, t)
f = root / "file.txt"; f.write_text("a", encoding="utf-8"); t = run("'x'", f); check("cwd-файл вместо папки", t.startswith("ОШИБКА"), t)

# ---- без разрешённых папок: служебная папка ----
db.DB_PATH = tmp / "empty.db"
cfg0 = load_config(tmp / "mcp0.json", db.DB_PATH); cfg0["mcpServers"]["toolbox"]["env"]["SAI_SHELL_DIR"] = str(tmp / "scratch")
m0 = McpManager(); m0.connect_all(cfg0)
t = m0.call("toolbox", "shell_run", {"command": "(Get-Location).Path; hostname"}, 90)[0]
check("без корней команда работает в служебной папке", "код выхода 0" in t and str((tmp / "scratch").resolve()) in t, t)
m0.shutdown()

# ---- запись файлов (то, ради чего задумано) ----
t = run("Set-Content -Path notes.txt -Value 'создано моделью' -Encoding UTF8; Get-Content notes.txt -Encoding UTF8")
check("shell_run создаёт файл в корне", (root / "notes.txt").exists() and "создано моделью" in (root / "notes.txt").read_text(encoding="utf-8-sig") and "создано моделью" in t, t)
r = m.call("toolbox", "fs_read", {"path": str(root / "notes.txt")})[0]
check("созданный файл читается через fs_read", "создано моделью" in r, r)
t = run("New-Item -ItemType Directory -Force -Path newdir | Out-Null; 'print(1)' | Set-Content newdir\\a.py; Get-ChildItem newdir -Name")
check("создаётся папка с файлом", (root / "newdir" / "a.py").exists() and "a.py" in t, t)

# ---- длинный вывод: обрезка и offset без повторного запуска ----
t = run("1..3000 | ForEach-Object { 'line{0:D5}' -f $_ }")
check("вывод обрезан с подсказкой shell_output", "[обрезано:" in t and "shell_output" in t and len(t) <= 10_800, len(t))
rid = int(t.split("run_id=")[1].split("]")[0].split(";")[0]); nxt = int(t.rsplit("offset=", 1)[1].rstrip("]"))
t2 = out(rid, nxt); check("shell_output продолжает с offset", "line00001" not in t2 and "line010" in t2, t2[:60])
parts, off, guard = [], 0, 0
while guard < 20:
    guard += 1
    chunk = out(rid, off)
    body = chunk.split("\n[")[0] if "\n[" in chunk and chunk.rstrip().endswith("]") else chunk
    parts.append(body)
    if "продолжение:" not in chunk: break
    off = int(chunk.rsplit("offset=", 1)[1].rstrip("]"))
joined = "\n".join(parts)
check("кусками читается весь вывод без потерь", "line00001" in joined and "line03000" in joined, joined[-80:])
check("несуществующий run_id", out(9999).startswith("ОШИБКА"))
check("offset за концом", out(rid, 10_000_000).startswith("ОШИБКА"))
t = run("'короткий'"); check("короткий вывод без обрезки", "[обрезано" not in t and "короткий" in t, t)
t = run("$null"); check("пустой вывод помечен", "(вывода нет)" in t, t)

# ---- таймаут и убийство дерева процессов (напрямую в core.shell, чтобы не ждать 60 с) ----
marker = tmp / "alive.txt"
t0 = time.time()
code, text, timed = shell.run(f"'старт'; Start-Sleep 2; 'x' | Set-Content '{marker}'; 'конец'", str(root), timeout=0.8)
check("таймаут: убито, код None, вывод частичный", timed and code is None and time.time() - t0 < 6, (code, timed, time.time() - t0))
time.sleep(2.5); check("после таймаута процесс не дописал файл", not marker.exists())
code, text, timed = shell.run("exit 7", str(root)); check("core.shell.run: код выхода", (code, timed) == (7, False), (code, text))

m.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
