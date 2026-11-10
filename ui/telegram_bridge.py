"""Telegram ↔ chats: commands, the /chat list, and messages sent into the active chat the same way as from the window.

Each Telegram chat has its own active harness chat (CHATS_KEY). A message opens that chat in the window and goes through
MainWindow.submit with telegram=<Telegram chat id>; the reply comes back through finished() and photo().
"""
from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtGui import QImage

from core import db, images, telegram
from core.telegram import send_reply

TYPING_MS = 4500   # Telegram shows "typing…" for about 5 s
PREVIEW_CHARS = 300


def save_image(data: bytes) -> str:
    """A picture from Telegram -> a file in data/images (a large one is shrunk); empty if it cannot be read."""
    image = QImage.fromData(data)
    if image.isNull():
        return ""
    if max(image.width(), image.height()) > images.MAX_SIDE:
        image = image.scaled(images.MAX_SIDE, images.MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    path = images.new_image_path(".jpg")
    return str(path) if image.save(str(path), "JPG", 90) else ""


def _short(text: str, limit: int = PREVIEW_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class TelegramBridge(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.panel = window.context.telegram_panel
        self.service = self.panel.service
        self.active = telegram.load_chat_map(db.get_setting(telegram.CHATS_KEY))
        self.typing = QTimer(self)
        self.typing.setInterval(TYPING_MS)
        self.typing.timeout.connect(self._typing)
        self._typing_chat: int | None = None
        self.service.received.connect(self.on_received)

    # --- sending ---

    def say(self, tg: int, text: str, markup: dict | None = None):
        self.service.post(lambda api: api.send_message(tg, text, markup=markup))

    def finished(self, tg: int, content: str, note: str):
        """The reply to a Telegram message is done: send its text and the error/stop note, if any."""
        self._stop_typing()
        content = (content or "").strip()
        if content:
            self.service.post(lambda api: send_reply(api, tg, content))
        if note:
            self.say(tg, f"⚠ {note}")

    def photo(self, tg: int, path: str):
        self.service.post(lambda api: api.send_photo(tg, path))

    def _typing(self):
        s = self.w.stream
        tg = self._typing_chat
        if tg is None or not s or s.get("telegram") != tg:
            self._stop_typing()
            return
        self.service.post(lambda api: api.send_action(tg))

    def _stop_typing(self):
        self.typing.stop()
        self._typing_chat = None

    # --- chats ---

    def active_chat(self, tg: int) -> int | None:
        chat_id = self.active.get(tg)
        return chat_id if chat_id is not None and db.get_chat(chat_id) is not None else None

    def set_active(self, tg: int, chat_id: int):
        self.active[tg] = chat_id
        db.set_setting(telegram.CHATS_KEY, telegram.dump_chat_map(self.active))

    def new_chat(self, tg: int) -> int:
        """A new chat with the tool set of the Telegram tab (tools the connected servers lack are skipped)."""
        tools = self.panel.tool_ids()
        tp = self.w.tools_panel
        if tp.states:
            known = {t.id for t in tp.tools}
            missing = sorted(tools - known)
            if missing:
                self.panel.add_log(f"Tools not found and skipped in the new chat: {', '.join(missing)}")
            tools &= known
        chat_id = db.create_chat()
        if tools:
            db.save_chat_tools(chat_id, tools)
        self.set_active(tg, chat_id)
        self.w.chat_list.reload()
        return chat_id

    def _preview(self, chat_id: int) -> str:
        rows = [r for r in db.list_messages(chat_id) if r["role"] in ("user", "assistant") and (r["content"] or "").strip()]
        lines = [f"{'You' if r['role'] == 'user' else 'Model'}: {_short(r['content'])}" for r in rows[-2:]]
        return "\n\n".join(lines)

    # --- incoming ---

    def on_received(self, item: dict):
        tg = item["chat_id"]
        if item["kind"] == "callback":
            self._on_callback(item)
            return
        cmd = telegram.command(item["text"]) if not item.get("photo") else None
        if cmd is None:
            self.ask(tg, item["text"], item.get("image", b"") if item.get("photo") else None)
            return
        name = cmd[0]
        if name == "chat":
            text, markup = telegram.chats_keyboard(db.list_chats(), 0, self.active_chat(tg))
            self.say(tg, text, markup)
        elif name == "new":
            self.new_chat(tg)
            self.say(tg, "Started a new chat. Write your message.")
        elif name == "stop":
            s = self.w.stream
            if s and s.get("telegram") == tg:
                self.w.stop_stream()
            else:
                self.say(tg, "Nothing is running.")
        elif name == "tools":
            self.say(tg, self._tools_text(tg))
        else:  # /start, /help and unknown commands
            self.say(tg, telegram.HELP)

    def _tools_text(self, tg: int) -> str:
        chat_id = self.active_chat(tg)
        if chat_id is None:
            tools = sorted(self.panel.tool_ids())
            head = "No active chat. A new chat will get these tools:"
        else:
            tools = sorted(db.load_chat_tools(chat_id))
            head = f"Tools of \"{db.get_chat(chat_id)['title']}\":"
            agents = sorted(db.load_chat_agents(chat_id))
            if agents:
                tools += [f"agent: {a}" for a in agents]
        return head + ("\n" + "\n".join(f"• {t}" for t in tools) if tools else " none")

    def _on_callback(self, item: dict):
        tg, message_id, data = item["chat_id"], item["message_id"], item["data"]
        callback_id = item["callback_id"]
        self.service.post(lambda api: api.answer_callback(callback_id))

        def edit(text, markup=None):
            self.service.post(lambda api: api.edit_message(tg, message_id, text, markup=markup))

        def show_page(page: int):
            text, markup = telegram.chats_keyboard(db.list_chats(), page, self.active_chat(tg))
            edit(text, markup)

        if data.startswith("page:"):
            show_page(int(data[5:]) if data[5:].isdigit() else 0)
        elif data == "new":
            self.new_chat(tg)
            edit("Started a new chat. Write your message.")
        elif data.startswith("chat:") and data[5:].isdigit():
            chat = db.get_chat(int(data[5:]))
            if chat is None:
                show_page(0)
                return
            self.set_active(tg, chat["id"])
            preview = self._preview(chat["id"])
            edit(f"Active chat: {telegram.short_title(chat['title'])}" + (f"\n\n{preview}" if preview else ""))

    def ask(self, tg: int, text: str, image: bytes | None):
        """A message for the model: into the active chat (a new one if there is none), as if typed in the window."""
        s = self.w.stream
        if s:
            chat = db.get_chat(s["chat_id"])
            where = f" in \"{chat['title']}\"" if chat else ""
            self.say(tg, f"Busy: a reply is being generated{where}. Wait for it or send /stop.")
            return
        paths = []
        if image is not None:
            path = save_image(image) if image else ""
            if path:
                paths.append(path)
            else:
                self.say(tg, "Could not read the picture.")
                if not text.strip():
                    return
        chat_id = self.active_chat(tg) or self.new_chat(tg)
        self.w.show_chat(chat_id)
        error = self.w.submit(text, paths, telegram=tg)
        if error:
            self.say(tg, f"⚠ {error}")
            return
        self._typing_chat = tg
        self.service.post(lambda api: api.send_action(tg))
        self.typing.start()
