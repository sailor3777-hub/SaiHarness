"""Изображения в чате: хранение, сборка запроса, оценка токенов, ввод (кнопка, вставка, перетаскивание), миниатюры
в истории, отправка модели с vision (data-URL) и без (описание от vision-модели). Модель чата подменена, vision-сервер
поддельный локальный http-сервер; LM Studio не нужен."""
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

# ---------- ядро ----------
p1, p2, p3 = (str(make_png(f"i{n}.png")) for n in range(3))
check("пути картинок кодируются туда и обратно", images.decode_paths(images.encode_paths([p1, p2])) == [p1, p2] and images.decode_paths("мусор") == [] and images.encode_paths([]) == "")
check("user_content: без картинок строка, с картинками части", images.user_content("привет", []) == "привет"
      and [x["type"] for x in images.user_content("привет", [p1])] == ["text", "image_url"]
      and [x["type"] for x in images.user_content("", [p1])] == ["image_url"])
msg = [{"role": "user", "content": images.user_content("что это?", [p1])}]
check("заглушка узнаётся", images.has_placeholders(msg) and images.placeholder_path(msg[0]["content"][1]) == p1 and not images.has_placeholders([{"role": "user", "content": "a"}]))
sent = images.to_data_urls(msg)
url = sent[0]["content"][1]["image_url"]["url"]
check("to_data_urls: настоящий data-URL, исходные сообщения целы", url.startswith("data:image/png;base64,") and base64.b64decode(url.split(",", 1)[1]) == pathlib.Path(p1).read_bytes()
      and images.placeholder_path(msg[0]["content"][1]) == p1)
desc = images.to_descriptions(msg, lambda path: "красный прямоугольник")
check("to_descriptions: одна строка с описанием", isinstance(desc[0]["content"], str) and "красный прямоугольник" in desc[0]["content"] and "что это?" in desc[0]["content"] and "i0.png" in desc[0]["content"], desc)
check("оценка: картинка считается фиксированно, а не по длине адреса", estimate_request_tokens(msg) - estimate_request_tokens([{"role": "user", "content": "что это?"}]) == images.IMAGE_TOKENS)
check("оценка: две картинки", estimate_request_tokens([{"role": "user", "content": images.user_content("", [p1, p2])}]) >= 2 * images.IMAGE_TOKENS)

# ---------- БД и история ----------
db.connect().close()
chat = db.create_chat()
db.add_message(chat, "user", "первая", images=images.encode_paths([p1]))
db.add_message(chat, "assistant", "вижу красное")
db.add_message(chat, "user", "вторая", images=images.encode_paths([p2]))
db.add_message(chat, "assistant", "ок")
db.add_message(chat, "user", "", images=images.encode_paths([p3]))
db.add_message(chat, "user", "простой текст")
rows = db.list_messages(chat)
check("колонка images сохраняется", images.decode_paths(rows[0]["images"]) == [p1] and rows[1]["images"] == "")
hist = history_messages(rows)
check("история: старая картинка не уходит, остаётся пометка", isinstance(hist[0]["content"], str) and "i0.png" in hist[0]["content"] and "не включены" in hist[0]["content"], hist[0])
check("история: две последние картинки уходят", images.count_images(hist[2]["content"]) == 1 and images.count_images(hist[4]["content"]) == 1, hist)
check("история: сообщение только с картинкой", [x["type"] for x in hist[4]["content"]] == ["image_url"])
check("история: обычный текст остаётся строкой", hist[5] == {"role": "user", "content": "простой текст"})
pathlib.Path(p3).unlink()
check("история: файл исчез — пометка вместо падения", "не включены" in history_messages(db.list_messages(chat))[4]["content"])
make_png("i2.png")

# ---------- тип модели ----------
real_get = httpx.get
httpx.get = lambda url, **k: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [
    {"id": "eyes", "type": "vlm"}, {"id": "blind", "type": "llm"}]}))).get(url)
check("model_type: vlm / llm / неизвестная / сбой", (llm.model_type("http://x/v1", "", "eyes"), llm.model_type("http://x/v1", "", "blind"),
      llm.model_type("http://x/v1", "", "нет")) == ("vlm", "llm", None))
httpx.get = lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("нет"))
check("model_type: сервер недоступен — None", llm.model_type("http://x/v1", "", "eyes") is None)
httpx.get = real_get

# ---------- поддельный vision-сервер ----------
vision_seen = []
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        vision_seen.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
        data = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "на картинке красный прямоугольник"}}]}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
cfg = vision.VisionConfig(base, "", "vl", 0.3, 100, "t")
vision_seen.clear()
a = vision.describe_cached(cfg, pathlib.Path(p1)); b = vision.describe_cached(cfg, pathlib.Path(p1))
check("describe_cached: второй раз из кеша рядом с файлом", a == b and len(vision_seen) == 1 and pathlib.Path(p1 + ".txt").exists(), len(vision_seen))
vision.describe_cached(cfg, pathlib.Path(p1), "а что ещё?")
check("describe_cached: вопрос всегда идёт модели", len(vision_seen) == 2)

