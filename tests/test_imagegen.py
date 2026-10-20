"""image_generate: ядро (core.imagegen) на MockTransport, инструмент через настоящий MCP-сервер toolbox + поддельный ComfyUI
на локальном http-сервере; поле адреса во вкладке «Инструменты»."""
import base64, copy, http.server, json, os, pathlib, tempfile, threading, urllib.parse
import httpx
from core import db, imagegen, images
from core.mcp_manager import McpManager, default_config
from core.tools import TOOL_TIMEOUTS, default_policy

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
images.IMAGES_DIR = tmp / "images"
os.environ["SAI_IMAGES_DIR"] = str(tmp / "images")  # подпроцесс MCP-сервера тоже пишет во временную папку

# workflow формы Z-Image Turbo из Open WebUI: текст идёт через CLIPTextEncode, negative из ConditioningZeroOut
WF = {
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "z", "images": ["57:8", 0]}},
    "57:30": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors"}},
    "57:33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["57:27", 0]}},
    "57:8": {"class_type": "VAEDecode", "inputs": {"samples": ["57:3", 0], "vae": ["57:29", 0]}},
    "57:27": {"class_type": "CLIPTextEncode", "inputs": {"text": "старый текст", "clip": ["57:30", 0]}},
    "57:13": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 200, "height": 200, "batch_size": 1}},
    "57:11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3.0, "model": ["57:28", 0]}},
    "57:3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 8, "cfg": 1.0, "sampler_name": "res_multistep",
             "model": ["57:11", 0], "positive": ["57:27", 0], "negative": ["57:33", 0], "latent_image": ["57:13", 0]}},
}
wf_path = tmp / "wf.json"; wf_path.write_text(json.dumps(WF), encoding="utf-8")
os.environ["SAI_COMFY_WORKFLOW"] = str(wf_path)

# ---------- адрес ----------
check("адрес: схема и слэш", imagegen.normalize_url(" 192.168.1.5:8188/ ") == "http://192.168.1.5:8188"
      and imagegen.normalize_url("https://h.lan:1/") == "https://h.lan:1")
for bad in ("", "ftp://x"):
    try: imagegen.normalize_url(bad); check(f"адрес {bad!r} отклонён", False)
    except ValueError: check(f"адрес {bad!r} отклонён", True)

# ---------- workflow ----------
def err_of(fn, *a, **k):
    try: fn(*a, **k); return ""
    except imagegen.ImageGenError as e: return str(e)
check("workflow читается", imagegen.load_workflow(wf_path) == WF)
check("нет файла workflow", "Нет файла" in err_of(imagegen.load_workflow, tmp / "нет.json"))
(tmp / "ui.json").write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
check("формат интерфейса (не API) отклонён", "API" in err_of(imagegen.load_workflow, tmp / "ui.json"))
(tmp / "broken.json").write_text("{", encoding="utf-8")
check("битый JSON", "не читается" in err_of(imagegen.load_workflow, tmp / "broken.json"))

ids = imagegen.find_nodes(WF)
check("узлы найдены по связям", ids == {"sampler": "57:3", "text": "57:27", "latent": "57:13"}, ids)
flux = copy.deepcopy(WF); flux["57:20"] = {"class_type": "FluxGuidance", "inputs": {"conditioning": ["57:27", 0], "guidance": 3.5}}
flux["57:3"]["inputs"]["positive"] = ["57:20", 0]
check("текстовый узел находится через FluxGuidance", imagegen.find_nodes(flux)["text"] == "57:27")
no_sampler = {k: v for k, v in WF.items() if k != "57:3"}
check("нет KSampler", "KSampler" in err_of(imagegen.find_nodes, no_sampler))
no_text = copy.deepcopy(WF); no_text["57:3"]["inputs"]["positive"] = ["57:30", 0]
check("нет текстового узла", "CLIPTextEncode" in err_of(imagegen.find_nodes, no_text))
no_latent = copy.deepcopy(WF); no_latent["57:13"]["inputs"] = {"batch_size": 1}
check("нет латента с размером", "латент" in err_of(imagegen.find_nodes, no_latent))

# ---------- размер и подстановка ----------
check("размер: кратно 8, границы", imagegen.clamp_size(500, 70) == (496, 64) and imagegen.clamp_size(9999, 9999) == (1024, 1024)
      and imagegen.clamp_size(512, 512) == (512, 512), (imagegen.clamp_size(500, 70), imagegen.clamp_size(9999, 9999)))
check("размер от модели сохраняется: 200×200 и 640×384", imagegen.clamp_size(200, 200) == (200, 200) and imagegen.clamp_size(640, 384) == (640, 384))
check("размер: пропорции при превышении площади", imagegen.clamp_size(1536, 1024) == (1248, 832), imagegen.clamp_size(1536, 1024))
before = copy.deepcopy(WF)
p = imagegen.build_prompt(WF, "  кот  ", 640, 384, steps=12, seed=7)
check("подстановка: текст, размер, шаги, seed", p["57:27"]["inputs"]["text"] == "кот" and (p["57:13"]["inputs"]["width"], p["57:13"]["inputs"]["height"]) == (640, 384)
      and p["57:3"]["inputs"]["steps"] == 12 and p["57:3"]["inputs"]["seed"] == 7, p["57:3"])
