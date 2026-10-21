"""SaiHarness installer and launcher: checks the environment, pulls in dependencies by itself, then starts the app.

It runs on plain standard Python (3.11+), so any interpreter can start it before .venv exists.
Every run: whether .venv exists (if not, it is created), whether the installed versions match requirements.txt
(if not, pip install -r is run), and then app.py is started. The fast path without installing takes a fraction of a second.

    python scripts\\bootstrap.py              check, install what is missing, start the app
    python scripts\\bootstrap.py --no-launch  only the environment (for the first installation)
    python scripts\\bootstrap.py --check      check only: exit code 0 if everything is in place, otherwise 1; installs nothing
"""
import argparse
import re
import struct
import subprocess
import sys
import venv
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 11)
REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==\s*([^\s;#]+))?")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(path: Path) -> list[tuple[str, str | None]]:
    """[(name, version or None)] from requirements.txt; comments and empty lines are skipped, a BOM is tolerated."""
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = REQ_LINE.match(line)
        if m:
            out.append((normalize(m.group(1)), m.group(2)))
    return out


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / "Scripts" / "python.exe"


def installed_versions(python: Path, names: list[str]) -> dict[str, str | None]:
    """Package versions in the python environment (None: not installed or the environment is broken). One interpreter run."""
    code = ("import sys, json\nfrom importlib import metadata\nout = {}\n"
            "for n in sys.argv[1:]:\n    try: out[n] = metadata.version(n)\n    except metadata.PackageNotFoundError: out[n] = None\n"
            "print(json.dumps(out))")
    try:
        r = subprocess.run([str(python), "-c", code, *names], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {n: None for n in names}
        import json
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return {n: None for n in names}


def missing(requirements: list[tuple[str, str | None]], installed: dict[str, str | None]) -> list[str]:
    """What needs to be installed: a package is missing or its version differs from the pinned one."""
    bad = []
    for name, wanted in requirements:
        have = installed.get(name)
        if have is None:
            bad.append(f"{name} (not installed)")
        elif wanted and have != wanted:
            bad.append(f"{name} (installed {have}, required {wanted})")
    return bad


def check_python() -> str:
    """An empty string if the current interpreter is suitable, otherwise the reason."""
    if sys.version_info < MIN_PYTHON:
        return f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required, this one is {sys.version.split()[0]}"
    if struct.calcsize("P") != 8:
        return "a 64-bit Python is required (PySide6 does not install on 32-bit)"
    return ""


def ensure_venv(venv_dir: Path, log=print) -> Path:
    py = venv_python(venv_dir)
    if py.exists():
        return py
    log(f"Creating the environment {venv_dir} …")
    venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
    return py


def install(python: Path, requirements_file: Path, log=print) -> bool:
    log("Installing dependencies (the first time takes a few minutes: PySide6 is about 200 MB) …")
    cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements_file)]
    return subprocess.run(cmd).returncode == 0


def prepare(root: Path = ROOT, venv_dir: Path | None = None, check_only: bool = False, log=print,
            installer=install) -> int:
    """Puts the environment in order. Exit code: 0 all ready, 1 something is missing (check_only) or the installation failed."""
    venv_dir = venv_dir or root / ".venv"
    requirements_file = root / "requirements.txt"
    if not requirements_file.exists():
        log(f"No file {requirements_file}")
        return 1
    reqs = parse_requirements(requirements_file)
    py = venv_python(venv_dir)
    if not py.exists():
        if check_only:
            log("The .venv environment has not been created.")
            return 1
        reason = check_python()
        if reason:
            log(f"Cannot create the environment: {reason}.")
            return 1
        py = ensure_venv(venv_dir, log)
    todo = missing(reqs, installed_versions(py, [n for n, _ in reqs]))
    if not todo:
        return 0
    log("Missing: " + "; ".join(todo))
    if check_only:
        return 1
    if not installer(py, requirements_file, log):
        log("Installing the dependencies failed. Check the internet connection and run it again.")
        return 1
    todo = missing(reqs, installed_versions(py, [n for n, _ in reqs]))
    if todo:
        log("Still missing after the installation: " + "; ".join(todo))
        return 1
    log("Dependencies installed.")
    return 0


def launch(root: Path = ROOT, venv_dir: Path | None = None):
    """Starts the app as a separate process without a console and does not wait for it."""
    venv_dir = venv_dir or root / ".venv"
    gui = venv_dir / "Scripts" / "pythonw.exe"
    exe = gui if gui.exists() else venv_python(venv_dir)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), str(root / "app.py")], cwd=root, creationflags=flags, close_fds=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the environment and start SaiHarness")
    parser.add_argument("--check", action="store_true", help="check only, do not install or start anything")
    parser.add_argument("--no-launch", action="store_true", help="prepare the environment but do not start the app")
    args = parser.parse_args(argv)
    if not sys.stdout.isatty():  # output to a pipe or file uses the system encoding, which may not encode every character
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    code = prepare(check_only=args.check)
    if code == 0 and not (args.check or args.no_launch):
        launch()
    elif code == 0 and args.no_launch and not args.check:
        print("Done. Start Run.cmd.")
    return code


if __name__ == "__main__":
    sys.exit(main())
