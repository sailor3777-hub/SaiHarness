import os
import threading

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from core import llm
from core.agent_loop import CONTEXT_SAFETY, LIMIT_NUDGE, STUCK_NUDGE, AgentLoop  # noqa: F401 (re-exported for main_window and tests)
from core.db import DEFAULT_MAX_STEPS


class _CallThread(QThread):
    """Runs one function in the background and returns the result through a signal."""

    done = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, request_id: int, fn, args: tuple):
        super().__init__()
        self.request_id, self._fn, self._args = request_id, fn, args

    def run(self):
        try:
            self.done.emit(self.request_id, self._fn(*self._args))
        except llm.LLMError as e:
            self.failed.emit(self.request_id, str(e))
        except Exception as e:  # the thread must not die silently
            self.failed.emit(self.request_id, f"Error: {e}")


class AsyncCall(QObject):
    """A function call in a background thread with protection against hangs.

    A new start() invalidates the previous call (its result is ignored), and a watchdog
    timer guarantees that the interface gets a result or an error within TIMEOUT_MS,
    even if the function hangs.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    busy_changed = Signal(bool)

    TIMEOUT_MS = 8000
    TIMEOUT_TEXT = "The server did not answer within {seconds:g} s"

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._seq = 0
        self._busy = False
        self._threads: set[_CallThread] = set()
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_timeout)

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, *args):
        self._reap()
        self._seq += 1
        t = _CallThread(self._seq, self._fn, args)
        t.done.connect(self._on_done)
        t.failed.connect(self._on_failed)
        t.finished.connect(self._reap)
        self._threads.add(t)
        self._set_busy(True)
        self._watchdog.start(self.timeout_ms(args))
        t.start()

    def timeout_ms(self, args) -> int:
        """The watchdog for this call; args[0] is the server address for the model requests."""
        return self.TIMEOUT_MS

    def cancel(self):
        """Give up on the expected result (it will be ignored)."""
        self._seq += 1
        self._watchdog.stop()
        self._set_busy(False)

    def wait(self, timeout_ms: int = 6000):
        for t in list(self._threads):
            t.wait(timeout_ms)

    def _on_done(self, seq: int, result):
        if seq == self._seq:
            self._settle()
            self.succeeded.emit(result)

    def _on_failed(self, seq: int, message: str):
        if seq == self._seq:
            self._settle()
            self.failed.emit(message)

    def _on_timeout(self):
        self._seq += 1  # a late result of this call is ignored
        self._settle()
        self.failed.emit(self.TIMEOUT_TEXT.format(seconds=self._watchdog.interval() / 1000))

    def _settle(self):
        self._watchdog.stop()
        self._set_busy(False)

    def _set_busy(self, busy: bool):
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)

    def _reap(self):
        """Forget finished threads (only once the OS thread has actually ended)."""
        self._threads = {t for t in self._threads if not t.isFinished()}


class CrawlThread(QThread):
    """Crawls one site for the local search index. Its own thread and its own DB connection: a crawl takes minutes,
    so AsyncCall's watchdog timer does not fit here; stopping is cooperative (stop())."""

    progress = Signal(int, str)   # pages saved, current address
    finished_ok = Signal(object)  # result of crawl()
    failed = Signal(str)

    def __init__(self, source_id: int, db_path, parent=None):
        super().__init__(parent)
        self.source_id, self._db_path = source_id, db_path
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        import sqlite3
        from core.web.fetch import WebFetcher
        from core.web.index import WebIndex
        from core.web.render import render_html
        conn = None
        allow_private = os.environ.get("SAI_WEB_ALLOW_PRIVATE") == "1"  # only for tests with a local server
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            fetcher = WebFetcher(renderer=lambda u: render_html(u, allow_private=allow_private), allow_private=allow_private,
                                 delay=float(os.environ.get("SAI_WEB_DELAY", "1.0")))
            result = WebIndex(conn).crawl(
                self.source_id, fetcher,
                progress=lambda n, url: self.progress.emit(n, url), should_stop=self._stop.is_set)
            self.finished_ok.emit(result)
        except Exception as e:  # the thread must not die silently
            self.failed.emit(f"Crawl error: {e}")
        finally:
            if conn is not None:
                conn.close()


ANTHROPIC_TIMEOUT_MS = 20000  # a cloud server, plus loading the SDK on the first call


