"""Images in the chat: storage, request building, token estimate, input (button, paste, drag and drop), thumbnails
in the history, sending to a model with vision (data URL) and without (a description from the vision model). The chat model is
faked, the vision server is a fake local http server; LM Studio is not needed."""
import base64, http.server, json, os, pathlib, tempfile, threading, time
import httpx
from PySide6.QtCore import QMimeData, QUrl, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from core import db, images, llm, vision
from core.context_builder import build_messages, estimate_request_tokens, history_messages

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
images.IMAGES_DIR = tmp / "images"
app = QApplication([])
def wait(cond, timeout=40.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()

def make_png(name, w=64, h=48, color="red"):
    img = QImage(w, h, QImage.Format_RGB32); img.fill(QColor(color))
    path = tmp / name; img.save(str(path)); return path

# ---------- core ----------
p1, p2, p3 = (str(make_png(f"i{n}.png")) for n in range(3))
check("picture paths are encoded there and back", images.decode_paths(images.encode_paths([p1, p2])) == [p1, p2] and images.decode_paths("garbage") == [] and images.encode_paths([]) == "")
check("user_content: a string without pictures, parts with them", images.user_content("hello", []) == "hello"
      and [x["type"] for x in images.user_content("hello", [p1])] == ["text", "image_url"]
      and [x["type"] for x in images.user_content("", [p1])] == ["image_url"])
msg = [{"role": "user", "content": images.user_content("what is this?", [p1])}]
check("a placeholder is recognized", images.has_placeholders(msg) and images.placeholder_path(msg[0]["content"][1]) == p1 and not images.has_placeholders([{"role": "user", "content": "a"}]))
sent = images.to_data_urls(msg)
url = sent[0]["content"][1]["image_url"]["url"]
check("to_data_urls: a real data URL, the original messages are intact", url.startswith("data:image/png;base64,") and base64.b64decode(url.split(",", 1)[1]) == pathlib.Path(p1).read_bytes()
      and images.placeholder_path(msg[0]["content"][1]) == p1)
desc = images.to_descriptions(msg, lambda path: "a red rectangle")
check("to_descriptions: one string with the description", isinstance(desc[0]["content"], str) and "a red rectangle" in desc[0]["content"] and "what is this?" in desc[0]["content"] and "i0.png" in desc[0]["content"], desc)
check("estimate: a picture counts as a fixed amount, not by the address length", estimate_request_tokens(msg) - estimate_request_tokens([{"role": "user", "content": "what is this?"}]) == images.IMAGE_TOKENS)
check("estimate: two pictures", estimate_request_tokens([{"role": "user", "content": images.user_content("", [p1, p2])}]) >= 2 * images.IMAGE_TOKENS)

# ---------- DB and history ----------
db.connect().close()
chat = db.create_chat()
db.add_message(chat, "user", "first", images=images.encode_paths([p1]))
db.add_message(chat, "assistant", "I see red")
db.add_message(chat, "user", "second", images=images.encode_paths([p2]))
db.add_message(chat, "assistant", "ok")
db.add_message(chat, "user", "", images=images.encode_paths([p3]))
db.add_message(chat, "user", "plain text")
rows = db.list_messages(chat)
check("the images column is saved", images.decode_paths(rows[0]["images"]) == [p1] and rows[1]["images"] == "")
hist = history_messages(rows)
check("history: an old picture is not sent, a note remains", isinstance(hist[0]["content"], str) and "i0.png" in hist[0]["content"] and "not included" in hist[0]["content"], hist[0])
check("history: the two latest pictures are sent", images.count_images(hist[2]["content"]) == 1 and images.count_images(hist[4]["content"]) == 1, hist)
check("history: a message with only a picture", [x["type"] for x in hist[4]["content"]] == ["image_url"])
check("history: plain text stays a string", hist[5] == {"role": "user", "content": "plain text"})
pathlib.Path(p3).unlink()
check("history: the file vanished, a note instead of a crash", "not included" in history_messages(db.list_messages(chat))[4]["content"])
make_png("i2.png")

# ---------- model type ----------
real_get = httpx.get
httpx.get = lambda url, **k: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [
    {"id": "eyes", "type": "vlm"}, {"id": "blind", "type": "llm"}]}))).get(url)
check("model_type: vlm / llm / unknown / failure", (llm.model_type("http://x/v1", "", "eyes"), llm.model_type("http://x/v1", "", "blind"),
      llm.model_type("http://x/v1", "", "none")) == ("vlm", "llm", None))
httpx.get = lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no"))
check("model_type: server unavailable gives None", llm.model_type("http://x/v1", "", "eyes") is None)
httpx.get = real_get

# ---------- a fake vision server ----------
vision_seen = []
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        vision_seen.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
        data = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "the picture shows a red rectangle"}}]}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
cfg = vision.VisionConfig(base, "", "vl", 0.3, 100, "t")
vision_seen.clear()
a = vision.describe_cached(cfg, pathlib.Path(p1)); b = vision.describe_cached(cfg, pathlib.Path(p1))
check("describe_cached: the second time from the cache next to the file", a == b and len(vision_seen) == 1 and pathlib.Path(p1 + ".txt").exists(), len(vision_seen))
vision.describe_cached(cfg, pathlib.Path(p1), "what else?")
check("describe_cached: a question always goes to the model", len(vision_seen) == 2)

