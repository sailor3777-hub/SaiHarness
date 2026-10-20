import sys, pathlib, time, socket, threading, subprocess, tempfile, os
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from core import db
db.DB_PATH = pathlib.Path(sys.argv[1])
from ui.main_window import MainWindow
from ui.request_dialog import RequestDialog
from worker import ModelListRequest

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

# --- кнопка «Проверить» ---
c.url.setText("http://"); c.check_btn.click()
check("проверка: неверный адрес -> сообщение, без запроса", c.status.text() == "✗ Не найден хост" and not c.check_req.busy)

c.url.setText("127.0.0.1:1234"); c.check_btn.click()
check("проверка: во время запроса кнопка выключена", not c.check_btn.isEnabled() and c.check_btn.text() == "Проверяю…"
      and "127.0.0.1:1234/v1" in c.status.text())
check("проверка: успех на реальном LM Studio", wait(lambda: not c.check_req.busy) and c.status.text().startswith("✓ Подключено, моделей на сервере:"), c.status.text())
print("      ", c.status.text())
check("проверка: кнопка возвращена", c.check_btn.isEnabled() and c.check_btn.text() == "Проверить")
check("проверка ничего не сохраняет", db.list_connections() == [])

c.url.setText("localhost:1"); c.check_btn.click(); wait(lambda: not c.check_req.busy)
check("проверка: сервер недоступен", c.status.text().startswith("✗ Нет связи"), c.status.text())

# --- сторожевой таймер на «молчащем» сервере ---
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5); hung_port = srv.getsockname()[1]
conns = []
threading.Thread(target=lambda: [conns.append(srv.accept()) for _ in range(5)], daemon=True).start()
ModelListRequest.TIMEOUT_MS = 800
c.url.setText(f"127.0.0.1:{hung_port}"); t0 = time.time(); c.check_btn.click()
check("watchdog: интерфейс получил ответ вовремя", wait(lambda: not c.check_req.busy, 5) and time.time() - t0 < 2.5, f"{time.time()-t0:.1f}s")
check("watchdog: понятное сообщение", c.status.text() == "✗ Сервер не ответил за 0.8 с", c.status.text())
check("watchdog: кнопка снова доступна", c.check_btn.isEnabled())
ModelListRequest.TIMEOUT_MS = 8000

# --- Builder: «Обновить» ---
c._new(); c.name.setText("Claw"); c.url.setText("127.0.0.1:1234"); c.save_btn.click()
b.refresh_btn.click()   # до окончания первого запроса
check("Builder: кнопка выключена, пока идёт запрос", not b.refresh_btn.isEnabled() or not b.models_req.busy)
for _ in range(30): b.refresh_btn.click()
check("Builder: повторные клики не плодят запросы", len(b.models_req._threads) <= 2, len(b.models_req._threads))
check("Builder: список подгрузился", wait(lambda: not b.models_req.busy) and b.base_model.count() >= 1 and b.refresh_btn.isEnabled())

# --- вся цепочка: профиль -> запрос -> просмотр ---
b.name.setText("Beta"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.system_prompt.setPlainText("Your name is Beta"); b.memory.setPlainText("Пользователя зовут Сай"); b.max_tokens.setValue(200); b.save_btn.click()
check("сводка профиля показывает system и память", "System: Your name is Beta" in sel.summary.text() and "Память: 22 симв." in sel.summary.text(), sel.summary.text())
v.input.setPlainText("как тебя зовут?"); v.send_btn.click()
check("запрос сохранён для просмотра", w.chat_id in w.last_requests)
req = w.last_requests[w.chat_id]
check("system prompt + память в запросе", req["messages"][0] == {"role": "system", "content": "Your name is Beta\n\nПамять:\nПользователя зовут Сай"}
      and req["messages"][-1] == {"role": "user", "content": "как тебя зовут?"}, req["messages"])
d = RequestDialog(w, req)
shown = d.findChild(QPlainTextEdit).toPlainText()
check("диалог показывает system prompt", "Your name is Beta" in shown and "gemma-4-e4b-it-erotica" in d.layout().itemAt(0).widget().text())
check("диалог без запроса не падает", RequestDialog(w, None) is not None)
wait(lambda: w.stream is None, 90)
print("      ответ модели:", repr((db.list_messages(w.chat_id)[-1]["content"] or db.list_messages(w.chat_id)[-1]["reasoning"])[:100]))

# --- лог вылетов ---
tmp = pathlib.Path(tempfile.mkdtemp())
code = "import sys; from pathlib import Path; from PySide6.QtWidgets import QApplication; from core import diag; " \
       "diag.install(Path(sys.argv[1])); app = QApplication([]); import faulthandler; " \
       "(faulthandler._sigsegv() if sys.argv[2] == 'crash' else None); " \
       "(1/0) if sys.argv[2] == 'exc' else None"
for mode in ("clean", "crash", "exc"):
    log = tmp / f"{mode}.log"
    subprocess.run([sys.executable, "-c", code, str(log), mode], cwd="C:/SaiHarness", capture_output=True,
                   env={**os.environ, "PYTHONPATH": "C:/SaiHarness", "QT_QPA_PLATFORM": "offscreen"})
    text = log.read_text(encoding="utf-8")
    if mode == "clean": check("diag: штатный выход помечен exit", "=== start" in text and "=== exit" in text)
    if mode == "crash": check("diag: вылет оставляет трассу и без exit", "Segmentation fault" in text or "Windows fatal exception" in text and "=== exit" not in text, text[-200:])
    if mode == "exc": check("diag: исключение попадает в лог", "ZeroDivisionError" in text)

w.close(); srv.close()
print("ALL OK" if ok_all else "SOME FAILED")