class ModelListRequest(AsyncCall):
    """start(url, api_key) -> succeeded(list[str]) with the server's list of models."""

    def timeout_ms(self, args) -> int:
        return ANTHROPIC_TIMEOUT_MS if args and llm.is_anthropic(args[0]) else self.TIMEOUT_MS

    def __init__(self, parent=None):
        super().__init__(llm.list_models, parent)


class ContextLimitRequest(AsyncCall):
    """start(url, api_key, model) -> succeeded((limit | None, model_loaded, max context | None))."""

    TIMEOUT_MS = 5000

    def timeout_ms(self, args) -> int:
        return ANTHROPIC_TIMEOUT_MS if args and llm.is_anthropic(args[0]) else self.TIMEOUT_MS

    def __init__(self, parent=None):
        super().__init__(llm.get_context_info, parent)


class ChatStreamer(QThread):
    """Reads the model's streamed reply in the background and passes chunks to the UI through signals.

    The loop itself is core.agent_loop.AgentLoop; this class turns its callbacks into signals.
    Every signal carries stream_id so that the UI can ignore stale streams.
    """

    delta = Signal(int, str, str)  # stream_id, kind ('reasoning' | 'content' | 'notice'), text
    tool_running = Signal(int, str, str)  # stream_id, name of the function being called, arguments (JSON string)
    tool_round = Signal(int, object, object)  # stream_id, the model message with the calls, tool results
    usage = Signal(int, int, int, int)  # stream_id, request tokens counted by the server, reply tokens, our request estimate
    failed = Signal(int, str)
    completed = Signal(int)
    progress = Signal(int, str)  # stream_id, a status line from a subagent working inside delegate

    MAX_STEPS = DEFAULT_MAX_STEPS  # the default; it is configurable per model profile

    def __init__(self, stream_id: int, base_url: str, api_key: str, model: str,
                 messages: list, temperature: float, max_tokens: int,
                 tools: list | None = None, router=None, max_steps: int | None = None,
                 context_limit: int | None = None, scale: float = 1.0, prepare=None):
        super().__init__()
        self.stream_id = stream_id
        self._messages = messages
        self._cancel = threading.Event()
        sid = stream_id
        self.agent = AgentLoop(
            base_url, api_key, model, temperature, max_tokens, tools=tools, router=router,
            max_steps=max_steps or self.MAX_STEPS, context_limit=context_limit, scale=scale, cancel=self._cancel,
            on_delta=lambda kind, text: self.delta.emit(sid, kind, text),
            on_tool_running=lambda name, args: self.tool_running.emit(sid, name, args),
            on_round=lambda assistant, results: self.tool_round.emit(sid, assistant, results),
            on_usage=lambda prompt, completion, raw: self.usage.emit(sid, prompt, completion, raw),
            log_tag=f"stream {stream_id}",
        )
        # prepare(messages, notify) -> messages: preparing the request in the thread before the first call to the model
        # (images: data URLs or a description from the vision model, which can take minutes)
        self._prepare = prepare

    # the context check lives in AgentLoop; these names stay for the callers and tests
    max_steps = property(lambda self: self.agent.max_steps)
    context_limit = property(lambda self: self.agent.context_limit)
    reserve = property(lambda self: self.agent.reserve)
    # main_window corrects the scale between rounds from the server's measurements
    scale = property(lambda self: self.agent.scale, lambda self, value: setattr(self.agent, "scale", value))

    def _estimate(self, messages: list, tools: list | None) -> tuple[int, int]:
        return self.agent.estimate(messages, tools)

    def _fits(self, messages: list, tools: list | None) -> bool:
        return self.agent.fits(messages, tools)

    def cancel(self):
        self._cancel.set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def run(self):
        try:
            messages = list(self._messages)
            if self._prepare is not None:
                messages = self._prepare(messages, lambda text: self.delta.emit(self.stream_id, "notice", text))
            if self._cancel.is_set():
                return
            if self.agent.run(messages):
                self.completed.emit(self.stream_id)
        except llm.LLMError as e:
            if not self._cancel.is_set():
                self.failed.emit(self.stream_id, str(e))
        except Exception as e:  # the thread must not die silently
            if not self._cancel.is_set():
                self.failed.emit(self.stream_id, f"Error: {e}")