# ---------- ChatView ----------
from ui.chat_view import ChatView
v = ChatView(); v.resize(700, 600); v.show()
big = QImage(3000, 1000, QImage.Format_RGB32); big.fill(QColor("blue"))
v.attach_image(big)
saved = QImage(v.pending[0])
check("a large picture is shrunk and saved to data/images", max(saved.width(), saved.height()) == images.MAX_SIDE and pathlib.Path(v.pending[0]).parent == images.IMAGES_DIR, saved.size())
check("a preview is shown", v.preview.isVisible() and v.preview_row.count() >= 2)
mime = QMimeData(); mime.setImageData(QImage(40, 40, QImage.Format_RGB32))
v.input.insertFromMimeData(mime)
mime2 = QMimeData(); mime2.setUrls([QUrl.fromLocalFile(p2), QUrl.fromLocalFile(str(tmp / "note.txt"))])
v.input.insertFromMimeData(mime2)
check("pasting a picture and dragging a picture file (a text file is ignored)", len(v.pending) == 3 and v.input.toPlainText() == "", len(v.pending))
v.attach_files([p1, p3]); v.attach_files([p1])
check("at most 4 pictures per message", len(v.pending) == images.MAX_IMAGES_PER_MESSAGE and "up to 4" in v.status.text(), (len(v.pending), v.status.text()))
v.remove_pending(v.pending[0])
check("a picture can be removed", len(v.pending) == 3)
got = []; v.message_submitted.connect(got.append)
v.send_btn.click()
check("sending is possible without text if there is a picture", got == [""], got)
v.clear_pending(); v.send_btn.click()
check("a completely empty message is not sent", got == [""] and not v.preview.isVisible())

v.set_messages([{"role": "user", "content": "look", "images": images.encode_paths([p1])}])
html = v.history.document().toHtml()
h_with = v.history.document().size().height()
v.set_messages([{"role": "user", "content": "look", "images": ""}])
h_without = v.history.document().size().height()
check("a thumbnail in the history: the picture is in the markup and makes the document noticeably taller", "<img" in html and h_with - h_without > 20, (h_with, h_without))
v.set_messages([{"role": "user", "content": "look", "images": images.encode_paths([str(tmp / "missing.png")])}])
check("a deleted file: a caption instead of the picture", "not found" in v.history.toPlainText())

# ---------- end-to-end: the window, a model with vision and without ----------
db.save_connection("lm", "LM Studio", base, "")
db.save_profile("eyes-prof", "lm", "eyes", "", 0.5, 300, "")
db.save_profile("blind-prof", "lm", "blind", "", 0.5, 300, "")
db.set_setting(vision.VISION_PROFILE_KEY, "")
db.set_setting(vision.VISION_URL_KEY, base); db.set_setting(vision.VISION_MODEL_KEY, "vl")

def sse(*chunks): return ("".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
chat_requests = []
def chat_handler(req):
    chat_requests.append(json.loads(req.content))
    return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "I see"}}]}))
real_stream = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(chat_handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
kinds = {"eyes": "vlm", "blind": "llm"}
llm.model_type = lambda url, key, model, timeout=4.0: kinds.get(model)

from ui.main_window import MainWindow
w = MainWindow(mcp_config=tmp / "mcp.json", connect_mcp=False)
def send(profile, text, paths):
    w.selector.select(profile)
    w.chat_view.pending = list(paths); w.chat_view.input.setPlainText(text); w.chat_view.send_btn.click()
    return wait(lambda: w.stream is None, 60)

chat_requests.clear()
check("a model with vision: the answer arrived", send("eyes-prof", "what is in the photo?", [p1]))
user = chat_requests[0]["messages"][-1]["content"]
check("the vision model got the text and the picture's data URL", user[0] == {"type": "text", "text": "what is in the photo?"} and user[1]["image_url"]["url"].startswith("data:image/png;base64,"), str(user)[:200])
rows = db.list_messages(w.chat_id)
check("in the DB the picture is recorded with the message, the preview is cleared", images.decode_paths(rows[0]["images"]) == [p1] and not w.chat_view.pending and not w.chat_view.preview.isVisible())
check("the message has a thumbnail in the chat", "<img" in w.chat_view.history.document().toHtml())
check("the Request window sees the placeholder, not megabytes of base64", images.PLACEHOLDER_PREFIX in json.dumps(w.last_requests[w.chat_id]["messages"]) and "base64" not in json.dumps(w.last_requests[w.chat_id]["messages"]))

w.new_chat(); chat_requests.clear(); vision_seen.clear()
pathlib.Path(p2 + ".txt").unlink(missing_ok=True)
check("a model without vision: the answer arrived", send("blind-prof", "what is in the photo?", [p2]))
user = chat_requests[0]["messages"][-1]["content"]
check("the blind model got a description from the vision model, not the picture", isinstance(user, str) and "red rectangle" in user and "what is in the photo?" in user and "image_url" not in json.dumps(chat_requests[0]), user)
check("the vision model got the picture and a general question", len(vision_seen) == 1 and vision_seen[0]["model"] == "vl"
      and vision_seen[0]["messages"][1]["content"][1]["type"] == "image_url")
check("the status mentions the description", "describing" in w.chat_view.status.text(), w.chat_view.status.text())

w.new_chat(); chat_requests.clear()
db.set_setting(vision.VISION_URL_KEY, "")
check("a blind model without vision set up: an error, not silence", send("blind-prof", "and now?", [p2]))
sysrows = [r for r in db.list_messages(w.chat_id) if r["role"] == "system"]
check("a clear error in the chat, no request to the model", sysrows and "cannot see images" in sysrows[-1]["content"] and not chat_requests, [r["content"] for r in sysrows])

llm.httpx.stream = real_stream
print("ALL OK" if ok_all else "SOME FAILED")
