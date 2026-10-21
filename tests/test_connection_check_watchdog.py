import sys, pathlib, time, socket, threading, subprocess, tempfile, os
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from core import db
db.DB_PATH = pathlib.Path(sys.argv[1])
from ui.main_window import MainWindow
from ui.request_dialog import RequestDialog
from worker import ModelListRequest
ROOT = str(pathlib.Path(__file__).resolve().parents[1])  # this checkout, not a hardcoded copy

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

app = QApplication([])
def wait(cond, timeout=30.0):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents(); time.sleep(0.005)
    app.processEvents()
    return cond()

w = MainWindow(); ctx = w.context; c = ctx.connections; b = ctx.builder; v = w.chat_view; sel = w.selector

# --- the "Check" button ---
c.url.setText("http://"); c.check_btn.click()
check("check: a wrong address -> a message, no request", c.status.text() == "✗ No host found" and not c.check_req.busy)

c.url.setText("127.0.0.1:1234"); c.check_btn.click()
check("check: the button is disabled during the request", not c.check_btn.isEnabled() and c.check_btn.text() == "Checking…"
      and "127.0.0.1:1234/v1" in c.status.text())
check("check: success on a real LM Studio", wait(lambda: not c.check_req.busy) and c.status.text().startswith("✓ Connected, models on the server:"), c.status.text())
print("      ", c.status.text())
check("check: the button is back", c.check_btn.isEnabled() and c.check_btn.text() == "Check")
check("a check saves nothing", db.list_connections() == [])

c.url.setText("localhost:1"); c.check_btn.click(); wait(lambda: not c.check_req.busy)
check("check: the server is unavailable", c.status.text().startswith("✗ Cannot reach"), c.status.text())

# --- the watchdog timer on a "silent" server ---
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5); hung_port = srv.getsockname()[1]
conns = []
threading.Thread(target=lambda: [conns.append(srv.accept()) for _ in range(5)], daemon=True).start()
ModelListRequest.TIMEOUT_MS = 800
c.url.setText(f"127.0.0.1:{hung_port}"); t0 = time.time(); c.check_btn.click()
check("watchdog: the interface got an answer in time", wait(lambda: not c.check_req.busy, 5) and time.time() - t0 < 2.5, f"{time.time()-t0:.1f}s")
check("watchdog: a clear message", c.status.text() == "✗ The server did not answer within 0.8 s", c.status.text())
check("watchdog: the button is available again", c.check_btn.isEnabled())
ModelListRequest.TIMEOUT_MS = 8000

# --- Builder: "Refresh" ---
c._new(); c.name.setText("Claw"); c.url.setText("127.0.0.1:1234"); c.save_btn.click()
b.refresh_btn.click()   # before the first request ends
check("Builder: the button is disabled while the request runs", not b.refresh_btn.isEnabled() or not b.models_req.busy)
for _ in range(30): b.refresh_btn.click()
check("Builder: repeated clicks do not multiply requests", len(b.models_req._threads) <= 2, len(b.models_req._threads))
check("Builder: the list loaded", wait(lambda: not b.models_req.busy) and b.base_model.count() >= 1 and b.refresh_btn.isEnabled())

# --- the whole chain: profile -> request -> view ---
b.name.setText("Beta"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.system_prompt.setPlainText("Your name is Beta"); b.memory.setPlainText("The user's name is Sai"); b.max_tokens.setValue(200); b.save_btn.click()
check("the profile summary shows system and memory", "System: Your name is Beta" in sel.summary.text() and "Memory: 22 chars" in sel.summary.text(), sel.summary.text())
v.input.setPlainText("what is your name?"); v.send_btn.click()
check("the request is saved for viewing", w.chat_id in w.last_requests)
req = w.last_requests[w.chat_id]
check("system prompt + memory in the request", req["messages"][0] == {"role": "system", "content": "Your name is Beta\n\nMemory:\nThe user's name is Sai"}
      and req["messages"][-1] == {"role": "user", "content": "what is your name?"}, req["messages"])
d = RequestDialog(w, req)
shown = d.findChild(QPlainTextEdit).toPlainText()
check("the dialog shows the system prompt", "Your name is Beta" in shown and "gemma-4-e4b-it-erotica" in d.layout().itemAt(0).widget().text())
check("the dialog without a request does not crash", RequestDialog(w, None) is not None)
wait(lambda: w.stream is None, 90)
print("      model answer:", repr((db.list_messages(w.chat_id)[-1]["content"] or db.list_messages(w.chat_id)[-1]["reasoning"])[:100]))

# --- crash log ---
tmp = pathlib.Path(tempfile.mkdtemp())
code = "import sys; from pathlib import Path; from PySide6.QtWidgets import QApplication; from core import diag; " \
       "diag.install(Path(sys.argv[1])); app = QApplication([]); import faulthandler; " \
       "(faulthandler._sigsegv() if sys.argv[2] == 'crash' else None); " \
       "(1/0) if sys.argv[2] == 'exc' else None"
for mode in ("clean", "crash", "exc"):
    log = tmp / f"{mode}.log"
    subprocess.run([sys.executable, "-c", code, str(log), mode], cwd=ROOT, capture_output=True,
                   env={**os.environ, "PYTHONPATH": ROOT, "QT_QPA_PLATFORM": "offscreen"})
    text = log.read_text(encoding="utf-8")
    if mode == "clean": check("diag: a normal exit is marked exit", "=== start" in text and "=== exit" in text)
    if mode == "crash": check("diag: a crash leaves a trace and no exit", "Segmentation fault" in text or "Windows fatal exception" in text and "=== exit" not in text, text[-200:])
    if mode == "exc": check("diag: an exception gets into the log", "ZeroDivisionError" in text)

w.close(); srv.close()
print("ALL OK" if ok_all else "SOME FAILED")
