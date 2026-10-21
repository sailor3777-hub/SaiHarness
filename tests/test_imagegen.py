"""image_generate: the core (core.imagegen) on MockTransport, the tool through the real toolbox MCP server + a fake ComfyUI
on a local http server; the address field on the Tools tab."""
import base64, copy, http.server, json, os, pathlib, tempfile, threading, urllib.parse
import httpx
from core import db, imagegen, images
from core.mcp_manager import default_config
from model_view import McpManager  # errors with the prefix, as the model sees them
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
os.environ["SAI_IMAGES_DIR"] = str(tmp / "images")  # the MCP server subprocess writes to the temporary folder too

# a workflow shaped like Z-Image Turbo from Open WebUI: text goes through CLIPTextEncode, negative from ConditioningZeroOut
WF = {
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "z", "images": ["57:8", 0]}},
    "57:30": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors"}},
    "57:33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["57:27", 0]}},
    "57:8": {"class_type": "VAEDecode", "inputs": {"samples": ["57:3", 0], "vae": ["57:29", 0]}},
    "57:27": {"class_type": "CLIPTextEncode", "inputs": {"text": "old text", "clip": ["57:30", 0]}},
    "57:13": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 200, "height": 200, "batch_size": 1}},
    "57:11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3.0, "model": ["57:28", 0]}},
    "57:3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 8, "cfg": 1.0, "sampler_name": "res_multistep",
             "model": ["57:11", 0], "positive": ["57:27", 0], "negative": ["57:33", 0], "latent_image": ["57:13", 0]}},
}
wf_path = tmp / "wf.json"; wf_path.write_text(json.dumps(WF), encoding="utf-8")
os.environ["SAI_COMFY_WORKFLOW"] = str(wf_path)

# ---------- address ----------
check("address: scheme and slash", imagegen.normalize_url(" 192.168.1.5:8188/ ") == "http://192.168.1.5:8188"
      and imagegen.normalize_url("https://h.lan:1/") == "https://h.lan:1")
for bad in ("", "ftp://x"):
    try: imagegen.normalize_url(bad); check(f"address {bad!r} refused", False)
    except ValueError: check(f"address {bad!r} refused", True)

# ---------- workflow ----------
def err_of(fn, *a, **k):
    try: fn(*a, **k); return ""
    except imagegen.ImageGenError as e: return str(e)
check("the workflow is read", imagegen.load_workflow(wf_path) == WF)
check("no workflow file", "No workflow file" in err_of(imagegen.load_workflow, tmp / "missing.json"))
(tmp / "ui.json").write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
check("the UI format (not API) is refused", "API" in err_of(imagegen.load_workflow, tmp / "ui.json"))
(tmp / "broken.json").write_text("{", encoding="utf-8")
check("broken JSON", "cannot be read" in err_of(imagegen.load_workflow, tmp / "broken.json"))

ids = imagegen.find_nodes(WF)
check("nodes found by links", ids == {"sampler": "57:3", "text": "57:27", "latent": "57:13"}, ids)
flux = copy.deepcopy(WF); flux["57:20"] = {"class_type": "FluxGuidance", "inputs": {"conditioning": ["57:27", 0], "guidance": 3.5}}
flux["57:3"]["inputs"]["positive"] = ["57:20", 0]
check("the text node is found through FluxGuidance", imagegen.find_nodes(flux)["text"] == "57:27")
no_sampler = {k: v for k, v in WF.items() if k != "57:3"}
check("no KSampler", "KSampler" in err_of(imagegen.find_nodes, no_sampler))
no_text = copy.deepcopy(WF); no_text["57:3"]["inputs"]["positive"] = ["57:30", 0]
check("no text node", "CLIPTextEncode" in err_of(imagegen.find_nodes, no_text))
no_latent = copy.deepcopy(WF); no_latent["57:13"]["inputs"] = {"batch_size": 1}
check("no latent with a size", "latent" in err_of(imagegen.find_nodes, no_latent))

# ---------- size and substitution ----------
check("size: multiple of 8, bounds", imagegen.clamp_size(500, 70) == (496, 64) and imagegen.clamp_size(9999, 9999) == (1024, 1024)
      and imagegen.clamp_size(512, 512) == (512, 512), (imagegen.clamp_size(500, 70), imagegen.clamp_size(9999, 9999)))
