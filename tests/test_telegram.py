"""Telegram bot offline: the Bot API client (mock transport), formatting, parsing, the polling service,
and the window: commands, the /chat list, a message going into the active chat and the reply coming back."""
import json, pathlib, sys, tempfile, threading, time
import httpx
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from core import db, llm, telegram
from core.telegram import BotApi, TelegramError

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

TOKEN = "123456:SECRET-token"

# ---------- the client ----------
seen = []
def handler(req):
    seen.append(req)
    method = req.url.path.rsplit("/", 1)[-1]
    if method == "getMe":
        return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "sai_bot"}})
    if method == "sendMessage":
        body = json.loads(req.content)
        if body.get("parse_mode") == "HTML" and "<b>broken" in body["text"]:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
    if method == "getFile":
        return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg"}})
    if req.url.path.startswith("/file/"):
        return httpx.Response(200, content=b"JPEGDATA")
    return httpx.Response(401, json={"ok": False, "description": f"Unauthorized for {TOKEN}"})
api = BotApi(TOKEN, transport=httpx.MockTransport(handler))
check("getMe", api.get_me()["username"] == "sai_bot")
check("the token is in the method URL", seen[-1].url.path == f"/bot{TOKEN}/getMe")
try:
    api.call("deleteWebhook"); check("an API error raises", False)
except TelegramError as e:
    check("an API error never shows the token", TOKEN not in str(e) and "Unauthorized" in str(e), str(e))
def down(req): raise httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getMe")
try:
    BotApi(TOKEN, transport=httpx.MockTransport(down)).get_me(); check("a network error raises", False)
except TelegramError as e:
    check("a network error never shows the token", TOKEN not in str(e) and "no connection" in str(e), str(e))
check("download: getFile, then the file URL", api.download("F1") == b"JPEGDATA" and seen[-1].url.path == f"/file/bot{TOKEN}/photos/a.jpg")
seen.clear()
telegram.send_reply(api, 42, "**bold** and `code`")
body = json.loads(seen[-1].content)
check("send_reply: HTML", body["parse_mode"] == "HTML" and body["text"] == "<b>bold</b> and <code>code</code>", body)
seen.clear()
orig = telegram.to_html
telegram.to_html = lambda md: "<b>broken"
telegram.send_reply(api, 42, "text")
telegram.to_html = orig
bodies = [json.loads(r.content) for r in seen]
check("send_reply: a part Telegram cannot parse goes again as plain text",
      bodies[-1].get("parse_mode") is None and bodies[-1]["text"] == "text" and bodies[-2].get("parse_mode") == "HTML", bodies)

# ---------- formatting ----------
h = telegram.to_html("# Title\n- one\n- two\n\n*it* and __b__ <tag> & [link](https://ex.com/a_b)\n\n```python\nx = 1 < 2\n```\nafter")
check("to_html: heading, bullets, italic, bold", "<b>Title</b>" in h and "• one" in h and "<i>it</i>" in h and "<b>b</b>" in h, h)
check("to_html: raw HTML is escaped", "&lt;tag&gt; &amp;" in h, h)
check("to_html: link, underscores in the URL stay", '<a href="https://ex.com/a_b">link</a>' in h, h)
check("to_html: code block with language, content escaped", '<pre><code class="language-python">x = 1 &lt; 2</code></pre>' in h, h)
check("to_html: no markup inside inline code", telegram.to_html("`**x**`") == "<code>**x**</code>")
check("to_html: snake_case words are not italic", telegram.to_html("a_b_c and 2*3*4") == "a_b_c and 2*3*4")
long = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
parts = telegram.split_text(long, 1000)
check("split_text: parts within the limit, nothing lost", all(len(p) <= 1000 for p in parts) and "\n".join(parts) == long, [len(p) for p in parts])
code = "intro\n```py\n" + "\n".join("print(%d)" % i for i in range(300)) + "\n```\nend"
parts = telegram.split_text(code, 800)
check("split_text: a cut code block is closed and reopened",
      len(parts) > 1 and all(p.count("```") % 2 == 0 for p in parts) and parts[1].startswith("```py"), [p[:12] for p in parts])
check("split_text: a very long line is cut", all(len(p) <= 100 for p in telegram.split_text("y" * 450, 100)))

