"""Telegram bot (pure Python, no Qt): a small Bot API client on httpx, message formatting and parsing.

The bot works by long polling (getUpdates): only outgoing connections, no public address needed. The polling and
sending threads live in worker.TelegramService; routing into chats is ui/telegram_bridge.py.
"""
import html
import json
import re
import time
from pathlib import Path

import httpx

from core.richtext import simplify_latex

API_URL = "https://api.telegram.org"
MAX_MESSAGE = 4096          # Telegram's limit for one message (characters after entity parsing)
CHUNK = 3500                # a reply is cut into parts of this size before HTML: escaping and tags need room
POLL_TIMEOUT = 50           # seconds a getUpdates call waits for new messages
STALE_SECONDS = 600         # messages older than this (sent while the harness was closed) are not answered
PAGE_SIZE = 8               # chats per page in /chat
TITLE_MAX = 40              # a chat title on a button

TOKEN_KEY = "telegram_token"          # app_settings keys
USERS_KEY = "telegram_users"          # allowed Telegram user ids, "123, 456"
TOOLS_KEY = "telegram_tools"          # JSON list of tool ids for new chats started from Telegram
AUTOSTART_KEY = "telegram_autostart"  # "1" = start the bot with the app
CHATS_KEY = "telegram_chats"          # JSON {telegram chat id: harness chat id}, the active chat of each Telegram chat

COMMANDS = [
    ("chat", "Choose a chat or start a new one"),
    ("new", "Start a new chat"),
    ("stop", "Stop the current reply"),
    ("tools", "Tools of the active chat"),
]
HELP = ("Messages go to the active chat of SaiHarness.\n"
        "/chat: choose a chat or start a new one\n/new: start a new chat\n/stop: stop the current reply\n"
        "/tools: tools of the active chat")


class TelegramError(Exception):
    """A Bot API call failed; the text never contains the token."""


class BotApi:
    """Bot API methods used by the harness. transport is for tests (httpx.MockTransport)."""

    def __init__(self, token: str, transport=None, base_url: str = API_URL):
        self.token = token.strip()
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(transport=transport, timeout=30)

    def close(self):
        self._client.close()

    def _clean(self, text: str) -> str:
        return text.replace(self.token, "<token>") if self.token else text

    def call(self, method: str, http_timeout: float = 30, files=None, **params):
        url = f"{self._base}/bot{self.token}/{method}"
        params = {k: v for k, v in params.items() if v is not None}
        try:
            if files:
                data = {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
                r = self._client.post(url, data=data, files=files, timeout=http_timeout)
            else:
                r = self._client.post(url, json=params, timeout=http_timeout)
            body = r.json()
        except httpx.HTTPError as e:
            raise TelegramError(self._clean(f"no connection to Telegram: {e}")) from None
        except ValueError:
            raise TelegramError(f"Telegram returned something that is not JSON (HTTP {r.status_code})") from None
        if not body.get("ok"):
            raise TelegramError(self._clean(body.get("description") or f"HTTP {r.status_code}"))
        return body.get("result")

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int, timeout: int = POLL_TIMEOUT) -> list:
        return self.call("getUpdates", http_timeout=timeout + 15, offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"]) or []

    def set_commands(self, commands=COMMANDS):
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    def send_message(self, chat_id: int, text: str, html_mode: bool = False, markup: dict | None = None) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML" if html_mode else None,
                         reply_markup=markup, link_preview_options={"is_disabled": True})

    def edit_message(self, chat_id: int, message_id: int, text: str, html_mode: bool = False, markup: dict | None = None):
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         parse_mode="HTML" if html_mode else None, reply_markup=markup)

    def answer_callback(self, callback_id: str, text: str = ""):
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)

    def send_action(self, chat_id: int, action: str = "typing"):
        return self.call("sendChatAction", chat_id=chat_id, action=action)

    def send_photo(self, chat_id: int, path: str, caption: str = ""):
        p = Path(path)
        with p.open("rb") as f:
            return self.call("sendPhoto", http_timeout=120, files={"photo": (p.name, f)}, chat_id=str(chat_id),
                             caption=caption[:1024] or None)

    def download(self, file_id: str) -> bytes:
        info = self.call("getFile", file_id=file_id)
        url = f"{self._base}/file/bot{self.token}/{info['file_path']}"
        try:
            r = self._client.get(url, timeout=120)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise TelegramError(self._clean(f"could not download the file: {e}")) from None
        return r.content


# --- formatting ---

