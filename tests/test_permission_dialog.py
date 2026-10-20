"""Окно подтверждения: длинные строки переносятся, сбой окна даёт отказ сразу; обработчик сообщений Qt переживает sys.stderr = None."""
import os, pathlib, sys, tempfile, threading, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer, qWarning
from PySide6.QtWidgets import QApplication, QMessageBox

from core import diag
from ui import permission
from ui.permission import PermissionBroker, format_arguments

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

app = QApplication([])

# --- перенос длинных строк ---
text = "слово " * 120  # одна строка в 720 символов, как у memory_save
shown = format_arguments("toolbox.memory_save", {"topic": "поездки", "title": "Маккум", "text": text})
check("ни одна строка не длиннее окна", max(len(l) for l in shown.splitlines()) <= permission.LINE_WIDTH + 4, max(len(l) for l in shown.splitlines()))
check("подписи на русском", "Тема: поездки" in shown and "Что запомнить:" in shown)
check("текст не потерян", shown.replace("\n", " ").split().count("слово") >= 100)
cmd = "Get-ChildItem " + "C:\очень\длинный\путь" * 20
shown = format_arguments("toolbox.shell_run", {"command": cmd})
check("команда shell целиком (без «…»), но в строки по ширине", "…" not in shown and "".join(shown.split()) .endswith("".join(cmd.split())[-30:]))
multi = format_arguments("s.t", {"a": "x\n\ny"})
check("многострочные значения не искажаются", multi == "a:\n    x\n    \n    y", repr(multi))

# --- сбой окна даёт отказ сразу ---
broker = PermissionBroker(None, timeout=30)
def boom(*a, **k):
    raise RuntimeError("окно не открылось")
orig = QMessageBox.question
QMessageBox.question = boom
result = {}
def worker():
    t = time.monotonic()
    result["answer"] = broker.ask("toolbox.memory_save", {"topic": "т", "title": "з", "text": "т"})
    result["secs"] = time.monotonic() - t
th = threading.Thread(target=worker); th.start()
deadline = time.monotonic() + 10
sys_excepthook, sys.excepthook = sys.excepthook, lambda *a: None  # исключение слота ожидаемо
while th.is_alive() and time.monotonic() < deadline:
    app.processEvents(); time.sleep(0.02)
sys.excepthook = sys_excepthook
QMessageBox.question = orig
check("сбой окна: отказ, а не ожидание таймаута", result.get("answer") is False and result.get("secs", 99) < 5, result)

# --- Qt-предупреждение при sys.stderr = None (pythonw) ---
tmp = pathlib.Path(tempfile.mkdtemp()) / "crash.log"
diag.install(tmp)
real = sys.stderr
sys.stderr = None
try:
    qWarning("тестовое предупреждение Qt")
    app.processEvents()
finally:
    sys.stderr = real
log = tmp.read_text(encoding="utf-8")
check("предупреждение записано в лог", "тестовое предупреждение Qt" in log)
check("и обработчик не упал", "Traceback" not in log and "NoneType" not in log, log[-300:])

print("\nALL OK" if ok_all else "\nЕСТЬ ОШИБКИ")
