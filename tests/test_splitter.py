"""The three panels of the main window can be made narrow, and their widths survive a restart."""
import json
import sys
from pathlib import Path

from core import db

db.DB_PATH = Path(sys.argv[1])

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

app = QApplication(sys.argv)

from ui import main_window  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

failed = False


def check(name: str, ok: bool, detail: str = ""):
    global failed
    print(f"{'OK' if ok else 'FAIL'} {name}{' ' + detail if detail and not ok else ''}")
    failed |= not ok


def min_width(widget) -> int:
    return max(widget.minimumWidth(), widget.minimumSizeHint().width()) if widget.minimumWidth() == 0 \
        else widget.minimumWidth()


w = MainWindow(connect_mcp=False)
w.resize(1400, 850)
w.show()
app.processEvents()

for name, panel in (("chats", w.chat_list), ("dialog", w.chat_view), ("context", w.context)):
    check(f"{name} panel can be narrow", min_width(panel) <= main_window.PANEL_MIN_WIDTH, str(min_width(panel)))

# every tab page is in a scroll area, so a wide page does not hold the splitter
for i in range(w.context.tabs.count()):
    area = w.context.tabs.widget(i)
    check(f"tab {w.context.tabs.tabText(i)} scrolls", area.widgetResizable() and area.minimumSizeHint().width() < 200,
          str(area.minimumSizeHint().width()))

# drag the context panel down to a narrow width
w.splitter.setSizes([240, 1000, 150])
app.processEvents()
check("context panel shrinks to 150", abs(w.splitter.sizes()[2] - 150) <= 2, str(w.splitter.sizes()))
check("panels cannot collapse", not w.splitter.childrenCollapsible())

# a long status during generation ("Tool: …") must not change the dialog panel's width
v = w.chat_view
hint_before = v.minimumSizeHint().width()
sizes_before = w.splitter.sizes()
long_status = "Tool: shell_run(command=\"" + "Get-ChildItem -Recurse C:\\some\\very\\long\\path | " * 8 + "\")"
v.set_busy(True)
v.set_status(long_status)
app.processEvents()
check("a long status does not widen the dialog panel", v.minimumSizeHint().width() == hint_before,
      f"{hint_before} -> {v.minimumSizeHint().width()}")
check("a long status does not move the splitter", w.splitter.sizes() == sizes_before, f"{sizes_before} -> {w.splitter.sizes()}")
check("the status keeps the whole text", v.status.text() == long_status)
check("the whole text is in the tooltip", v.status.toolTip() == long_status)
check("a long status is shown cut with an ellipsis", QLabel.text(v.status).endswith("…"), QLabel.text(v.status))
v.set_status("Generating…")
app.processEvents()
check("a short status is shown whole", QLabel.text(v.status) == "Generating…", QLabel.text(v.status))
v.set_title("A very long chat title " * 10)
app.processEvents()
check("a long chat title does not widen the dialog panel", v.minimumSizeHint().width() == hint_before)
v.set_busy(False)

w.splitter.splitterMoved.emit(0, 1)
w._save_sizes_timer.timeout.emit()
saved = json.loads(db.get_setting(main_window.SPLITTER_KEY))
check("sizes saved", saved == w.splitter.sizes(), str(saved))
w.context.shutdown()
w.close()

w2 = MainWindow(connect_mcp=False)
w2.resize(1400, 850)
w2.show()
app.processEvents()
check("sizes restored", abs(w2.splitter.sizes()[2] - saved[2]) <= 30, f"{w2.splitter.sizes()} vs {saved}")

db.set_setting(main_window.SPLITTER_KEY, "garbage")
check("bad saved value is ignored", main_window._saved_sizes() is None)
w2.context.shutdown()

print("FAILED" if failed else "ALL OK")