check("подстановка: исходный workflow не тронут", WF == before)
p2 = imagegen.build_prompt(WF, "кот")
check("шаги из workflow, seed случайный и разный", p2["57:3"]["inputs"]["steps"] == 8
      and p2["57:3"]["inputs"]["seed"] != imagegen.build_prompt(WF, "кот")["57:3"]["inputs"]["seed"])
adv = copy.deepcopy(WF); adv["57:3"]["class_type"] = "KSamplerAdvanced"; adv["57:3"]["inputs"]["noise_seed"] = 0
check("KSamplerAdvanced: noise_seed", imagegen.build_prompt(adv, "x", seed=5)["57:3"]["inputs"]["noise_seed"] == 5)
check("пустой prompt", "пуст" in err_of(imagegen.build_prompt, WF, "  "))

# ---------- ядро на MockTransport ----------
cfg = imagegen.ImageGenConfig("http://comfy.lan:8188", WF)
state = {"polls": 0, "history": "ok", "prompt_status": 200, "seen": []}
def handler(req):
    path = req.url.path
    if path == "/prompt":
        state["seen"].append(json.loads(req.content))
        if state["prompt_status"] == 400:
            return httpx.Response(400, json={"error": {"message": "Prompt outputs failed validation"},
                                             "node_errors": {"57:28": {"class_type": "UNETLoader", "errors": [{"message": "Value not in list", "details": "unet_name: 'x'"}]}}})
        return httpx.Response(200, json={"prompt_id": "abc"})
    if path == "/history/abc":
        state["polls"] += 1
        if state["history"] == "slow" or state["polls"] < 3:
            return httpx.Response(200, json={})
        if state["history"] == "error":
            return httpx.Response(200, json={"abc": {"status": {"status_str": "error", "messages": [
                ["execution_error", {"node_type": "KSampler", "exception_message": "CUDA out of memory"}]]}, "outputs": {}}})
        if state["history"] == "noimg":
            return httpx.Response(200, json={"abc": {"status": {"status_str": "success"}, "outputs": {"9": {"text": ["x"]}}}})
        return httpx.Response(200, json={"abc": {"status": {"status_str": "success"}, "outputs": {
            "9": {"images": [{"filename": "z_0001.png", "subfolder": "sub", "type": "output"}]}}}})
    if path == "/view":
        state["seen"].append(dict(req.url.params))
        return httpx.Response(200, content=PNG)
    return httpx.Response(404)
T = httpx.MockTransport(handler)
out = imagegen.generate(cfg, "кот в шляпе", 512, 512, seed=3, transport=T, poll=0.001)
check("generate: файл сохранён в data/images (временной)", out.parent == images.IMAGES_DIR and out.read_bytes() == PNG and out.suffix == ".png", out)
check("generate: ждал историю, пока пусто", state["polls"] == 3, state["polls"])
check("generate: в /prompt ушёл workflow с текстом", state["seen"][0]["prompt"]["57:27"]["inputs"]["text"] == "кот в шляпе" and "client_id" in state["seen"][0])
check("generate: /view с именем, папкой и типом", state["seen"][1] == {"filename": "z_0001.png", "subfolder": "sub", "type": "output"}, state["seen"][1])

state.update(prompt_status=400)
e = err_of(imagegen.generate, cfg, "x", transport=T, poll=0.001)
check("generate: ComfyUI не принял workflow — узел и причина", "не принял" in e and "57:28" in e and "Value not in list" in e, e)
state.update(prompt_status=200, polls=0, history="error")
e = err_of(imagegen.generate, cfg, "x", transport=T, poll=0.001)
check("generate: ошибка выполнения с подсказкой про видеопамять", "CUDA out of memory" in e and "видеопамят" in e, e)
state.update(polls=0, history="noimg")
check("generate: нет картинки", "не отдал картинку" in err_of(imagegen.generate, cfg, "x", transport=T, poll=0.001))
state.update(polls=0, history="slow")
check("generate: таймаут ожидания", "не ответил" in err_of(imagegen.generate, cfg, "x", transport=T, timeout=0.05, poll=0.01))
def refuse(req): raise httpx.ConnectError("нет")
check("generate: ComfyUI выключен", "выключен" in err_of(imagegen.generate, cfg, "x", transport=httpx.MockTransport(refuse)))

# ---------- настройка не задана ----------
db.connect().close()
check("без адреса — понятная ошибка", "не настроена" in err_of(imagegen.load_config, db.DB_PATH))

