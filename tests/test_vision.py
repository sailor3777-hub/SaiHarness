"""vision_describe: ядро (core.vision) на MockTransport и инструмент через настоящий MCP-сервер toolbox + поддельный LM Studio
на локальном http-сервере; настройка профиля во вкладке «Инструменты»."""
import base64, http.server, json, pathlib, sqlite3, tempfile, threading
import httpx
from core import db, vision
from core.mcp_manager import McpManager, default_config
from core.tools import default_policy

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
proj = tmp / "proj"; proj.mkdir()
(proj / "pic.png").write_bytes(PNG); (proj / "note.txt").write_text("не картинка", encoding="utf-8")
db.add_root(str(proj))

# ---------- поддельный LM Studio ----------
seen = []
mode = {"kind": "ok"}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        seen.append(body)
        if mode["kind"] == "400":
            code, payload = 400, {"error": {"message": "Model does not support images"}}
        elif mode["kind"] == "empty":
            code, payload = 200, {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "думаю"}}]}
        else:
            code, payload = 200, {"choices": [{"finish_reason": "stop", "message": {"content": "Красный пиксель. Текст: ПРИВЕТ-42", "reasoning_content": "размышления"}}]}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/v1"

# ---------- настройка не задана ----------
db.connect().close()
try:
    vision.load_config(db.DB_PATH); check("без профиля — понятная ошибка", False)
except vision.VisionError as e:
    check("без профиля — понятная ошибка", "не настроена" in str(e), e)

db.save_connection("LM", "LM Studio", base, "key123")
db.save_profile("Глаза", "LM", "vl-model", "персонаж, который не должен попасть в запрос", 0.3, 777, "")
db.save_profile("БезПодключения", "", "x", "", 0.5, 100, "")
db.set_setting(vision.VISION_PROFILE_KEY, "Нет такого")
try: vision.load_config(db.DB_PATH); check("профиль удалён — ошибка", False)
except vision.VisionError as e: check("профиль удалён — ошибка", "не найден" in str(e), e)
db.set_setting(vision.VISION_PROFILE_KEY, "БезПодключения")
try: vision.load_config(db.DB_PATH); check("профиль без подключения — ошибка", False)
except vision.VisionError as e: check("профиль без подключения — ошибка", "нет подключения" in str(e), e)
db.set_setting(vision.VISION_PROFILE_KEY, "Глаза")
cfg = vision.load_config(db.DB_PATH)
check("конфиг из профиля и подключения", (cfg.base_url, cfg.api_key, cfg.model, cfg.temperature, cfg.max_tokens) == (base, "key123", "vl-model", 0.3, 777), cfg)

# ---------- ядро ----------
try: vision.describe(cfg, proj / "note.txt"); check("не картинка отклоняется", False)
except vision.VisionError as e: check("не картинка отклоняется", "не поддерживаемое" in str(e), e)
def handler(req):
    seen.append(json.loads(req.content)); seen.append(req.headers.get("authorization"))
    return httpx.Response(200, json={"choices": [{"message": {"content": " ответ "}}]})