check("a size from the model is kept: 200×200 and 640×384", imagegen.clamp_size(200, 200) == (200, 200) and imagegen.clamp_size(640, 384) == (640, 384))
check("size: proportions when the area is exceeded", imagegen.clamp_size(1536, 1024) == (1248, 832), imagegen.clamp_size(1536, 1024))
before = copy.deepcopy(WF)
p = imagegen.build_prompt(WF, "  cat  ", 640, 384, steps=12, seed=7)
check("substitution: text, size, steps, seed", p["57:27"]["inputs"]["text"] == "cat" and (p["57:13"]["inputs"]["width"], p["57:13"]["inputs"]["height"]) == (640, 384)
      and p["57:3"]["inputs"]["steps"] == 12 and p["57:3"]["inputs"]["seed"] == 7, p["57:3"])
check("substitution: the original workflow is untouched", WF == before)
p2 = imagegen.build_prompt(WF, "cat")
check("steps from the workflow, the seed is random and different", p2["57:3"]["inputs"]["steps"] == 8
      and p2["57:3"]["inputs"]["seed"] != imagegen.build_prompt(WF, "cat")["57:3"]["inputs"]["seed"])
adv = copy.deepcopy(WF); adv["57:3"]["class_type"] = "KSamplerAdvanced"; adv["57:3"]["inputs"]["noise_seed"] = 0
check("KSamplerAdvanced: noise_seed", imagegen.build_prompt(adv, "x", seed=5)["57:3"]["inputs"]["noise_seed"] == 5)
check("empty prompt", "empty" in err_of(imagegen.build_prompt, WF, "  "))

# ---------- the core on MockTransport ----------
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
out = imagegen.generate(cfg, "a cat in a hat", 512, 512, seed=3, transport=T, poll=0.001)
check("generate: the file is saved to (temporary) data/images", out.parent == images.IMAGES_DIR and out.read_bytes() == PNG and out.suffix == ".png", out)
check("generate: waited for the history while it was empty", state["polls"] == 3, state["polls"])
check("generate: the workflow with the text went to /prompt", state["seen"][0]["prompt"]["57:27"]["inputs"]["text"] == "a cat in a hat" and "client_id" in state["seen"][0])
check("generate: /view with name, folder and type", state["seen"][1] == {"filename": "z_0001.png", "subfolder": "sub", "type": "output"}, state["seen"][1])

state.update(prompt_status=400)
e = err_of(imagegen.generate, cfg, "x", transport=T, poll=0.001)
check("generate: ComfyUI did not accept the workflow, node and reason", "did not accept" in e and "57:28" in e and "Value not in list" in e, e)
state.update(prompt_status=200, polls=0, history="error")
e = err_of(imagegen.generate, cfg, "x", transport=T, poll=0.001)
check("generate: an execution error with a hint about video memory", "CUDA out of memory" in e and "video memory" in e, e)
state.update(polls=0, history="noimg")
check("generate: no picture", "returned no picture" in err_of(imagegen.generate, cfg, "x", transport=T, poll=0.001))
state.update(polls=0, history="slow")
check("generate: waiting timeout", "did not answer" in err_of(imagegen.generate, cfg, "x", transport=T, timeout=0.05, poll=0.01))
def refuse(req): raise httpx.ConnectError("no")
check("generate: ComfyUI is off", "turned off" in err_of(imagegen.generate, cfg, "x", transport=httpx.MockTransport(refuse)))

# ---------- not configured ----------
db.connect().close()
check("without an address: a clear error", "not set up" in err_of(imagegen.load_config, db.DB_PATH))