# ---------- parsing ----------
now = 1_000_000
msg = {"update_id": 1, "message": {"message_id": 3, "date": now, "from": {"id": 7, "username": "sai"}, "chat": {"id": 7}, "text": "/chat@sai_bot"}}
p = telegram.parse_update(msg, now=now)
check("parse_update: message", p["kind"] == "message" and p["user_id"] == 7 and p["chat_id"] == 7 and not p["stale"] and not p["photo"], p)
check("command: /chat@bot", telegram.command(p["text"]) == ("chat", "") and telegram.command("hello") is None
      and telegram.command("/use 5") == ("use", "5"))
photo = {"update_id": 2, "message": {"message_id": 4, "date": now - 700, "from": {"id": 7}, "chat": {"id": 7}, "caption": "what is it",
                                     "photo": [{"file_id": "small"}, {"file_id": "big"}]}}
p = telegram.parse_update(photo, now=now)
check("parse_update: the largest photo, caption as text, old = stale", p["photo"] == "big" and p["text"] == "what is it" and p["stale"], p)
cb = {"update_id": 3, "callback_query": {"id": "q1", "from": {"id": 7}, "data": "chat:5", "message": {"message_id": 9, "chat": {"id": 7}}}}
p = telegram.parse_update(cb)
check("parse_update: callback", p == {"kind": "callback", "user_id": 7, "username": "", "chat_id": 7, "message_id": 9, "callback_id": "q1", "data": "chat:5"}, p)
check("parse_update: a sticker is ignored", telegram.parse_update({"update_id": 4, "message": {"date": now, "from": {"id": 7}, "chat": {"id": 7}, "sticker": {}}}) is None)
check("parse_users", telegram.parse_users("123, 456 x 789;") == {123, 456, 789} and telegram.parse_users("") == set())
check("chat map round trip, junk ignored", telegram.load_chat_map(telegram.dump_chat_map({7: 3})) == {7: 3}
      and telegram.load_chat_map("{bad") == {} and telegram.load_chat_map('{"a": 1, "8": 2}') == {8: 2})
chats = [{"id": i, "title": f"Chat {i}" + ("y" * 60 if i == 12 else "")} for i in range(20, 0, -1)]
text, kb = telegram.chats_keyboard(chats, 0, 18)
rows = kb["inline_keyboard"]
check("keyboard: a page of chats, active marked", len(rows) == 8 + 2 and rows[2][0]["text"] == "✓ Chat 18" and rows[0][0]["callback_data"] == "chat:20", rows[:3])
check("keyboard: arrows and New chat", [b["callback_data"] for b in rows[8]] == ["page:0", "page:1"] and rows[9][0]["callback_data"] == "new")
check("keyboard: text names the active chat", text.startswith("Active chat: Chat 18"), text)
_, kb = telegram.chats_keyboard(chats, 2, None)
check("keyboard: last page, only the back arrow", [r[0]["text"] for r in kb["inline_keyboard"][:-2]] == ["Chat 4", "Chat 3", "Chat 2", "Chat 1"]
      and [b["callback_data"] for b in kb["inline_keyboard"][-2]] == ["page:1", "page:2"])
_, kb = telegram.chats_keyboard(chats, 1, None)
titles = [r[0]["text"] for r in kb["inline_keyboard"][:-2]]
check("keyboard: long titles cut", any(t.endswith("…") and len(t) == 40 for t in titles), titles)
check("keyboard: empty list", telegram.chats_keyboard([], 0, None)[0] == "There are no chats yet.")