seen.clear()
out = vision.describe(cfg, proj / "pic.png", "что на картинке?", transport=httpx.MockTransport(handler))
body = seen[0]; user = body["messages"][1]["content"]
check("ответ обрезан по краям", out == "ответ")
check("запрос: модель, лимиты, без стрима", body["model"] == "vl-model" and body["max_tokens"] == 777 and body["temperature"] == 0.3 and body["stream"] is False)
check("запрос: вопрос и картинка data-url", user[0] == {"type": "text", "text": "что на картинке?"}
      and user[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode())
check("запрос: свой системный промпт, не профиля", "глаза" in body["messages"][0]["content"] and "персонаж" not in json.dumps(body, ensure_ascii=False))
check("запрос: ключ API передан", seen[1] == "Bearer key123")
seen.clear(); vision.describe(cfg, proj / "pic.png", "  ", transport=httpx.MockTransport(handler))
check("пустой вопрос — общее описание", seen[0]["messages"][1]["content"][0]["text"] == vision.DEFAULT_QUESTION)

# ---------- инструмент через настоящий MCP-сервер ----------
m = McpManager(log_dir=tmp / "logs"); m.connect_all(default_config(db.DB_PATH))
by = {t.name: t for t in m.states["toolbox"].tools}
check("vision_describe есть в toolbox и выполняется без вопросов", "vision_describe" in by and default_policy(by["vision_describe"]) == "auto")
seen.clear()
txt, err = m.call("toolbox", "vision_describe", {"path": "pic.png", "question": "что тут?"}, 120)
check("инструмент: ответ vision-модели, без размышлений", not err and "ПРИВЕТ-42" in txt and "размышления" not in txt, txt)
check("инструмент: картинка и вопрос дошли до модели", seen and seen[0]["messages"][1]["content"][1]["type"] == "image_url"
      and seen[0]["messages"][1]["content"][0]["text"] == "что тут?")
txt, _ = m.call("toolbox", "vision_describe", {"path": "note.txt"}, 60)
check("инструмент: не картинка", "ОШИБКА" in txt and "не поддерживаемое" in txt, txt)
txt, _ = m.call("toolbox", "vision_describe", {"path": "нет.png"}, 60)
check("инструмент: файла нет", "ОШИБКА" in txt, txt)
txt, _ = m.call("toolbox", "vision_describe", {"path": str(tmp / "pic.png")}, 60)
check("инструмент: вне разрешённых папок", "ОШИБКА" in txt and "вне разрешённых" in txt, txt)
txt, _ = m.call("toolbox", "fs_read", {"path": "pic.png"}, 60)
check("fs_read картинки подсказывает vision_describe", "vision_describe" in txt, txt)
mode["kind"] = "400"
txt, _ = m.call("toolbox", "vision_describe", {"path": "pic.png"}, 60)
check("инструмент: модель без vision — ошибка с подсказкой", "ОШИБКА" in txt and "400" in txt and "поддержки изображений" in txt, txt)
mode["kind"] = "empty"
txt, _ = m.call("toolbox", "vision_describe", {"path": "pic.png"}, 60)
check("инструмент: пустой ответ из-за лимита токенов", "ОШИБКА" in txt and "лимитом токенов" in txt, txt)
db.set_setting(vision.VISION_PROFILE_KEY, "")
txt, _ = m.call("toolbox", "vision_describe", {"path": "pic.png"}, 60)
check("инструмент: профиль не выбран", "ОШИБКА" in txt and "не настроена" in txt, txt)
m.shutdown() if hasattr(m, "shutdown") else None

# ---------- вкладка «Инструменты» ----------
from PySide6.QtWidgets import QApplication
from ui.tools_panel import ToolsPanel
app = QApplication([])
panel = ToolsPanel(McpManager(log_dir=tmp / "logs2"), tmp / "mcp.json", auto_connect=False)
items = [panel.vision_combo.itemText(i) for i in range(panel.vision_combo.count())]
check("выбор профиля: список профилей и пункт «не выбран»", items[0].startswith("Вручную") and "Глаза" in items and panel.vision_combo.currentIndex() == 0, items)
panel.vision_combo.setCurrentIndex(items.index("Глаза")); panel._on_vision_chosen(0)
check("выбор профиля сохраняется в базе", db.get_setting(vision.VISION_PROFILE_KEY) == "Глаза")
db.delete_profile("Глаза"); panel.refresh_vision_profiles()
check("удалённый профиль виден как «не найден»", "не найден" in panel.vision_combo.currentText(), panel.vision_combo.currentText())
panel.vision_combo.setCurrentIndex(0); panel._on_vision_chosen(0)
check("сброс выбора", db.get_setting(vision.VISION_PROFILE_KEY) == "")

# ---------- ручные настройки ----------
check("вручную: поля видны, подсказка, что не задано", not panel.vision_url.isHidden() and "Задай адрес" in panel.vision_status.text())
panel.vision_url.setText(f"127.0.0.1:{srv.server_address[1]}"); panel.vision_key.setText("kk"); panel.vision_model.setText("hand-model")
panel._save_vision_manual()
check("вручную: адрес приведён к /v1 и всё сохранено", (db.get_setting(vision.VISION_URL_KEY), db.get_setting(vision.VISION_API_KEY_KEY),
      db.get_setting(vision.VISION_MODEL_KEY)) == (base, "kk", "hand-model") and panel.vision_status.isHidden(), db.get_setting(vision.VISION_URL_KEY))
panel.vision_url.setText("ftp://x"); panel._save_vision_manual()
check("вручную: плохой адрес отклонён", "не подходит" in panel.vision_status.text() and db.get_setting(vision.VISION_URL_KEY) == base)
cfg = vision.load_config(db.DB_PATH)
check("вручную: конфиг без профиля", (cfg.base_url, cfg.api_key, cfg.model, cfg.profile) == (base, "kk", "hand-model", "вручную"), cfg)
mode["kind"] = "ok"; seen.clear()
m2 = McpManager(log_dir=tmp / "logs3"); m2.connect_all(default_config(db.DB_PATH))
txt, err = m2.call("toolbox", "vision_describe", {"path": "pic.png"}, 120)
check("вручную: инструмент работает без профиля", not err and "ПРИВЕТ-42" in txt and seen[0]["model"] == "hand-model", txt)
db.set_setting(vision.VISION_PROFILE_KEY, "БезПодключения"); panel.refresh_vision_profiles()
check("выбран профиль: поля скрыты", panel.vision_url.isHidden())
from core.tools import TOOL_TIMEOUTS
check("vision_describe получает увеличенный таймаут вызова", TOOL_TIMEOUTS["toolbox.vision_describe"] > vision.TIMEOUT)

print("ALL OK" if ok_all else "SOME FAILED")
