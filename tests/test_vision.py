"""vision_describe: the core (core.vision) on MockTransport and the tool through the real toolbox MCP server + a fake LM Studio
on a local http server; the profile setting on the Tools tab."""
import base64, http.server, json, pathlib, sqlite3, tempfile, threading
import httpx
from core import db, vision
from core.mcp_manager import default_config
from model_view import McpManager  # errors with the prefix, as the model sees them
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
(proj / "pic.png").write_bytes(PNG); (proj / "note.txt").write_text("not a picture", encoding="utf-8")
db.add_root(str(proj))

# ---------- a fake LM Studio ----------
seen = []
mode = {"kind": "ok"}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        seen.append(body)
        if mode["kind"] == "400":
            code, payload = 400, {"error": {"message": "Model does not support images"}}
        elif mode["kind"] == "empty":
            code, payload = 200, {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "thinking"}}]}
        else:
            code, payload = 200, {"choices": [{"finish_reason": "stop", "message": {"content": "A red pixel. Text: HELLO-42", "reasoning_content": "my private reasoning"}}]}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/v1"

# ---------- not configured ----------
db.connect().close()
try:
    vision.load_config(db.DB_PATH); check("no profile: a clear error", False)
except vision.VisionError as e:
    check("no profile: a clear error", "not set up" in str(e), e)

db.save_connection("LM", "LM Studio", base, "key123")
db.save_profile("Eyes", "LM", "vl-model", "a persona that must not get into the request", 0.3, 777, "")
db.save_profile("NoConnection", "", "x", "", 0.5, 100, "")
db.set_setting(vision.VISION_PROFILE_KEY, "No such profile")
try: vision.load_config(db.DB_PATH); check("deleted profile: an error", False)
except vision.VisionError as e: check("deleted profile: an error", "not found" in str(e), e)
db.set_setting(vision.VISION_PROFILE_KEY, "NoConnection")
try: vision.load_config(db.DB_PATH); check("a profile without a connection: an error", False)
except vision.VisionError as e: check("a profile without a connection: an error", "has no connection" in str(e), e)
db.set_setting(vision.VISION_PROFILE_KEY, "Eyes")
cfg = vision.load_config(db.DB_PATH)
check("config from the profile and the connection", (cfg.base_url, cfg.api_key, cfg.model, cfg.temperature, cfg.max_tokens) == (base, "key123", "vl-model", 0.3, 777), cfg)

# ---------- core ----------
try: vision.describe(cfg, proj / "note.txt"); check("not a picture is refused", False)
except vision.VisionError as e: check("not a picture is refused", "Not a supported" in str(e), e)
def handler(req):
    seen.append(json.loads(req.content)); seen.append(req.headers.get("authorization"))
    return httpx.Response(200, json={"choices": [{"message": {"content": " answer "}}]})
