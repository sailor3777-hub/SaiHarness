import sys, json, pathlib, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PySide6.QtWidgets import QApplication, QMessageBox

from core import db, llm
db.DB_PATH = pathlib.Path(sys.argv[1])

ok_all = True
def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name)

# ---- миграция старой схемы ----
old = sqlite3.connect(db.DB_PATH)
old.executescript("""CREATE TABLE model_profiles (name TEXT PRIMARY KEY, base_model TEXT NOT NULL,
 system_prompt TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0.7,
 max_tokens INTEGER NOT NULL DEFAULT 2048, memory TEXT NOT NULL DEFAULT '');
 INSERT INTO model_profiles(name, base_model) VALUES('Тест','Базовая Модель');""")
old.commit(); old.close()
p = db.list_profiles()[0]
check("миграция: старый профиль цел, connection пуст", p["name"] == "Тест" and p["connection"] == "")

# ---- фейковый сервер ----
STATE = {"mode": "ok", "auth": None, "path": None}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        STATE["auth"] = self.headers.get("Authorization"); STATE["path"] = self.path
        m = STATE["mode"]
        code, body = 200, json.dumps({"data": [{"id": "qwen2.5-7b"}, {"id": "llama-3.1-8b"}, {"id": "bge-embed"}]})
        if m == "401": code, body = 401, "{}"
        if m == "500": code, body = 500, "{}"
        if m == "junk": body = "<html>not json"
        if m == "wrongshape": body = json.dumps({"models": []})
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body.encode())
srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}/v1"

# ---- core.llm ----
check("list_models: отсортированный список", llm.list_models(base) == ["bge-embed", "llama-3.1-8b", "qwen2.5-7b"])
check("путь /v1/models, без ключа нет заголовка", STATE["path"] == "/v1/models" and STATE["auth"] is None)
llm.list_models(base, "sekret")
check("ключ уходит как Bearer", STATE["auth"] == "Bearer sekret")
def err(mode):
    STATE["mode"] = mode
    try: llm.list_models(base); return None
    except llm.LLMError as e: return str(e)
check("401 -> отклонён ключ", "отклонил ключ (401)" in err("401"))
check("500 -> ошибка сервера", err("500") == "Ошибка сервера: 500")
check("не JSON -> некорректный ответ", err("junk") == "Некорректный ответ сервера")
check("неверная форма -> некорректный ответ", err("wrongshape") == "Некорректный ответ сервера")
STATE["mode"] = "ok"
try: llm.list_models("http://127.0.0.1:1/v1"); check("нет сервера", False)
except llm.LLMError as e: check(f"нет сервера -> '{e}'", "Нет связи" in str(e))

# ---- UI ----
from ui.context_panel import ContextPanel
app = QApplication([])
def spin(builder, limit=5.0):
    t = time.time()
    while (builder.models_req.busy or time.time() - t < 0.05) and time.time() - t < limit:
        app.processEvents(); time.sleep(0.01)
    app.processEvents()

panel = ContextPanel(); b = panel.builder; c = panel.connections; s = panel.model_selector
check("нет подключений: подсказка", "Connection" in b.fetch_status.text())
b.name.setText("x"); b.base_model.setCurrentText("m"); b.save_btn.click()
check("сохранение без подключения запрещено", "подключение" in b.status.text() and len(db.list_profiles()) == 1)

c.name.setText("lm"); c.url.setText(f"127.0.0.1:{port}"); c.api_key.setText("k1"); c.save_btn.click()
spin(b)
check("Builder увидел подключение", b.connection.count() == 1 and b.connection.currentText() == "lm")
check("модели подтянулись в список", [b.base_model.itemText(i) for i in range(b.base_model.count())]
      == ["bge-embed", "llama-3.1-8b", "qwen2.5-7b"])
check("статус: Моделей на сервере: 3", b.fetch_status.text() == "Моделей на сервере: 3")
check("ключ подключения ушёл на сервер", STATE["auth"] == "Bearer k1")

b.name.setText("coder"); b.base_model.setCurrentText("qwen2.5-7b"); b.save_btn.click()
prof = [r for r in db.list_profiles() if r["name"] == "coder"][0]
check("профиль сохранён с подключением", prof["connection"] == "lm" and prof["base_model"] == "qwen2.5-7b")
check("в сводке подключение", "Подключение: lm" in s.summary.text() and "qwen2.5-7b" in s.summary.text())

# ручной ввод модели, которой нет в списке, сохраняется
b.name.setText("manual"); b.base_model.setCurrentText("custom-model"); b.save_btn.click()
check("ручной ввод модели", [r for r in db.list_profiles() if r["name"] == "manual"][0]["base_model"] == "custom-model")

# переименование подключения тянет за собой профили
c.list.setCurrentRow(0); c.edit_btn.click(); c.name.setText("lm2"); c.save_btn.click(); spin(b)
check("переименование подключения обновило профили",
      all(r["connection"] == "lm2" for r in db.list_profiles() if r["name"] in ("coder", "manual")))
check("Builder показывает новое имя", b.connection.currentText() == "lm2")

# сервер недоступен
STATE["mode"] = "401"; b.refresh_btn.click(); spin(b)
check("ошибка видна пользователю", "отклонил ключ" in b.fetch_status.text())
STATE["mode"] = "ok"


# редактирование профиля
s.profile.setCurrentText("coder"); s.edit_btn.click(); spin(b)
check("загрузка профиля в Builder", b.name.text() == "coder" and b.connection.currentText() == "lm2"
      and b.base_model.currentText() == "qwen2.5-7b")

# удаление подключения
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
c.list.setCurrentRow(0); c.delete_btn.click(); spin(b)
s.profile.setCurrentText("coder")
check("после удаления подключения: пометка в сводке", "lm2 (удалено)" in s.summary.text())
check("Builder: нет подключений", b.connection.count() == 0 and "Connection" in b.fetch_status.text())
s.edit_btn.click(); spin(b)
check("загрузка профиля с удалённым подключением", b.connection.currentIndex() == -1 and "не найдено" in b.status.text())
b.save_btn.click()
check("сохранение без подключения запрещено (повторно)", "Сначала создай подключение" in b.status.text())

panel.shutdown()
srv.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")