# ---------- поддельный ComfyUI и настоящий MCP-сервер ----------
seen = []
class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, payload=None, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        seen.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
        self._send(200, {"prompt_id": "p1"})
    def do_GET(self):
        if self.path.startswith("/history/p1"):
            self._send(200, {"p1": {"status": {"status_str": "success"}, "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}}})
        elif self.path.startswith("/view"):
            self._send(200, raw=PNG)
        else:
            self._send(404, {})
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]

m = McpManager(log_dir=tmp / "logs"); m.connect_all(default_config(db.DB_PATH))
by = {t.name: t for t in m.states["toolbox"].tools}
check("image_generate есть в toolbox и спрашивает подтверждение", "image_generate" in by and default_policy(by["image_generate"]) == "ask")
check("image_generate получает увеличенный таймаут вызова", TOOL_TIMEOUTS["toolbox.image_generate"] > imagegen.TOTAL_TIMEOUT)
txt, _ = m.call("toolbox", "image_generate", {"prompt": "кот"}, 120)
check("инструмент: адрес не задан", "ОШИБКА" in txt and "не настроена" in txt, txt)
db.set_setting(imagegen.COMFY_URL_KEY, f"http://127.0.0.1:{port}")
m2 = McpManager(log_dir=tmp / "logs2"); m2.connect_all(default_config(db.DB_PATH))
txt, err = m2.call("toolbox", "image_generate", {"prompt": "red cat", "width": 640, "height": 384, "steps": 4}, 120)
txt_ok = txt
saved = pathlib.Path(imagegen.result_path(txt))
check("инструмент: путь к файлу в ответе, файл записан", not err and saved.parent == tmp / "images" and saved.read_bytes() == PNG, txt)
sent = seen[-1]["prompt"]
check("инструмент: workflow дошёл с текстом, размером и шагами", sent["57:27"]["inputs"]["text"] == "red cat" and sent["57:13"]["inputs"]["width"] == 640
      and sent["57:13"]["inputs"]["height"] == 384 and sent["57:3"]["inputs"]["steps"] == 4, sent["57:3"])
txt, _ = m2.call("toolbox", "image_generate", {"prompt": " "}, 60)
check("инструмент: пустой prompt", "ОШИБКА" in txt and "пуст" in txt, txt)
srv.shutdown(); srv.server_close()
txt, _ = m2.call("toolbox", "image_generate", {"prompt": "кот"}, 60)
check("инструмент: сервер выключен", "ОШИБКА" in txt and "выключен" in txt, txt)

# ---------- вкладка «Инструменты» ----------
from PySide6.QtWidgets import QApplication
from ui.tools_panel import ToolsPanel
app = QApplication([])
db.set_setting(imagegen.COMFY_URL_KEY, "")
panel = ToolsPanel(McpManager(log_dir=tmp / "logs3"), tmp / "mcp.json", auto_connect=False)
check("панель: адрес не задан — подсказка", "Задай адрес" in panel.comfy_status.text() and not panel.comfy_status.isHidden() or "Задай адрес" in panel.comfy_status.text())
panel.comfy_url.setText("192.168.1.5:8188"); panel._save_comfy()
check("панель: адрес приведён и сохранён, подсказки нет", db.get_setting(imagegen.COMFY_URL_KEY) == "http://192.168.1.5:8188" and panel.comfy_status.text() == "", panel.comfy_status.text())
panel.comfy_url.setText("ftp://x"); panel._save_comfy()
check("панель: плохой адрес отклонён", "не подходит" in panel.comfy_status.text() and db.get_setting(imagegen.COMFY_URL_KEY) == "http://192.168.1.5:8188")
os.environ["SAI_COMFY_WORKFLOW"] = str(tmp / "нет.json"); panel.comfy_url.setText("192.168.1.5:8188"); panel._save_comfy()
check("панель: нет workflow — подсказка", "Нет файла" in panel.comfy_status.text(), panel.comfy_status.text())

# ---------- показ картинки в чате ----------
from ui.chat_view import ChatView
view = ChatView()
html = view._block_html({"role": "tool", "content": txt_ok, "tool_name": "toolbox.image_generate"})
check("чат: под результатом image_generate есть картинка", f'<img src="{saved.as_uri()}"' in html and "width=\"320\"" in html, html[-300:])
html = view._block_html({"role": "tool", "content": "ОШИБКА: ComfyUI выключен", "tool_name": "toolbox.image_generate"})
check("чат: у ошибки картинки нет", "<img" not in html)
html = view._block_html({"role": "tool", "content": "см. текст: " + txt_ok, "tool_name": "toolbox.web_extract"})
check("чат: путь внутри чужого текста картинку не открывает", "<img" not in html)
html = view._block_html({"role": "tool", "content": imagegen.RESULT_PREFIX + str(tmp / "нет.png"), "tool_name": "x"})
check("чат: файл исчез — подпись вместо картинки", "не найдено" in html)

print("ALL OK" if ok_all else "SOME FAILED")