seen.clear()
out = vision.describe(cfg, proj / "pic.png", "what is in the picture?", transport=httpx.MockTransport(handler))
body = seen[0]; user = body["messages"][1]["content"]
check("the answer is trimmed", out == "answer")
check("request: model, limits, no streaming", body["model"] == "vl-model" and body["max_tokens"] == 777 and body["temperature"] == 0.3 and body["stream"] is False)
check("request: the question and the picture as a data URL", user[0] == {"type": "text", "text": "what is in the picture?"}
      and user[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode())
check("request: its own system prompt, not the profile's", "eyes" in body["messages"][0]["content"] and "persona" not in json.dumps(body, ensure_ascii=False))
check("request: the API key is passed", seen[1] == "Bearer key123")
seen.clear(); vision.describe(cfg, proj / "pic.png", "  ", transport=httpx.MockTransport(handler))
check("an empty question gives a general description", seen[0]["messages"][1]["content"][0]["text"] == vision.DEFAULT_QUESTION)

# ---------- the tool through the real MCP server ----------
m = McpManager(log_dir=tmp / "logs"); m.connect_all(default_config(db.DB_PATH))
by = {t.name: t for t in m.states["toolbox"].tools}
check("vision_describe is in toolbox and runs without asking", "vision_describe" in by and default_policy(by["vision_describe"]) == "auto")
seen.clear()
txt, err = m.call("toolbox", "vision_describe", {"path": "pic.png", "question": "what is here?"}, 120)
check("tool: the vision model's answer, without reasoning", not err and "HELLO-42" in txt and "my private reasoning" not in txt, txt)
check("tool: the picture and the question reached the model", seen and seen[0]["messages"][1]["content"][1]["type"] == "image_url"
      and seen[0]["messages"][1]["content"][0]["text"] == "what is here?")
txt, _ = m.call("toolbox", "vision_describe", {"path": "note.txt"}, 60)
check("tool: not a picture", "ERROR" in txt and "Not a supported" in txt, txt)
txt, _ = m.call("toolbox", "vision_describe", {"path": "missing.png"}, 60)
check("tool: no file", "ERROR" in txt, txt)
txt, _ = m.call("toolbox", "vision_describe", {"path": str(tmp / "pic.png")}, 60)
check("tool: outside the allowed folders", "ERROR" in txt and "outside the allowed" in txt, txt)
txt, _ = m.call("toolbox", "fs_read", {"path": "pic.png"}, 60)
check("fs_read of a picture suggests vision_describe", "vision_describe" in txt, txt)
mode["kind"] = "400"
txt, _ = m.call("toolbox", "vision_describe", {"path": "pic.png"}, 60)
check("tool: a model without vision, an error with a hint", "ERROR" in txt and "400" in txt and "support images" in txt, txt)
mode["kind"] = "empty"
txt, _ = m.call("toolbox", "vision_describe", {"path": "pic.png"}, 60)
check("tool: an empty reply because of the token limit", "ERROR" in txt and "token limit" in txt, txt)
db.set_setting(vision.VISION_PROFILE_KEY, "")
txt, _ = m.call("toolbox", "vision_describe", {"path": "pic.png"}, 60)
check("tool: no profile chosen", "ERROR" in txt and "not set up" in txt, txt)
m.shutdown() if hasattr(m, "shutdown") else None

# ---------- the Tools tab ----------
from PySide6.QtWidgets import QApplication
from ui.tools_panel import ToolsPanel
app = QApplication([])
panel = ToolsPanel(McpManager(log_dir=tmp / "logs2"), tmp / "mcp.json", auto_connect=False)
items = [panel.vision_combo.itemText(i) for i in range(panel.vision_combo.count())]
check("profile choice: the profile list and the \"not chosen\" item", items[0].startswith("Manual") and "Eyes" in items and panel.vision_combo.currentIndex() == 0, items)
panel.vision_combo.setCurrentIndex(items.index("Eyes")); panel._on_vision_chosen(0)
check("the profile choice is saved in the database", db.get_setting(vision.VISION_PROFILE_KEY) == "Eyes")
db.delete_profile("Eyes"); panel.refresh_vision_profiles()
check("a deleted profile is shown as \"not found\"", "not found" in panel.vision_combo.currentText(), panel.vision_combo.currentText())
panel.vision_combo.setCurrentIndex(0); panel._on_vision_chosen(0)
check("resetting the choice", db.get_setting(vision.VISION_PROFILE_KEY) == "")

# ---------- manual settings ----------
check("manual: the fields are visible, a hint that nothing is set", not panel.vision_url.isHidden() and "Enter the address" in panel.vision_status.text())
panel.vision_url.setText(f"127.0.0.1:{srv.server_address[1]}"); panel.vision_key.setText("kk"); panel.vision_model.setText("hand-model")
panel._save_vision_manual()
check("manual: the address is brought to /v1 and everything is saved", (db.get_setting(vision.VISION_URL_KEY), db.get_setting(vision.VISION_API_KEY_KEY),
      db.get_setting(vision.VISION_MODEL_KEY)) == (base, "kk", "hand-model") and panel.vision_status.isHidden(), db.get_setting(vision.VISION_URL_KEY))
panel.vision_url.setText("ftp://x"); panel._save_vision_manual()
check("manual: a bad address is refused", "Invalid address" in panel.vision_status.text() and db.get_setting(vision.VISION_URL_KEY) == base)
cfg = vision.load_config(db.DB_PATH)
check("manual: config without a profile", (cfg.base_url, cfg.api_key, cfg.model, cfg.profile) == (base, "kk", "hand-model", "manual"), cfg)
mode["kind"] = "ok"; seen.clear()
m2 = McpManager(log_dir=tmp / "logs3"); m2.connect_all(default_config(db.DB_PATH))
txt, err = m2.call("toolbox", "vision_describe", {"path": "pic.png"}, 120)
check("manual: the tool works without a profile", not err and "HELLO-42" in txt and seen[0]["model"] == "hand-model", txt)
db.set_setting(vision.VISION_PROFILE_KEY, "NoConnection"); panel.refresh_vision_profiles()
check("a profile is chosen: the fields are hidden", panel.vision_url.isHidden())
from core.tools import TOOL_TIMEOUTS
check("vision_describe gets a longer call timeout", TOOL_TIMEOUTS["toolbox.vision_describe"] > vision.TIMEOUT)

print("ALL OK" if ok_all else "SOME FAILED")