# ---------- the polling service ----------
app = QApplication([])
def wait(cond, timeout=20.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()

from worker import TelegramService
batches = [[
    {"update_id": 10, "message": {"message_id": 1, "date": time.time(), "from": {"id": 99, "username": "stranger"}, "chat": {"id": 99}, "text": "hi"}},
    {"update_id": 11, "message": {"message_id": 2, "date": time.time(), "from": {"id": 7}, "chat": {"id": 7}, "text": "hello"}},
    {"update_id": 12, "message": {"message_id": 3, "date": time.time() - 3600, "from": {"id": 7}, "chat": {"id": 7}, "text": "old"}},
    {"update_id": 13, "message": {"message_id": 4, "date": time.time(), "from": {"id": 7}, "chat": {"id": 7}, "photo": [{"file_id": "P"}]}},
]]
offsets = []
def poll_handler(req):
    method = req.url.path.rsplit("/", 1)[-1]
    if method == "getUpdates":
        offsets.append(json.loads(req.content)["offset"])
        if batches:
            return httpx.Response(200, json={"ok": True, "result": batches.pop(0)})
        time.sleep(0.05)
        return httpx.Response(200, json={"ok": True, "result": []})
    if method == "setMyCommands":
        return httpx.Response(200, json={"ok": True, "result": True})
    return handler(req)
svc = TelegramService(api_factory=lambda token: BotApi(token, transport=httpx.MockTransport(poll_handler)))
got, logs, states = [], [], []
svc.received.connect(got.append); svc.log.connect(logs.append); svc.state_changed.connect(lambda r, t: states.append((r, t)))
svc.start("  ")
check("service: an empty token does not start", not svc.running and states[-1] == (False, "Enter the bot token"))
svc.allowed = {7}
svc.start(TOKEN)
wait(lambda: len(got) >= 2)
check("service: running as the bot", svc.running and (True, "Running as @sai_bot") in states, states)
check("service: only the allowed user's messages pass", [g["text"] for g in got] == ["hello", ""], got)
check("service: a stranger is logged with the id", any("99" in l and "@stranger" in l for l in logs), logs)
check("service: an old message is skipped", any("old message" in l for l in logs), logs)
check("service: a picture is downloaded", got[1].get("image") == b"JPEGDATA")
wait(lambda: offsets and offsets[-1] == 14)
check("service: the offset confirms handled updates", offsets[0] == 0 and offsets[-1] == 14, offsets)
done = threading.Event()
svc.post(lambda a: a.send_message(7, "queued") and done.set())
check("service: post runs in the sending thread", done.wait(5))
svc.stop()
check("service: stopped", not svc.running and states[-1] == (False, "Stopped"))
bad = TelegramService(api_factory=lambda token: BotApi(token, transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))))
bstates = []; bad.state_changed.connect(lambda r, t: bstates.append((r, t)))
bad.start(TOKEN); wait(lambda: bstates and not bstates[-1][0])
check("service: a wrong token stops with the reason", bstates[-1] == (False, "Could not start: Unauthorized") and not bad.running, bstates)

# ---------- the window ----------
tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else tmp / "tg.db"
db.save_connection("lm", "LM Studio", "http://127.0.0.1:1/v1", "")
db.save_profile("p", "lm", "fake", "", 0.7, 200, "")
db.set_setting(telegram.TOOLS_KEY, json.dumps(["toolbox.calc", "toolbox.nope"]))

def sse(*c): return ("".join(f"data: {json.dumps(x, ensure_ascii=False)}\n\n" for x in c) + "data: [DONE]\n\n").encode()
class LM:
    steps = []; requests = []
    def handler(self, r): self.requests.append(json.loads(r.content)); return httpx.Response(200, content=sse(*self.steps.pop(0)))
lm = LM()
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))

from ui.main_window import MainWindow
w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; panel = w.context.telegram_panel; bridge = w.telegram
wait(lambda: tp.states and not tp.connect_req.busy, 40)
w.selector.profile.setCurrentText("p"); w.selector.user_changed.emit("p")
check("tab: the tool tree shows the servers and the saved set", panel.tree.topLevelItemCount() > 0 and "toolbox.calc" in panel.tree.checked)

class FakeApi:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a, k))
fake = FakeApi()
panel.service.post = lambda fn: fn(fake)   # the window's sends are run right here
def calls(name): return [c for c in fake.calls if c[0] == name]
def texts(): return [c[1][1] for c in fake.calls if c[0] == "send_message"]
def message(text, image=None):
    item = {"kind": "message", "user_id": 7, "username": "", "chat_id": 7, "text": text, "photo": "F" if image is not None else "", "stale": False}
    if image is not None: item["image"] = image
    bridge.on_received(item)
def callback(data):
    bridge.on_received({"kind": "callback", "user_id": 7, "username": "", "chat_id": 7, "message_id": 50, "callback_id": "q", "data": data})

message("/chat")
check("/chat with no chats", texts()[-1] == "There are no chats yet." and calls("send_message")[-1][2]["markup"]["inline_keyboard"][-1][0]["callback_data"] == "new")
message("/stop"); check("/stop with nothing running", texts()[-1] == "Nothing is running.")
message("/tools"); check("/tools without an active chat shows the new-chat set", "A new chat will get" in texts()[-1] and "toolbox.calc" in texts()[-1], texts()[-1])
message("/help"); check("/help", texts()[-1] == telegram.HELP)

