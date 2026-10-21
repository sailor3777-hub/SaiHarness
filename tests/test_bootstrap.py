"""The installer and launcher: parsing requirements, finding missing packages, creating the environment, the Run.cmd / Install.cmd files."""
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

# ---- parsing requirements ----
req = tmp / "r.txt"
req.write_bytes("﻿httpx==0.28.1\r\n# a comment\r\n\r\nMarkdown_It.Py==4.2.0  # a tail\r\nPySide6\r\n-r other.txt\r\nmcp==2.3.0 ; python_version >= '3.11'\r\n".encode("utf-8"))
check("requirements: BOM, comments, names are normalized",
      bs.parse_requirements(req) == [("httpx", "0.28.1"), ("markdown-it-py", "4.2.0"), ("pyside6", None), ("mcp", "2.3.0")],
      bs.parse_requirements(req))
real = bs.parse_requirements(ROOT / "requirements.txt")
check("the real requirements.txt is parsed in full", len(real) >= 7 and all(v for _, v in real), real)

# ---- what is missing ----
reqs = [("a", "1.0"), ("b", None), ("c", "2.0"), ("d", "3.0")]
got = bs.missing(reqs, {"a": "1.0", "b": "9", "c": None, "d": "3.1"})
check("missing: no package and a different version, an unpinned version is accepted",
      len(got) == 2 and "c (not installed)" in got and "d (installed 3.1, required 3.0)" in got, got)
check("everything in place: empty", bs.missing(reqs, {"a": "1.0", "b": "1", "c": "2.0", "d": "3.0"}) == [])

# ---- the interpreter and versions in the environment ----
check("the current Python is suitable", bs.check_python() == "", bs.check_python())
v = bs.installed_versions(pathlib.Path(sys.executable), ["no-such-package-xyz", "pip"])
check("installed_versions: a missing package gives None", v["no-such-package-xyz"] is None, v)
check("installed_versions: a broken interpreter gives None for all", bs.installed_versions(tmp / "nope.exe", ["pip"]) == {"pip": None})

# ---- prepare on a fresh environment (venv is created offline, pip comes from ensurepip) ----
root = tmp / "proj"; root.mkdir()
logs = []; log = logs.append
(root / "requirements.txt").write_text("pip\n", encoding="utf-8")
check("check_only without .venv: code 1, the environment is not created", bs.prepare(root, check_only=True, log=log) == 1 and not (root / ".venv").exists(), logs)
calls = []
check("first run: the environment is created, packages in place, no installation needed",
      bs.prepare(root, log=log, installer=lambda *a: calls.append(a) or True) == 0 and bs.venv_python(root / ".venv").exists() and calls == [], (logs, calls))
pip_ver = bs.installed_versions(bs.venv_python(root / ".venv"), ["pip"])["pip"]
(root / "requirements.txt").write_text(f"pip=={pip_ver}\nno-such-package-xyz==1.0\n", encoding="utf-8")
logs.clear(); calls.clear()
check("check_only: a missing package gives 1 and installs nothing", bs.prepare(root, check_only=True, log=log, installer=lambda *a: calls.append(a) or True) == 1 and calls == [] and any("no-such-package-xyz" in m for m in logs), (logs, calls))
logs.clear(); calls.clear()
check("a package is missing: the installation is called", bs.prepare(root, log=log, installer=lambda *a: calls.append(a) or True) == 1 and len(calls) == 1, calls)
check("the installation reported success but the package is missing: an honest error", any("Still missing" in m for m in logs), logs)
logs.clear()
check("the installation failed: code 1 and a hint about the internet", bs.prepare(root, log=log, installer=lambda *a: False) == 1 and any("internet" in m for m in logs), logs)
(root / "requirements.txt").unlink()
check("no requirements.txt: code 1", bs.prepare(root, log=log) == 1)

# ---- Run.cmd / Install.cmd ----
run, inst = (ROOT / "Run.cmd").read_bytes(), (ROOT / "Install.cmd").read_bytes()
for name, data in (("Run.cmd", run), ("Install.cmd", inst)):
    try: data.decode("ascii"); ascii_ok = True
    except UnicodeDecodeError: ascii_ok = False
    check(f"{name}: ASCII only (cmd mixes up UTF-8 after chcp 65001), CRLF, no control characters",
          ascii_ok and b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n") and not [b for b in data if b < 9])
check("Run.cmd starts bootstrap, takes .venv, then py and python, can install Python with winget",
      b"scripts\\bootstrap.py" in run and b".venv\\Scripts\\python.exe" in run and b":find py -3" in run and b"winget install" in run)
check("Install.cmd calls Run.cmd with --no-launch by full path (cmd may not search the current folder)", b'call "%~dp0Run.cmd" --no-launch' in inst)
help_text = subprocess.run([sys.executable, str(ROOT / "scripts" / "bootstrap.py"), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
check("bootstrap.py --help works", "--check" in help_text and "--no-launch" in help_text, help_text)

print("ALL OK" if ok_all else "SOME FAILED")
