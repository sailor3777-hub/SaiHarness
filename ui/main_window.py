import json
import time
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow, QSplitter

from core import db, images, llm, memory, vision
from core.context_builder import build_messages, estimate_request_tokens
from core.files import Selection
from core.tools import META_TOOLS, Catalog, ToolRouter, describe_call, result_label
from ui.chat_list import ChatList
from ui.chat_view import ChatView
from ui import themes
from ui.context_panel import ContextPanel
from ui.permission import PermissionBroker
from ui.request_dialog import RequestDialog
from worker import CONTEXT_SAFETY, ChatStreamer, ContextLimitRequest

TITLE_MAX = 40
SCALE_MIN, SCALE_MAX = 0.6, 2.0   # the range within which the server's measurements may correct our token estimate
MIN_TOKENS_TO_CALIBRATE = 300     # on short requests the ratio is too noisy


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")
LIMIT_TTL = 20.0  # how often to re-check with the server how much context the loaded model has
LOCK_NOTE = "The model is locked to this chat: it is not changed after the first message."


class MainWindow(QMainWindow):
    def __init__(self, mcp_config: Path | None = None, connect_mcp: bool = True):
        super().__init__()
        self.setWindowTitle("SaiHarness")
        self.resize(1400, 850)
        self.chat_list = ChatList()
        self.chat_view = ChatView()
        self.context = ContextPanel(mcp_config, connect_mcp)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.chat_list)
        splitter.addWidget(self.chat_view)
        splitter.addWidget(self.context)
        splitter.setSizes([240, 760, 400])
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.chat_id: int | None = None
        self.stream: dict | None = None  # active generation: id, chat_id, thread, content, reasoning
        self._stream_seq = 0
        self._threads: set[ChatStreamer] = set()
        self.last_requests: dict[int, dict] = {}  # chat_id -> the last request sent (in memory only)
        self._free_choice: str | None = None  # the user's last profile choice in an unlocked chat
        self.selector = self.context.model_selector
        self.files_panel = self.context.files_panel
        self.limit_req = ContextLimitRequest(self)
        self._limit_key: tuple | None = None
        self._limit_time = 0.0
        self._limit_info: tuple | None = None  # (working limit | None, model loaded, maximum) for _limit_key
        self._scale: dict[tuple, float] = {}  # (url, model) -> correction of our token estimate from the server's measurements
        self._last_usage: dict[int, int] = {}  # chat_id -> tokens in the last request counted by the server
        self.limit_req.succeeded.connect(self._on_limit)
        self.limit_req.failed.connect(self._on_limit_failed)
        self.files_panel.selection_changed.connect(self._on_selection_changed)
        self.tools_panel = self.context.tools_panel
        self.broker = PermissionBroker(self)
        self.tools_panel.selection_changed.connect(self._on_tools_changed)
        self.tools_panel.catalog_changed.connect(self._refresh_context_info)

        self.chat_list.new_requested.connect(self.new_chat)
        self.chat_list.theme_requested.connect(themes.apply)
        self.chat_list.chat_selected.connect(self.open_chat)
        self.chat_list.chat_renamed.connect(self.on_renamed)
        self.chat_list.chat_deleted.connect(self.on_chat_deleted)
        self.chat_view.message_submitted.connect(self.on_submit)
        self.chat_view.stop_requested.connect(self.stop_stream)
        self.chat_view.request_view_requested.connect(self.show_request)
        self.selector.user_changed.connect(self._remember_choice)
        self.selector.profiles_changed.connect(self._refresh_lock)

        self._free_choice = self.selector.profile.currentText() or None
        self.chat_list.reload()
        first = self.chat_list.list.item(0)
        if first is not None:
            self.chat_list.list.setCurrentItem(first)
        else:
            self.open_chat(None)

    # --- chats ---

    def new_chat(self):
        # files checked before the first chat is created move into it; otherwise a chat starts empty
        draft = self.files_panel.selection.copy() if self.chat_id is None else Selection()
        draft_tools = set(self.tools_panel.enabled) if self.chat_id is None else set()
        chat_id = db.create_chat()
        if not draft.is_empty():
            db.save_chat_context(chat_id, draft.include, draft.exclude)
        if draft_tools:
            db.save_chat_tools(chat_id, draft_tools)
        self.chat_list.reload(select_id=chat_id)
        self.open_chat(chat_id)

    def open_chat(self, chat_id: int | None):
        self.chat_id = chat_id
        chat = db.get_chat(chat_id) if chat_id is not None else None
        if chat is None:
            self.chat_id = None
            self.chat_view.set_title("Dialog")
            self.chat_view.set_messages([])
            self.files_panel.set_selection(Selection())
            self.tools_panel.set_enabled(set())
        else:
            self.chat_view.set_title(chat["title"])
            self.chat_view.set_messages(db.list_messages(chat_id))
            self.files_panel.set_selection(Selection(*db.load_chat_context(chat_id)))
            self.tools_panel.set_enabled(db.load_chat_tools(chat_id))
            if self.stream and self.stream["chat_id"] == chat_id:
                self.chat_view.begin_live(self.stream["reasoning"], self.stream["content"])
        self._refresh_lock()
        self._update_status()

    def _on_selection_changed(self):
        if self.chat_id is not None:
            sel = self.files_panel.selection
            db.save_chat_context(self.chat_id, sel.include, sel.exclude)

    def _on_tools_changed(self):
        if self.chat_id is not None:
            db.save_chat_tools(self.chat_id, self.tools_panel.enabled)
        self._refresh_context_info()

    def _tools_text(self, profile, catalog) -> str:
        """The tool catalog and, if memory tools are enabled, the profile's rules with the list of memory topics."""
        if not catalog:
            return ""
        with closing(db.connect()) as conn:
            block = memory.prompt_block(profile["memory_rules"], [t.id for t in catalog.tools], memory.Memory(conn))
        return catalog.text() + (f"\n\n{block}" if block else "")

    # --- context size ---

    def _refresh_context_info(self, force: bool = False):
        """Updates the "whole request" estimate and the model's context limit under the file tree."""
        profile = self._bound_profile()
        if profile is None and self.selector.profile.currentText():
            profile = db.get_profile(self.selector.profile.currentText())
        history = db.list_messages(self.chat_id) if self.chat_id is not None else []
        messages = build_messages(profile, history) if profile else [
            {"role": m["role"], "content": m["content"]} for m in history if m["content"]
        ]
        tokens = estimate_request_tokens(messages)
        self.files_panel.set_extra_tokens(tokens + self.tools_panel.prompt_tokens())
        self.files_panel.set_actual(self._last_usage.get(self.chat_id))

        conn = db.get_connection(profile["connection"]) if profile and profile["connection"] else None
        if conn is None:
            self._limit_key = None
            self._limit_info = None
            self.limit_req.cancel()
            self.files_panel.set_limit(None)
            self.files_panel.set_scale(1.0)
            return
        key = (conn["url"], profile["base_model"])
        self.files_panel.set_scale(self._scale.get(key, 1.0))
        if force or key != self._limit_key or time.monotonic() - self._limit_time > LIMIT_TTL:
            if key != self._limit_key:
                self._limit_info = None
            self._limit_key, self._limit_time = key, time.monotonic()
            self.limit_req.start(conn["url"], conn["api_key"], profile["base_model"])

    def _on_limit(self, info: tuple):
        self._limit_info = info
        self.files_panel.set_limit(info)

    def _on_limit_failed(self, _message: str):
        self._limit_info = None
        self.files_panel.set_limit(None)

    def _known_limit(self, conn, profile) -> int | None:
        """Working context of the loaded model, if it is known for exactly this connection + model pair."""
        if self._limit_info and self._limit_key == (conn["url"], profile["base_model"]):
            return self._limit_info[0] or None
        return None

    def _context_overflow(self, conn, profile, messages: list, tools: list | None) -> str:
        """The refusal text if the request with room for the reply does not fit into the model's context, otherwise an empty string."""
        limit = self._known_limit(conn, profile)
        if not limit:
            return ""
        scale = max(1.0, self._scale.get((conn["url"], profile["base_model"]), 1.0))  # the guard does not "learn downwards"
        estimated = int(estimate_request_tokens(messages, tools) * scale * CONTEXT_SAFETY)
        reserve = min(profile["max_tokens"], limit // 4)
        if estimated + reserve <= limit:
            return ""
        return (f"The request ≈{_fmt(estimated)} tok. and a reply of up to {_fmt(reserve)} do not fit into the model's context "
                f"({_fmt(limit)}). Uncheck files, start a new chat or increase the context in LM Studio.")

    def _on_usage(self, stream_id: int, prompt_tokens: int, _completion: int, raw_estimate: int):
        """The real token count from the server: show it and correct the estimate for this model."""
        s = self.stream
        if not s or s["id"] != stream_id:
            return
        self._last_usage[s["chat_id"]] = prompt_tokens
        if raw_estimate >= MIN_TOKENS_TO_CALIBRATE and prompt_tokens > 0:
            old = self._scale.get(s["key"], 1.0)
            new = min(SCALE_MAX, max(SCALE_MIN, 0.5 * old + 0.5 * prompt_tokens / raw_estimate))
            self._scale[s["key"]] = new
            s["thread"].scale = max(1.0, new)  # the next rounds are checked with the correction, but never softer than the base estimate
        if s["chat_id"] == self.chat_id:
            self.files_panel.set_scale(self._scale.get(s["key"], 1.0))
            self.files_panel.set_actual(prompt_tokens)

    def on_renamed(self, chat_id: int, title: str):
        if chat_id == self.chat_id:
            self.chat_view.set_title(title)

    def on_chat_deleted(self, chat_id: int):
        if self.stream and self.stream["chat_id"] == chat_id:
            self._cancel_stream()
            self.stream = None
            self.chat_view.set_busy(False)

    # --- chat profile ---

    def _remember_choice(self, name: str):
        self._free_choice = name
        self._refresh_context_info()

    def _bound_profile(self):
        """The profile locked to the current chat, if it still exists."""
        chat = db.get_chat(self.chat_id) if self.chat_id is not None else None
        if chat and chat["profile"]:
            return db.get_profile(chat["profile"])
        return None

    def _refresh_lock(self):
        bound = self._bound_profile()
        if bound is not None:
            self.selector.select(bound["name"])
            self.selector.set_locked(True, LOCK_NOTE)
        else:
            self.selector.set_locked(False)
            if self._free_choice:
                self.selector.select(self._free_choice)
        self._refresh_context_info()

    def _update_status(self):
        if self.stream:
            if self.stream["chat_id"] != self.chat_id:
                self.chat_view.set_status("A reply is in progress in another chat…")
                return
            parts = ["Generating…"]
            if self.stream["files"]:
                parts.append(f"files: {self.stream['files']}")
            if self.stream["skipped"]:
                parts.append(f"skipped: {self.stream['skipped']}")
            if self.stream["rounds"]:
                parts.append(f"tool calls: {self.stream['rounds']}")
            self.chat_view.set_status(" · ".join(parts))
            return
        bound = self._bound_profile()
        self.chat_view.set_status(f"{bound['name']} · {bound['base_model']}" if bound else "")

    # --- sending and streaming ---

    def on_submit(self, text: str):
        picked = list(self.chat_view.pending)
        if self.stream:
            return
        profile = self._bound_profile()
        if profile is None:
            name = self.selector.profile.currentText()
            profile = db.get_profile(name) if name else None
        if profile is None:
            self.chat_view.set_status("Choose a model on the Model tab")
            return
        conn = db.get_connection(profile["connection"]) if profile["connection"] else None
        if conn is None:
            self.chat_view.set_status(
                f"Profile \"{profile['name']}\" has no connection. Choose one in Model Builder."
            )
            return
        bundle = self.files_panel.load_bundle()
        if bundle.truncated:
            self.chat_view.set_status(
                "Too many files or too much content selected. Reduce the selection on the Files tab."
            )
            return

        enabled = self.tools_panel.enabled_tools()
        catalog = Catalog(enabled) if enabled else None
        # Context overflow guard: before writing anything to the history, check that the request fits
        known_history = db.list_messages(self.chat_id) if self.chat_id is not None else []
        candidate = build_messages(profile, known_history, bundle.files, self._tools_text(profile, catalog))
        candidate.append({"role": "user", "content": images.user_content(text, picked)})
        overflow = self._context_overflow(conn, profile, candidate, META_TOOLS if catalog else None)
        if overflow:
            self.chat_view.set_status(overflow)
            return

        if self.chat_id is None:
            self.new_chat()
        chat = db.get_chat(self.chat_id)
        if db.is_default_title(chat["title"]):
            title = " ".join((text or "Image").split())[:TITLE_MAX]
            db.rename_chat(self.chat_id, title)
            self.chat_view.set_title(title)
        if chat["profile"] != profile["name"]:
            db.set_chat_profile(self.chat_id, profile["name"])
        db.add_message(self.chat_id, "user", text, images=images.encode_paths(picked))
        self.chat_view.input.clear()
        self.chat_view.clear_pending()
        self.chat_view.append_message("user", text, image_paths=picked)

        router = ToolRouter(self.tools_panel.manager, catalog, policy_for=self.tools_panel.effective_policy) if catalog else None
        messages = build_messages(
            profile, db.list_messages(self.chat_id), bundle.files, self._tools_text(profile, catalog)
        )
        key = (conn["url"], profile["base_model"])
        self._stream_seq += 1
        thread = ChatStreamer(
            self._stream_seq, conn["url"], conn["api_key"], profile["base_model"],
            messages, profile["temperature"], profile["max_tokens"],
            tools=META_TOOLS if catalog else None, router=router, max_steps=profile["max_steps"],
            context_limit=self._known_limit(conn, profile), scale=max(1.0, self._scale.get(key, 1.0)),
            prepare=self._image_preparer(conn, profile) if images.has_placeholders(messages) else None,
        )
        if router is not None:
            router.confirm = lambda tool_id, args, cancel=thread.cancel_event: self.broker.ask(tool_id, args, cancel)
        self.last_requests[self.chat_id] = {
            "url": conn["url"], "model": profile["base_model"], "temperature": profile["temperature"],
            "max_tokens": profile["max_tokens"], "messages": messages,
            "tools": [t["function"]["name"] for t in META_TOOLS] if catalog else [],
        }
        thread.delta.connect(self._on_delta)
        thread.tool_running.connect(self._on_tool_running)
        thread.tool_round.connect(self._on_tool_round)
        thread.usage.connect(self._on_usage)
        thread.failed.connect(self._on_stream_failed)
        thread.completed.connect(self._on_stream_completed)
        thread.finished.connect(self._reap_threads)
        self._reap_threads()
        self.stream = {
            "id": self._stream_seq, "chat_id": self.chat_id, "thread": thread,
            "content": "", "reasoning": "", "notice": "", "rounds": 0, "key": key,
            "files": len(bundle.files), "skipped": len(bundle.skipped),
        }
        self._threads.add(thread)
        self.chat_view.begin_live()
        self.chat_view.set_busy(True)
        self._refresh_lock()
        self._update_status()
        self.chat_list.reload(select_id=self.chat_id)
        thread.start()

    @staticmethod
    def _image_preparer(conn, profile):
        """Preparing pictures in the reply thread: a vision model gets the images themselves, the others get a description."""
        url, key, model = conn["url"], conn["api_key"], profile["base_model"]

        def prepare(messages: list, notify) -> list:
            if llm.model_type(url, key, model) != "llm":  # vlm or an unknown server: send the pictures as they are
                return images.to_data_urls(messages)
            try:
                cfg = vision.load_config(db.DB_PATH)
            except vision.VisionError as e:
                raise llm.LLMError(f"Model \"{model}\" cannot see images, and there is no model to describe them: {e}") from None
            notify(f"The model cannot see images: describing them with \"{cfg.model}\"")

            def describe(path):
                try:
                    return vision.describe_cached(cfg, path)
                except vision.VisionError as e:
                    raise llm.LLMError(str(e)) from None
            return images.to_descriptions(messages, describe)
        return prepare

    def _on_delta(self, stream_id: int, kind: str, text: str):
        if not self.stream or self.stream["id"] != stream_id:
            return
        if kind == "notice":
            s = self.stream
            s["notice"] = f"{s['notice']} · {text}" if s["notice"] else text
            if s["chat_id"] == self.chat_id:
                self.chat_view.set_status(s["notice"])
            return
        self.stream["reasoning" if kind == "reasoning" else "content"] += text
        if self.stream["chat_id"] == self.chat_id:
            self.chat_view.append_delta(kind, text)

    def _on_tool_running(self, stream_id: int, name: str, arguments: str):
        s = self.stream
        if s and s["id"] == stream_id and s["chat_id"] == self.chat_id:
            self.chat_view.set_status(f"Tool: {describe_call(name, arguments)}")

    def _on_tool_round(self, stream_id: int, assistant: dict, results: list):
        """A "model called tools and got the results" round is saved as a whole and shown in the chat."""
        s = self.stream
        if not s or s["id"] != stream_id or db.get_chat(s["chat_id"]) is None:
            return
        labels = {
            c["id"]: result_label(c["function"]["name"], c["function"]["arguments"]) for c in assistant["tool_calls"]
        }
        rows = [{"role": "assistant", "content": assistant["content"], "reasoning": assistant["reasoning"],
                 "tool_calls": json.dumps(assistant["tool_calls"], ensure_ascii=False)}]
        rows += [
            {"role": "tool", "content": r["content"], "tool_call_id": r["tool_call_id"],
             "tool_name": labels.get(r["tool_call_id"], r["name"])}
            for r in results
        ]
        db.add_messages(s["chat_id"], rows)
        s["content"] = s["reasoning"] = ""  # the next part of the reply accumulates from here
        s["rounds"] += 1
        if s["chat_id"] == self.chat_id:
            self.chat_view.set_messages(db.list_messages(s["chat_id"]))
            self.chat_view.begin_live()
            self._update_status()
        self._refresh_context_info()

    def _on_stream_failed(self, stream_id: int, message: str):
        self._finish(stream_id, error=message)

    def _on_stream_completed(self, stream_id: int):
        self._finish(stream_id)

    def _reap_threads(self):
        """Forget finished threads (only once the OS thread has actually ended)."""
        self._threads = {t for t in self._threads if not t.isFinished()}

    def show_request(self):
        RequestDialog(self, self.last_requests.get(self.chat_id)).exec()

    def stop_stream(self):
        if self.stream:
            self._finish(self.stream["id"], stopped=True)

    def _cancel_stream(self):
        if self.stream:
            self.stream["thread"].cancel()

    def _finish(self, stream_id: int, error: str | None = None, stopped: bool = False):
        """Finishes generation: saves what has arrived and updates the interface."""
        s = self.stream
        if not s or s["id"] != stream_id:
            return
        s["thread"].cancel()
        self.stream = None
        has_text = bool(s["content"] or s["reasoning"])
        # a reply that arrived as reasoning: save it as the answer, otherwise it would disappear from the model's history
        promoted = not error and not stopped and llm.answer_in_reasoning(s["content"], s["reasoning"], s["notice"])
        if promoted:
            s["content"], s["reasoning"] = s["reasoning"].strip(), ""
        if has_text and db.get_chat(s["chat_id"]) is not None:
            db.add_message(s["chat_id"], "assistant", s["content"], s["reasoning"])
        if s["chat_id"] == self.chat_id:
            self.chat_view.end_live(keep=has_text)
            if promoted and db.get_chat(s["chat_id"]) is not None:
                self.chat_view.set_messages(db.list_messages(s["chat_id"]))
        # an error, a stop or an empty reply stays in the history as a "System" line (it is not sent to the model)
        if error:
            note = error
        elif stopped:
            note = "Stopped"
        elif not has_text:
            note = "The model returned an empty reply" + (f" ({s['notice']})" if s["notice"] else "")
        else:
            note = ""
        if note and db.get_chat(s["chat_id"]) is not None:
            db.add_message(s["chat_id"], "system", note)
            if s["chat_id"] == self.chat_id:
                self.chat_view.append_message("system", note)
        self.chat_view.set_busy(False)
        self._update_status()
        self._refresh_context_info()
        if error:
            self.chat_view.set_status(error)
        elif stopped:
            self.chat_view.set_status("Stopped")
        elif s.get("notice") and s["chat_id"] == self.chat_id:
            self.chat_view.set_status(s["notice"])
        self.chat_list.reload()

    def closeEvent(self, event):
        self._cancel_stream()
        for t in list(self._threads):
            if not t.wait(3000):
                t.terminate()
                t.wait(1000)
        self.context.shutdown()
        self.limit_req.wait()
        super().closeEvent(event)