# ---------- a fake ComfyUI and the real MCP server ----------
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
check("image_generate is in toolbox and asks for confirmation", "image_generate" in by and default_policy(by["image_generate"]) == "ask")
check("image_generate gets a longer call timeout", TOOL_TIMEOUTS["toolbox.image_generate"] > imagegen.TOTAL_TIMEOUT)
txt, _ = m.call("toolbox", "image_generate", {"prompt": "cat"}, 120)
check("tool: the address is not set", "ERROR" in txt and "not set up" in txt, txt)
db.set_setting(imagegen.COMFY_URL_KEY, f"http://127.0.0.1:{port}")
m2 = McpManager(log_dir=tmp / "logs2"); m2.connect_all(default_config(db.DB_PATH))
txt, err = m2.call("toolbox", "image_generate", {"prompt": "red cat", "width": 640, "height": 384, "steps": 4}, 120)
txt_ok = txt
saved = pathlib.Path(imagegen.result_path(txt))
check("tool: the file path in the reply, the file is written", not err and saved.parent == tmp / "images" and saved.read_bytes() == PNG, txt)
sent = seen[-1]["prompt"]
check("tool: the workflow arrived with the text, size and steps", sent["57:27"]["inputs"]["text"] == "red cat" and sent["57:13"]["inputs"]["width"] == 640
      and sent["57:13"]["inputs"]["height"] == 384 and sent["57:3"]["inputs"]["steps"] == 4, sent["57:3"])
txt, _ = m2.call("toolbox", "image_generate", {"prompt": " "}, 60)
check("tool: empty prompt", "ERROR" in txt and "empty" in txt, txt)
srv.shutdown(); srv.server_close()
txt, _ = m2.call("toolbox", "image_generate", {"prompt": "cat"}, 60)
check("tool: the server is off", "ERROR" in txt and "turned off" in txt, txt)

# ---------- the Tools tab ----------
from PySide6.QtWidgets import QApplication
from ui.tools_panel import ToolsPanel
app = QApplication([])
db.set_setting(imagegen.COMFY_URL_KEY, "")
panel = ToolsPanel(McpManager(log_dir=tmp / "logs3"), tmp / "mcp.json", auto_connect=False)
check("panel: the address is not set, a hint", "Enter the ComfyUI address" in panel.comfy_status.text() and not panel.comfy_status.isHidden() or "Enter the ComfyUI address" in panel.comfy_status.text())
panel.comfy_url.setText("192.168.1.5:8188"); panel._save_comfy()
check("panel: the address is normalized and saved, no hint", db.get_setting(imagegen.COMFY_URL_KEY) == "http://192.168.1.5:8188" and panel.comfy_status.text() == "", panel.comfy_status.text())
panel.comfy_url.setText("ftp://x"); panel._save_comfy()
check("panel: a bad address is refused", "Invalid address" in panel.comfy_status.text() and db.get_setting(imagegen.COMFY_URL_KEY) == "http://192.168.1.5:8188")
os.environ["SAI_COMFY_WORKFLOW"] = str(tmp / "missing.json"); panel.comfy_url.setText("192.168.1.5:8188"); panel._save_comfy()
check("panel: no workflow, a hint", "No workflow file" in panel.comfy_status.text(), panel.comfy_status.text())

# ---------- showing the picture in the chat ----------
from ui.chat_view import ChatView
view = ChatView()
html = view._block_html({"role": "tool", "content": txt_ok, "tool_name": "toolbox.image_generate"})
check("chat: there is a picture under the image_generate result", f'<img src="{saved.as_uri()}"' in html and "width=\"320\"" in html, html[-300:])
html = view._block_html({"role": "tool", "content": "ERROR: ComfyUI is turned off", "tool_name": "toolbox.image_generate"})
check("chat: an error has no picture", "<img" not in html)
html = view._block_html({"role": "tool", "content": "see the text: " + txt_ok, "tool_name": "toolbox.web_extract"})
check("chat: a path inside someone else's text does not open a picture", "<img" not in html)
html = view._block_html({"role": "tool", "content": imagegen.RESULT_PREFIX + str(tmp / "missing.png"), "tool_name": "x"})
check("chat: the file is gone, a caption instead of the picture", "not found" in html)
html = view._block_html({"role": "tool", "content": "Изображение готово и сохранено: " + str(saved), "tool_name": "toolbox.image_generate"})
check("chat: a result from the Russian version (old history) still shows the picture", f'<img src="{saved.as_uri()}"' in html, html[-300:])

print("ALL OK" if ok_all else "SOME FAILED")