_FENCE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)(?:^\1[ \t]*$|\Z)", re.S | re.M)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?!\s)(.+?)(?<!\s)\1")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?![\s_])([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)


def _inline(text: str) -> str:
    """Markdown outside code blocks -> Telegram HTML (b, i, s, code, a). Everything else is escaped text."""
    keep: list[str] = []

    def hold(s: str) -> str:
        keep.append(s)
        return f"\x00{len(keep) - 1}\x00"

    text = _INLINE_CODE.sub(lambda m: hold(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = _LINK.sub(lambda m: hold(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1), quote=False)}</a>'), text)
    text = html.escape(text, quote=False)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _BOLD.sub(r"<b>\2</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def to_html(markdown: str) -> str:
    """A model reply (Markdown) -> the HTML subset Telegram accepts. Tables and other markup stay as plain text."""
    text = simplify_latex(markdown)
    out, pos = [], 0
    for m in _FENCE.finditer(text):
        out.append(_inline(text[pos:m.start()]))
        lang = text[m.start():text.index("\n", m.start())].strip("`~ \t")
        cls = f' class="language-{html.escape(lang)}"' if re.fullmatch(r"[\w+#.-]+", lang or "") else ""
        out.append(f"<pre><code{cls}>{html.escape(m.group(2).rstrip(), quote=False)}</code></pre>\n")
        pos = m.end()
    out.append(_inline(text[pos:]))
    return "".join(out).strip()


def split_text(text: str, limit: int = CHUNK) -> list[str]:
    """Cuts a long reply into parts at line boundaries; a code block cut in two is closed and reopened."""
    lines = []
    for line in text.split("\n"):
        while len(line) > limit:
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)
    chunks, cur, size, fence = [], [], 0, None
    for line in lines:
        if cur and size + len(line) + 1 > limit:
            if fence:
                cur.append(fence[:3])
            chunks.append("\n".join(cur))
            cur, size = ([fence], len(fence) + 1) if fence else ([], 0)
        cur.append(line)
        size += len(line) + 1
        marker = line.strip()
        if marker.startswith(("```", "~~~")):
            fence = None if fence else marker
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def send_reply(api: BotApi, chat_id: int, text: str):
    """Sends a model reply: Markdown as Telegram HTML, cut into parts; a part Telegram cannot parse goes as plain text."""
    for part in split_text(text) or ["(empty reply)"]:
        rendered = to_html(part)
        if rendered and len(rendered) <= MAX_MESSAGE:
            try:
                api.send_message(chat_id, rendered, html_mode=True)
                continue
            except TelegramError as e:
                if "parse" not in str(e).lower() and "entit" not in str(e).lower():
                    raise
        api.send_message(chat_id, part)


# --- input ---

def parse_users(text: str) -> set[int]:
    """'123, 456 789' -> {123, 456, 789}; anything that is not a number is ignored."""
    return {int(x) for x in re.findall(r"-?\d+", text or "")}


def parse_update(update: dict, now: float | None = None) -> dict | None:
    """A Bot API update -> a flat dict (kind 'message' or 'callback'); None for anything the bot does not handle."""
    if "callback_query" in update:
        q = update["callback_query"]
        msg = q.get("message") or {}
        return {"kind": "callback", "user_id": q["from"]["id"], "username": q["from"].get("username", ""),
                "chat_id": (msg.get("chat") or {}).get("id", q["from"]["id"]), "message_id": msg.get("message_id"),
                "callback_id": q["id"], "data": q.get("data", "")}
    msg = update.get("message")
    if not msg or "from" not in msg:
        return None
    photo = ""
    if msg.get("photo"):
        photo = msg["photo"][-1]["file_id"]  # the largest size comes last
    elif (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
        photo = msg["document"]["file_id"]
    text = msg.get("text") or msg.get("caption") or ""
    if not text and not photo:
        return None
    stale = (now or time.time()) - msg.get("date", 0) > STALE_SECONDS
    return {"kind": "message", "user_id": msg["from"]["id"], "username": msg["from"].get("username", ""),
            "chat_id": msg["chat"]["id"], "text": text, "photo": photo, "stale": stale}


def command(text: str) -> tuple[str, str] | None:
    """'/chat@MyBot args' -> ('chat', 'args'); None if the text is not a command."""
    m = re.match(r"^/([A-Za-z0-9_]+)(?:@\w+)?(?:\s+(.*))?$", text.strip(), re.S)
    return (m.group(1).lower(), (m.group(2) or "").strip()) if m else None


# --- the chat list ---

def short_title(title: str, limit: int = TITLE_MAX) -> str:
    title = " ".join(title.split())
    return title if len(title) <= limit else title[:limit - 1] + "…"


def chats_keyboard(chats: list, page: int, active_id: int | None, page_size: int = PAGE_SIZE) -> tuple[str, dict]:
    """The /chat message: text and inline keyboard (chats of one page, page arrows, "New chat").

    chats are rows with id and title, newest first. Button data: chat:<id>, page:<n>, new.
    """
    pages = max(1, -(-len(chats) // page_size))
    page = min(max(0, page), pages - 1)
    rows = [[{"text": ("✓ " if c["id"] == active_id else "") + short_title(c["title"]), "callback_data": f"chat:{c['id']}"}]
            for c in chats[page * page_size:(page + 1) * page_size]]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "◀", "callback_data": f"page:{page - 1}"})
        nav.append({"text": f"{page + 1}/{pages}", "callback_data": f"page:{page}"})
        if page < pages - 1:
            nav.append({"text": "▶", "callback_data": f"page:{page + 1}"})
        rows.append(nav)
    rows.append([{"text": "➕ New chat", "callback_data": "new"}])
    active = next((c for c in chats if c["id"] == active_id), None)
    text = f"Active chat: {short_title(active['title'])}\nChoose a chat:" if active else "Choose a chat:"
    if not chats:
        text = "There are no chats yet."
    return text, {"inline_keyboard": rows}


def load_chat_map(raw: str) -> dict[int, int]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def dump_chat_map(mapping: dict[int, int]) -> str:
    return json.dumps({str(k): v for k, v in mapping.items()})
