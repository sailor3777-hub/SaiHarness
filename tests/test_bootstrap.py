"""Установщик-запускатор: разбор requirements, поиск недостающих пакетов, создание окружения, файлы Run.cmd / Install.cmd."""
import importlib.util, pathlib, subprocess, sys, tempfile
from importlib import metadata

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts" / "bootstrap.py")
bs = importlib.util.module_from_spec(spec); spec.loader.exec_module(bs)

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())

# ---- разбор requirements ----
req = tmp / "r.txt"
req.write_bytes("﻿httpx==0.28.1\r\n# комментарий\r\n\r\nMarkdown_It.Py==4.2.0  # хвост\r\nPySide6\r\n-r other.txt\r\nmcp==2.3.0 ; python_version >= '3.11'\r\n".encode("utf-8"))
check("requirements: BOM, комментарии, имена приводятся к норме",
      bs.parse_requirements(req) == [("httpx", "0.28.1"), ("markdown-it-py", "4.2.0"), ("pyside6", None), ("mcp", "2.3.0")],
      bs.parse_requirements(req))
real = bs.parse_requirements(ROOT / "requirements.txt")
check("настоящий requirements.txt разбирается целиком", len(real) >= 7 and all(v for _, v in real), real)

# ---- что не хватает ----
reqs = [("a", "1.0"), ("b", None), ("c", "2.0"), ("d", "3.0")]
got = bs.missing(reqs, {"a": "1.0", "b": "9", "c": None, "d": "3.1"})
check("недостающие: нет пакета и чужая версия, не закреплённая версия принимается",
      len(got) == 2 and "c (не установлен)" in got and "d (стоит 3.1, нужна 3.0)" in got, got)
check("всё на месте: пусто", bs.missing(reqs, {"a": "1.0", "b": "1", "c": "2.0", "d": "3.0"}) == [])

# ---- интерпретатор и версии в окружении ----
check("текущий Python годится", bs.check_python() == "", bs.check_python())
v = bs.installed_versions(pathlib.Path(sys.executable), ["no-such-package-xyz", "pip"])
check("installed_versions: отсутствующий пакет даёт None", v["no-such-package-xyz"] is None, v)
check("installed_versions: сломанный интерпретатор даёт None для всех", bs.installed_versions(tmp / "nope.exe", ["pip"]) == {"pip": None})

# ---- prepare на свежем окружении (venv создаётся офлайн, pip берётся из ensurepip) ----
root = tmp / "proj"; root.mkdir()
logs = []; log = logs.append
(root / "requirements.txt").write_text("pip\n", encoding="utf-8")
check("check_only без .venv: код 1, окружение не создано", bs.prepare(root, check_only=True, log=log) == 1 and not (root / ".venv").exists(), logs)
calls = []
check("первый запуск: окружение создаётся, пакеты на месте, установка не нужна",
      bs.prepare(root, log=log, installer=lambda *a: calls.append(a) or True) == 0 and bs.venv_python(root / ".venv").exists() and calls == [], (logs, calls))
pip_ver = bs.installed_versions(bs.venv_python(root / ".venv"), ["pip"])["pip"]
(root / "requirements.txt").write_text(f"pip=={pip_ver}\nno-such-package-xyz==1.0\n", encoding="utf-8")
logs.clear(); calls.clear()
check("check_only: недостающий пакет даёт 1 и ничего не ставит", bs.prepare(root, check_only=True, log=log, installer=lambda *a: calls.append(a) or True) == 1 and calls == [] and any("no-such-package-xyz" in m for m in logs), (logs, calls))
logs.clear(); calls.clear()
check("не хватает пакета: вызывается установка", bs.prepare(root, log=log, installer=lambda *a: calls.append(a) or True) == 1 and len(calls) == 1, calls)
check("установка вернула успех, но пакета нет: честная ошибка", any("всё ещё не хватает" in m for m in logs), logs)
logs.clear()
check("установка не удалась: код 1 и подсказка про интернет", bs.prepare(root, log=log, installer=lambda *a: False) == 1 and any("интернет" in m for m in logs), logs)
(root / "requirements.txt").unlink()
check("нет requirements.txt: код 1", bs.prepare(root, log=log) == 1)

# ---- Run.cmd / Install.cmd ----
run, inst = (ROOT / "Run.cmd").read_bytes(), (ROOT / "Install.cmd").read_bytes()
for name, data in (("Run.cmd", run), ("Install.cmd", inst)):
    try: data.decode("ascii"); ascii_ok = True
    except UnicodeDecodeError: ascii_ok = False
    check(f"{name}: только ASCII (cmd путает UTF-8 после chcp 65001), CRLF, без управляющих символов",
          ascii_ok and b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n") and not [b for b in data if b < 9])
check("Run.cmd запускает bootstrap, берёт .venv, потом py и python, умеет ставить Python через winget",
      b"scripts\\bootstrap.py" in run and b".venv\\Scripts\\python.exe" in run and b":find py -3" in run and b"winget install" in run)
check("Install.cmd вызывает Run.cmd с --no-launch полным путём (cmd может не искать в текущей папке)", b'call "%~dp0Run.cmd" --no-launch' in inst)
help_text = subprocess.run([sys.executable, str(ROOT / "scripts" / "bootstrap.py"), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
check("bootstrap.py --help работает", "--check" in help_text and "--no-launch" in help_text, help_text)

print("ALL OK" if ok_all else "SOME FAILED")
