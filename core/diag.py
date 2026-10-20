"""Диагностика: лог вылетов и необработанных ошибок в data/crash.log.

Если приложение закрылось молча (вылет на уровне Qt/C++), в логе будет трасса от faulthandler.
Запуск пишет «=== start <время> pid=N», штатный выход «=== exit <время> pid=N». Номер процесса нужен,
потому что приложение можно запустить дважды одновременно, и тогда записи двух запусков перемешиваются.
При следующем запуске для каждого старта без выхода, чей процесс уже не существует, дописывается
«=== lost …»: это и есть аварийное завершение (процесс убили или он упал без трассы).
"""
import atexit
import ctypes
import datetime
import faulthandler
import os
import re
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import qInstallMessageHandler

MAX_LOG_BYTES = 512 * 1024   # больше этого размера лог усекается при запуске
KEEP_TAIL_BYTES = 256 * 1024
_START = re.compile(r"^=== start (\S+)(?: pid=(\d+))?")
_CLOSE = re.compile(r"^=== (?:exit|lost) (\S+)(?: pid=(\d+))?")

_log = None


def _write(text: str):
    if _log is not None:
        _log.write(text if text.endswith("\n") else text + "\n")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def note(text: str):
    """Строка в лог (если диагностика установлена). Пишется перед рискованными действиями, чтобы после
    молчаливого вылета было видно, что происходило последним."""
    _write(f"[{_now()}] {text}")


def memory_summary() -> str:
    """Память процесса и системы одной строкой (только Windows; иначе пусто)."""
    if os.name != "nt":
        return ""
    try:
        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        class ProcMem(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32")
        status = MemStatus()
        status.dwLength = ctypes.sizeof(status)
        kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        proc = ProcMem()
        proc.cb = ctypes.sizeof(proc)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.K32GetProcessMemoryInfo(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(proc), proc.cb)
        return (f"rss={proc.WorkingSetSize / 2**20:.0f}МБ ram_свободно={status.ullAvailPhys / 2**30:.1f}ГБ "
                f"из {status.ullTotalPhys / 2**30:.0f}ГБ нагрузка={status.dwMemoryLoad}%")
    except Exception:  # диагностика не должна ломать работу
        return ""


def pid_alive(pid: int) -> bool:
    """Существует ли ещё процесс с таким номером."""
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # доступ запрещён: процесс есть, просто чужой
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def unclean_runs(text: str) -> list[tuple[int, str]]:
    """Старты с номером процесса, у которых нет парного выхода: [(pid, время старта)]. Записи без pid (старый формат) пропускаются."""
    open_runs: dict[int, str] = {}
    for line in text.splitlines():
        m = _START.match(line)
        if m and m.group(2):
            open_runs[int(m.group(2))] = m.group(1)
            continue
        m = _CLOSE.match(line)
        if m and m.group(2):
            open_runs.pop(int(m.group(2)), None)
    return list(open_runs.items())


def _rotate(path: Path):
    """Слишком большой лог усекается до хвоста (начиная с целой строки)."""
    try:
        if path.stat().st_size <= MAX_LOG_BYTES:
            return
        data = path.read_bytes()[-KEEP_TAIL_BYTES:]
        data = data[data.find(b"\n") + 1:]
        path.write_bytes(data)
    except OSError:
        pass


def install(path: Path):
    global _log
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate(path)
    previous = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    _log = open(path, "a", encoding="utf-8", buffering=1)
    pid = os.getpid()
    _write("")
    for old_pid, started in unclean_runs(previous):
        if old_pid != pid and not pid_alive(old_pid):
            _write(f"=== lost {_now()} pid={old_pid}  (запуск {started} не завершился штатно: процесса уже нет)")
    _write(f"=== start {_now()} pid={pid}")
    faulthandler.enable(file=_log, all_threads=True)
    atexit.register(lambda: _write(f"=== exit {_now()} pid={pid}"))

    def excepthook(exc_type, exc, tb):
        _write("[python] " + "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = excepthook
    threading.excepthook = lambda a: excepthook(a.exc_type, a.exc_value, a.exc_traceback)

    def qt_handler(_mode, _context, message):
        _write(f"[qt] {message}")
        if sys.stderr is not None:  # под pythonw потока ошибок нет; исключение здесь ломает то, что Qt делает в этот момент
            try:
                sys.stderr.write(message + "\n")
            except (OSError, ValueError):
                pass

    qInstallMessageHandler(qt_handler)