lm.steps = [[{"choices": [{"delta": {"content": "**Hi** from the model"}}]}]]
message("hello from the phone")
cid = bridge.active_chat(7)
check("a message starts a new chat and opens it", cid is not None and w.chat_id == cid)
check("the new chat got the Telegram tool set, unknown tools skipped", db.load_chat_tools(cid) == {"toolbox.calc"}, db.load_chat_tools(cid))
check("the skipped tool is in the log", "toolbox.nope" in panel.log_view.toPlainText())
check("typing is shown", calls("send_action") and bridge.typing.isActive())
wait(lambda: w.stream is None)
rows = db.list_messages(cid)
check("the exchange is saved in the chat", [r["role"] for r in rows] == ["user", "assistant"] and rows[0]["content"] == "hello from the phone", [tuple(r) for r in rows])
check("the chat got its title", db.get_chat(cid)["title"] == "hello from the phone")
check("the reply went to Telegram as HTML", texts()[-1] == "<b>Hi</b> from the model" and calls("send_message")[-1][2].get("html_mode"), fake.calls[-1])
check("typing stops", not bridge.typing.isActive())
check("the input field is left alone", w.chat_view.input.toPlainText() == "")
check("the active chat is saved", telegram.load_chat_map(db.get_setting(telegram.CHATS_KEY)) == {7: cid})

# a picture
buf = QBuffer(); buf.open(QIODevice.WriteOnly); img = QImage(40, 30, QImage.Format_RGB32); img.fill(0x3366cc); img.save(buf, "PNG")
lm.steps = [[{"choices": [{"delta": {"content": "a blue square"}}]}]]
message("what is this", bytes(buf.data()))
wait(lambda: w.stream is None)
rows = db.list_messages(cid)
check("a picture from Telegram is attached to the message", rows[-2]["images"] and json.loads(rows[-2]["images"])[0].endswith(".jpg"), rows[-2]["images"])
check("the model got the picture", any(isinstance(m["content"], list) for m in lm.requests[-1]["messages"]))
message("", b"not an image"); check("an unreadable picture without text", texts()[-1] == "Could not read the picture.")

# busy
w.stream = {"chat_id": cid}
message("again"); check("busy: a second message is refused", texts()[-1].startswith("Busy: a reply is being generated in"), texts()[-1])
w.stream = None

# the list and switching
other = db.create_chat("Another chat")
message("/chat")
kb = calls("send_message")[-1][2]["markup"]["inline_keyboard"]
check("/chat lists the chats, the active one marked", any(b[0]["text"] == "✓ hello from the phone" for b in kb) and any(b[0]["callback_data"] == f"chat:{other}" for b in kb), kb)
callback(f"chat:{other}")
check("choosing a chat: answered and the message edited", calls("answer_callback") and calls("edit_message")[-1][1][2].startswith("Active chat: Another chat"))
check("the active chat changed", bridge.active_chat(7) == other)
callback(f"chat:{cid}")
check("the preview shows the last exchange", "Model: a blue square" in calls("edit_message")[-1][1][2], calls("edit_message")[-1])
callback("page:0"); check("page: the list again", calls("edit_message")[-1][2]["markup"] is not None)
callback("new")
check("New chat from the list", bridge.active_chat(7) not in (cid, other) and calls("edit_message")[-1][1][2].startswith("Started a new chat"))
db.delete_chat(bridge.active_chat(7))
check("a deleted active chat is forgotten", bridge.active_chat(7) is None)

# errors go back to Telegram
lm.steps = []
def boom(r): return httpx.Response(500, json={"error": "model crashed"})
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(boom)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
message("this will fail")
wait(lambda: w.stream is None)
check("a server error is sent as a note", texts()[-1].startswith("⚠ "), texts()[-1])

# confirmations from Telegram: "ask" runs, "deny" stays
tool = next(t for t in tp.tools if t.name == "notes_write")
check("Telegram policy: ask -> auto", tp.policy_for(tool) == "ask" and w._telegram_policy(tool) == "auto")
tp.set_policy(tool.id, "deny")
check("Telegram policy: deny stays", w._telegram_policy(tool) == "deny")
tp.set_policy(tool.id, None)
check("the window keeps asking", tp.effective_policy(tool) == "ask")

panel.tree.set_checked({"toolbox.calc"})
check("tab: tool_ids", panel.tool_ids() == {"toolbox.calc"})
w.close()
print("ALL OK" if ok_all else "SOME FAILED")
