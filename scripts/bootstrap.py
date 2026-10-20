"""Установщик-запускатор SaiHarness: проверяет окружение и сам подтягивает зависимости, затем запускает приложение.

Работает на чистом стандартном Python (3.11+), поэтому его можно запускать любым интерпретатором до появления .venv.
Каждый запуск: есть ли .venv (если нет, создаётся), совпадают ли установленные версии с requirements.txt
(если нет, делается pip install -r), после чего стартует app.py. Быстрый путь без установки занимает доли секунды.

    python scripts\\bootstrap.py              проверить, доустановить, запустить приложение
    python scripts\\bootstrap.py --no-launch  только окружение (для первой установки)
    python scripts\\bootstrap.py --check      только проверка: код 0, если всё на месте, иначе 1; ничего не ставит
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
    """[(имя, версия или None)] из requirements.txt; комментарии и пустые строки пропускаются, BOM терпится."""
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
    """Версии пакетов в окружении python (None: не установлен или окружение сломано). Один запуск интерпретатора."""
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
    """Что нужно (до)установить: пакет отсутствует или версия не равна закреплённой."""
    bad = []
    for name, wanted in requirements:
        have = installed.get(name)
        if have is None:
            bad.append(f"{name} (не установлен)")
        elif wanted and have != wanted:
            bad.append(f"{name} (стоит {have}, нужна {wanted})")
    return bad


def check_python() -> str:
    """Пустая строка, если текущий интерпретатор годится, иначе причина."""
    if sys.version_info < MIN_PYTHON:
        return f"нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} или новее, а этот {sys.version.split()[0]}"
    if struct.calcsize("P") != 8:
        return "нужен 64-разрядный Python (PySide6 не ставится на 32-разрядный)"
    return ""


def ensure_venv(venv_dir: Path, log=print) -> Path:
    py = venv_python(venv_dir)
    if py.exists():
        return py
    log(f"Создаю окружение {venv_dir} …")
    venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
    return py


def install(python: Path, requirements_file: Path, log=print) -> bool:
    log("Устанавливаю зависимости (первый раз это несколько минут: PySide6 весит около 200 МБ) …")
    cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements_file)]
    return subprocess.run(cmd).returncode == 0


def prepare(root: Path = ROOT, venv_dir: Path | None = None, check_only: bool = False, log=print,
            installer=install) -> int:
    """Приводит окружение в порядок. Код выхода: 0 всё готово, 1 чего-то не хватает (check_only) или установка не удалась."""
    venv_dir = venv_dir or root / ".venv"
    requirements_file = root / "requirements.txt"
    if not requirements_file.exists():
        log(f"Нет файла {requirements_file}")
        return 1
    reqs = parse_requirements(requirements_file)
    py = venv_python(venv_dir)
    if not py.exists():
        if check_only:
            log("Окружение .venv не создано.")
            return 1
        reason = check_python()
        if reason:
            log(f"Не могу создать окружение: {reason}.")
            return 1
        py = ensure_venv(venv_dir, log)
    todo = missing(reqs, installed_versions(py, [n for n, _ in reqs]))
    if not todo:
        return 0
    log("Не хватает: " + "; ".join(todo))
    if check_only:
        return 1
    if not installer(py, requirements_file, log):
        log("Установка зависимостей не удалась. Проверь интернет и запусти ещё раз.")
        return 1
    todo = missing(reqs, installed_versions(py, [n for n, _ in reqs]))
    if todo:
        log("После установки всё ещё не хватает: " + "; ".join(todo))
        return 1
    log("Зависимости установлены.")
    return 0


def launch(root: Path = ROOT, venv_dir: Path | None = None):
    """Запускает приложение отдельным процессом без консоли и не ждёт его."""
    venv_dir = venv_dir or root / ".venv"
    gui = venv_dir / "Scripts" / "pythonw.exe"
    exe = gui if gui.exists() else venv_python(venv_dir)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), str(root / "app.py")], cwd=root, creationflags=flags, close_fds=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка окружения и запуск SaiHarness")
    parser.add_argument("--check", action="store_true", help="только проверить, ничего не устанавливать и не запускать")
    parser.add_argument("--no-launch", action="store_true", help="подготовить окружение, но не запускать приложение")
    args = parser.parse_args(argv)
    if not sys.stdout.isatty():  # вывод в канал или файл идёт в системной кодировке, русский в ней может не кодироваться
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    code = prepare(check_only=args.check)
    if code == 0 and not (args.check or args.no_launch):
        launch()
    elif code == 0 and args.no_launch and not args.check:
        print("Готово. Запускай Run.cmd.")
    return code


if __name__ == "__main__":
    sys.exit(main())