# ---------- ChatView ----------
from ui.chat_view import ChatView
v = ChatView(); v.resize(700, 600); v.show()
big = QImage(3000, 1000, QImage.Format_RGB32); big.fill(QColor("blue"))
v.attach_image(big)
saved = QImage(v.pending[0])
check("крупная картинка уменьшена и сохранена в data/images", max(saved.width(), saved.height()) == images.MAX_SIDE and pathlib.Path(v.pending[0]).parent == images.IMAGES_DIR, saved.size())
check("превью показано", v.preview.isVisible() and v.preview_row.count() >= 2)
mime = QMimeData(); mime.setImageData(QImage(40, 40, QImage.Format_RGB32))
v.input.insertFromMimeData(mime)
mime2 = QMimeData(); mime2.setUrls([QUrl.fromLocalFile(p2), QUrl.fromLocalFile(str(tmp / "note.txt"))])
v.input.insertFromMimeData(mime2)
check("вставка картинки и перетаскивание файла-картинки (текстовый файл игнорируется)", len(v.pending) == 3 and v.input.toPlainText() == "", len(v.pending))
v.attach_files([p1, p3]); v.attach_files([p1])
check("не больше 4 картинок в сообщении", len(v.pending) == images.MAX_IMAGES_PER_MESSAGE and "до 4" in v.status.text(), (len(v.pending), v.status.text()))
v.remove_pending(v.pending[0])
check("картинку можно убрать", len(v.pending) == 3)
got = []; v.message_submitted.connect(got.append)
v.send_btn.click()
check("отправка возможна и без текста, если есть картинка", got == [""], got)
v.clear_pending(); v.send_btn.click()
check("совсем пустое сообщение не отправляется", got == [""] and not v.preview.isVisible())

v.set_messages([{"role": "user", "content": "смотри", "images": images.encode_paths([p1])}])
html = v.history.document().toHtml()
h_with = v.history.document().size().height()
v.set_messages([{"role": "user", "content": "смотри", "images": ""}])
h_without = v.history.document().size().height()
check("в истории миниатюра: картинка в разметке и заметно увеличивает документ", "<img" in html and h_with - h_without > 20, (h_with, h_without))
v.set_messages([{"role": "user", "content": "смотри", "images": images.encode_paths([str(tmp / "нет.png")])}])
check("удалённый файл: подпись вместо картинки", "не найдено" in v.history.toPlainText())

# ---------- сквозной сценарий: окно, модель с vision и без ----------
db.save_connection("lm", "LM Studio", base, "")
db.save_profile("eyes-prof", "lm", "eyes", "", 0.5, 300, "")
db.save_profile("blind-prof", "lm", "blind", "", 0.5, 300, "")
db.set_setting(vision.VISION_PROFILE_KEY, "")
db.set_setting(vision.VISION_URL_KEY, base); db.set_setting(vision.VISION_MODEL_KEY, "vl")

def sse(*chunks): return ("".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
chat_requests = []
def chat_handler(req):
    chat_requests.append(json.loads(req.content))
    return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "вижу"}}]}))
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
check("модель с vision: ответ получен", send("eyes-prof", "что на фото?", [p1]))
user = chat_requests[0]["messages"][-1]["content"]
check("vision-модели ушли текст и data-URL картинки", user[0] == {"type": "text", "text": "что на фото?"} and user[1]["image_url"]["url"].startswith("data:image/png;base64,"), str(user)[:200])
rows = db.list_messages(w.chat_id)
check("в БД картинка записана за сообщением, превью очищено", images.decode_paths(rows[0]["images"]) == [p1] and not w.chat_view.pending and not w.chat_view.preview.isVisible())
check("в чате у сообщения есть миниатюра", "<img" in w.chat_view.history.document().toHtml())
check("окно «Запрос» видит заглушку, а не мегабайты base64", images.PLACEHOLDER_PREFIX in json.dumps(w.last_requests[w.chat_id]["messages"]) and "base64" not in json.dumps(w.last_requests[w.chat_id]["messages"]))

w.new_chat(); chat_requests.clear(); vision_seen.clear()
pathlib.Path(p2 + ".txt").unlink(missing_ok=True)
check("модель без зрения: ответ получен", send("blind-prof", "что на фото?", [p2]))
user = chat_requests[0]["messages"][-1]["content"]
check("слепой модели ушло описание от vision-модели, а не картинка", isinstance(user, str) and "красный прямоугольник" in user and "что на фото?" in user and "image_url" not in json.dumps(chat_requests[0]), user)
check("vision-модель получила картинку и общий вопрос", len(vision_seen) == 1 and vision_seen[0]["model"] == "vl"
      and vision_seen[0]["messages"][1]["content"][1]["type"] == "image_url")
check("в статусе есть пометка об описании", "описываю" in w.chat_view.status.text(), w.chat_view.status.text())

w.new_chat(); chat_requests.clear()
db.set_setting(vision.VISION_URL_KEY, "")
check("слепая модель без настроенного vision: ошибка, а не молчание", send("blind-prof", "а теперь?", [p2]))
sysrows = [r for r in db.list_messages(w.chat_id) if r["role"] == "system"]
check("понятная ошибка в чате, запрос модели не ушёл", sysrows and "не видит изображений" in sysrows[-1]["content"] and not chat_requests, [r["content"] for r in sysrows])

llm.httpx.stream = real_stream
print("ALL OK" if ok_all else "SOME FAILED")
