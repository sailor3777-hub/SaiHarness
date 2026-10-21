"""The confirmation window: long lines are wrapped, a window failure gives a refusal at once; the Qt message handler survives sys.stderr = None."""
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

# --- wrapping long lines ---
text = "word " * 144  # one line of 720 characters, like memory_save has
shown = format_arguments("toolbox.memory_save", {"topic": "trips", "title": "Makkum", "text": text})
check("no line is longer than the window", max(len(l) for l in shown.splitlines()) <= permission.LINE_WIDTH + 4, max(len(l) for l in shown.splitlines()))
check("readable labels", "Topic: trips" in shown and "What to remember:" in shown)
check("the text is not lost", shown.replace("\n", " ").split().count("word") >= 100)
cmd = "Get-ChildItem " + r"C:\some\very\long\path" * 20
shown = format_arguments("toolbox.shell_run", {"command": cmd})
check("the shell command is shown whole (no \"…\"), but in lines of the window width", "…" not in shown and "".join(shown.split()) .endswith("".join(cmd.split())[-30:]))
multi = format_arguments("s.t", {"a": "x\n\ny"})
check("multi-line values are not distorted", multi == "a:\n    x\n    \n    y", repr(multi))

# --- a window failure gives a refusal at once ---
broker = PermissionBroker(None, timeout=30)
def boom(*a, **k):
    raise RuntimeError("the window did not open")
orig = QMessageBox.question
QMessageBox.question = boom
result = {}
def worker():
    t = time.monotonic()
    result["answer"] = broker.ask("toolbox.memory_save", {"topic": "t", "title": "h", "text": "t"})
    result["secs"] = time.monotonic() - t
th = threading.Thread(target=worker); th.start()
deadline = time.monotonic() + 10
sys_excepthook, sys.excepthook = sys.excepthook, lambda *a: None  # the slot exception is expected
while th.is_alive() and time.monotonic() < deadline:
    app.processEvents(); time.sleep(0.02)
sys.excepthook = sys_excepthook
QMessageBox.question = orig
check("window failure: a refusal, not waiting for the timeout", result.get("answer") is False and result.get("secs", 99) < 5, result)

# --- a Qt warning with sys.stderr = None (pythonw) ---
tmp = pathlib.Path(tempfile.mkdtemp()) / "crash.log"
diag.install(tmp)
real = sys.stderr
sys.stderr = None
try:
    qWarning("a test Qt warning")
    app.processEvents()
finally:
    sys.stderr = real
log = tmp.read_text(encoding="utf-8")
check("the warning is written to the log", "a test Qt warning" in log)
check("and the handler did not crash", "Traceback" not in log and "NoneType" not in log, log[-300:])

print("\nALL OK" if ok_all else "\nSOME FAILED")
