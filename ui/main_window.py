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
SCALE_MIN, SCALE_MAX = 0.6, 2.0   # в каких пределах замеры сервера могут поправлять нашу оценку токенов
MIN_TOKENS_TO_CALIBRATE = 300     # на коротких запросах отношение слишком шумное


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")
LIMIT_TTL = 20.0  # как часто перепроверять у сервера, сколько контекста у загруженной модели
LOCK_NOTE = "Модель закреплена за этим чатом: после первого сообщения её не меняют."


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
        self.stream: dict | None = None  # активная генерация: id, chat_id, thread, content, reasoning
        self._stream_seq = 0
        self._threads: set[ChatStreamer] = set()
        self.last_requests: dict[int, dict] = {}  # chat_id -> последний отправленный запрос (только в памяти)
        self._free_choice: str | None = None  # последний выбор профиля пользователем в незакреплённом чате
        self.selector = self.context.model_selector
        self.files_panel = self.context.files_panel
        self.limit_req = ContextLimitRequest(self)
        self._limit_key: tuple | None = None
        self._limit_time = 0.0
        self._limit_info: tuple | None = None  # (рабочий лимит | None, модель загружена, максимум) для _limit_key
        self._scale: dict[tuple, float] = {}  # (url, модель) -> поправка нашей оценки токенов по замерам сервера
        self._last_usage: dict[int, int] = {}  # chat_id -> токенов в последнем запросе по счёту сервера
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

    # --- чаты ---

    def new_chat(self):
        # файлы, отмеченные до создания первого чата, переходят в него; в остальных случаях чат начинается пустым
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
            self.chat_view.set_title("Диалог")
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
        """Каталог инструментов и, если включены инструменты памяти, правила профиля с перечнем тем в памяти."""
        if not catalog:
            return ""
        with closing(db.connect()) as conn:
            block = memory.prompt_block(profile["memory_rules"], [t.id for t in catalog.tools], memory.Memory(conn))
        return catalog.text() + (f"\n\n{block}" if block else "")

    # --- размер контекста ---

    def _refresh_context_info(self, force: bool = False):
        """Обновляет под деревом файлов оценку «запрос целиком» и лимит контекста модели."""
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
        """Рабочий контекст загруженной модели, если он известен именно для этой пары подключение + модель."""
        if self._limit_info and self._limit_key == (conn["url"], profile["base_model"]):
            return self._limit_info[0] or None
        return None

    def _context_overflow(self, conn, profile, messages: list, tools: list | None) -> str:
        """Текст отказа, если запрос с запасом на ответ не помещается в контекст модели, иначе пустая строка."""
        limit = self._known_limit(conn, profile)
        if not limit:
            return ""
        scale = max(1.0, self._scale.get((conn["url"], profile["base_model"]), 1.0))  # защита не «учится вниз»
        estimated = int(estimate_request_tokens(messages, tools) * scale * CONTEXT_SAFETY)
        reserve = min(profile["max_tokens"], limit // 4)
        if estimated + reserve <= limit:
            return ""
        return (f"Запрос ≈{_fmt(estimated)} ток. и ответ до {_fmt(reserve)} не помещаются в контекст модели "
                f"({_fmt(limit)}). Сними файлы, начни новый чат или увеличь контекст в LM Studio.")

    def _on_usage(self, stream_id: int, prompt_tokens: int, _completion: int, raw_estimate: int):
        """Настоящее число токенов от сервера: показываем его и поправляем оценку для этой модели."""
        s = self.stream
        if not s or s["id"] != stream_id:
            return
        self._last_usage[s["chat_id"]] = prompt_tokens
        if raw_estimate >= MIN_TOKENS_TO_CALIBRATE and prompt_tokens > 0:
            old = self._scale.get(s["key"], 1.0)
            new = min(SCALE_MAX, max(SCALE_MIN, 0.5 * old + 0.5 * prompt_tokens / raw_estimate))
            self._scale[s["key"]] = new
            s["thread"].scale = max(1.0, new)  # следующие раунды проверяются уже с поправкой, но не мягче базовой оценки
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

    # --- профиль чата ---

    def _remember_choice(self, name: str):
        self._free_choice = name
        self._refresh_context_info()

    def _bound_profile(self):
        """Профиль, закреплённый за текущим чатом, если он ещё существует."""
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
                self.chat_view.set_status("Идёт ответ в другом чате…")
                return
            parts = ["Генерация…"]
            if self.stream["files"]:
                parts.append(f"файлов: {self.stream['files']}")
            if self.stream["skipped"]:
                parts.append(f"пропущено: {self.stream['skipped']}")
            if self.stream["rounds"]:
                parts.append(f"вызовов инструментов: {self.stream['rounds']}")
            self.chat_view.set_status(" · ".join(parts))
            return
        bound = self._bound_profile()
        self.chat_view.set_status(f"{bound['name']} · {bound['base_model']}" if bound else "")

    # --- отправка и стриминг ---

    def on_submit(self, text: str):
        picked = list(self.chat_view.pending)
        if self.stream:
            return
        profile = self._bound_profile()
        if profile is None:
            name = self.selector.profile.currentText()
            profile = db.get_profile(name) if name else None
        if profile is None:
            self.chat_view.set_status("Выбери модель во вкладке «Модель»")
            return
        conn = db.get_connection(profile["connection"]) if profile["connection"] else None
        if conn is None:
            self.chat_view.set_status(
                f"У профиля «{profile['name']}» нет подключения. Выбери его в Model Builder."
            )
            return
        bundle = self.files_panel.load_bundle()
        if bundle.truncated:
            self.chat_view.set_status(
                "Выбрано слишком много файлов или объёма. Сократи выбор во вкладке «Файлы»."
            )
            return

        enabled = self.tools_panel.enabled_tools()
        catalog = Catalog(enabled) if enabled else None
        # Защита от переполнения контекста: до любых записей в историю проверяем, что запрос поместится
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
        if chat["title"] == db.DEFAULT_CHAT_TITLE:
            title = " ".join((text or "Изображение").split())[:TITLE_MAX]
            db.rename_chat(self.chat_id, title)
            self.chat_view.set_title(title)
        if chat["profile"] != profile["name"]:
            db.set_chat_profile(self.chat_id, profile["name"])
        db.add_message(self.chat_id, "user", text, images=images.encode_paths(picked))
        self.chat_view.input.clear()
        self.chat_view.clear_pending()
        self.chat_view.append_message("user", text, image_paths=picked)

        router = ToolRouter(self.tools_panel.manager, catalog, policy_for=self.tools_panel.policy_for) if catalog else None
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
        """Подготовка картинок в потоке ответа: модель с vision получает сами изображения, остальные их описание."""
        url, key, model = conn["url"], conn["api_key"], profile["base_model"]

        def prepare(messages: list, notify) -> list:
            if llm.model_type(url, key, model) != "llm":  # vlm или неизвестный сервер: отправляем картинки как есть
                return images.to_data_urls(messages)
            try:
                cfg = vision.load_config(db.DB_PATH)
            except vision.VisionError as e:
                raise llm.LLMError(f"Модель «{model}» не видит изображений, а описать их некому: {e}") from None
            notify(f"Модель не видит изображений: описываю их моделью «{cfg.model}»")

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
            self.chat_view.set_status(f"Инструмент: {describe_call(name, arguments)}")

    def _on_tool_round(self, stream_id: int, assistant: dict, results: list):
        """Раунд «модель позвала инструменты, получила результаты» сохраняется целиком и показывается в чате."""
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
        s["content"] = s["reasoning"] = ""  # дальше копится следующий отрезок ответа
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
        """Забыть завершившиеся потоки (только когда ОС-поток уже закончил работу)."""
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
        """Завершает генерацию: сохраняет то, что успело прийти, и обновляет интерфейс."""
        s = self.stream
        if not s or s["id"] != stream_id:
            return
        s["thread"].cancel()
        self.stream = None
        has_text = bool(s["content"] or s["reasoning"])
        # ответ, пришедший как размышления: сохраняем его как ответ, иначе в истории модели он пропадёт
        promoted = not error and not stopped and llm.answer_in_reasoning(s["content"], s["reasoning"], s["notice"])
        if promoted:
            s["content"], s["reasoning"] = s["reasoning"].strip(), ""
        if has_text and db.get_chat(s["chat_id"]) is not None:
            db.add_message(s["chat_id"], "assistant", s["content"], s["reasoning"])
        if s["chat_id"] == self.chat_id:
            self.chat_view.end_live(keep=has_text)
            if promoted and db.get_chat(s["chat_id"]) is not None:
                self.chat_view.set_messages(db.list_messages(s["chat_id"]))
        # ошибка, остановка или пустой ответ остаются в истории строкой «Система» (модели она не отправляется)
        if error:
            note = error
        elif stopped:
            note = "Остановлено"
        elif not has_text:
            note = "Модель вернула пустой ответ" + (f" ({s['notice']})" if s["notice"] else "")
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
            self.chat_view.set_status("Остановлено")
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
