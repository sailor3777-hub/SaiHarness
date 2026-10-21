import os
import threading

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from core import diag, llm
from core.context_builder import estimate_request_tokens
from core.db import DEFAULT_MAX_STEPS

CONTEXT_SAFETY = 1.15  # margin for token estimate error (on average it underestimates code by 15-20%)


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


LIMIT_NUDGE = ("You have used up the tool call limit and cannot call tools any more. "
               "Reply to the user in plain text: what has been done and what is still left to do.")
STUCK_NUDGE = ("The tool {tool} returned the same error several times in a row, so tools are now disabled. "
               "Reply to the user in plain text: what worked, what did not, and why.")


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
        self._watchdog.start(self.TIMEOUT_MS)
        t.start()

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
        self.failed.emit(self.TIMEOUT_TEXT.format(seconds=self.TIMEOUT_MS / 1000))

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


class ModelListRequest(AsyncCall):
    """start(url, api_key) -> succeeded(list[str]) with the server's list of models."""

    def __init__(self, parent=None):
        super().__init__(llm.list_models, parent)


class ContextLimitRequest(AsyncCall):
    """start(url, api_key, model) -> succeeded((limit | None, model_loaded, max context | None))."""

    TIMEOUT_MS = 5000

    def __init__(self, parent=None):
        super().__init__(llm.get_context_info, parent)


class ChatStreamer(QThread):
    """Reads the model's streamed reply in the background and passes chunks to the UI through signals.

    Every signal carries stream_id so that the UI can ignore stale streams.
    """

    delta = Signal(int, str, str)  # stream_id, kind ('reasoning' | 'content' | 'notice'), text
    tool_running = Signal(int, str, str)  # stream_id, name of the function being called, arguments (JSON string)
    tool_round = Signal(int, object, object)  # stream_id, the model message with the calls, tool results
    usage = Signal(int, int, int, int)  # stream_id, request tokens counted by the server, reply tokens, our request estimate
    failed = Signal(int, str)
    completed = Signal(int)

    MAX_STEPS = DEFAULT_MAX_STEPS  # the default; it is configurable per model profile

    def __init__(self, stream_id: int, base_url: str, api_key: str, model: str,
                 messages: list, temperature: float, max_tokens: int,
                 tools: list | None = None, router=None, max_steps: int | None = None,
                 context_limit: int | None = None, scale: float = 1.0, prepare=None):
        super().__init__()
        self.stream_id = stream_id
        self._args = (base_url, api_key, model, messages, temperature, max_tokens)
        self._tools, self._router = tools, router
        self.max_steps = max_steps or self.MAX_STEPS  # how many "request, tool calls" rounds are allowed per reply
        # protection against overflowing the model's context: the limit is known only for a loaded model
        self.context_limit = context_limit
        self.reserve = min(max_tokens, context_limit // 4) if context_limit else 0  # room for the reply
        self.scale = scale  # correction of our estimate based on the server's measurements
        self._cancel = threading.Event()
        # prepare(messages, notify) -> messages: preparing the request in the thread before the first call to the model
        # (images: data URLs or a description from the vision model, which can take minutes)
        self._prepare = prepare

    def cancel(self):
        self._cancel.set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def run(self):
        try:
            if self._prepare is not None:
                prepared = self._prepare(
                    list(self._args[3]), lambda text: self.delta.emit(self.stream_id, "notice", text))
                self._args = self._args[:3] + (prepared,) + self._args[4:]
            if self._cancel.is_set():
                return
            self._loop()
        except llm.LLMError as e:
            if not self._cancel.is_set():
                self.failed.emit(self.stream_id, str(e))
        except Exception as e:  # the thread must not die silently
            if not self._cancel.is_set():
                self.failed.emit(self.stream_id, f"Error: {e}")

    def _estimate(self, messages: list, tools: list | None) -> tuple[int, int]:
        """(raw request estimate, estimate with the correction and the safety margin)."""
        raw = estimate_request_tokens(messages, tools)
        return raw, int(raw * self.scale * CONTEXT_SAFETY)

    def _fits(self, messages: list, tools: list | None) -> bool:
        return not self.context_limit or self._estimate(messages, tools)[1] + self.reserve <= self.context_limit

    def _overflow_error(self, messages: list, tools: list | None) -> llm.LLMError:
        estimated = self._estimate(messages, tools)[1]
        return llm.LLMError(
            f"The model's context is full: the request ≈{_fmt(estimated)} tok. and a reply of up to {_fmt(self.reserve)} do not fit "
            f"into {_fmt(self.context_limit)}. Start a new chat or remove files and tools."
        )

    def _request(self, messages: list, tools: list | None):
        """One request to the model: context check, a log entry, real token measurements from the server."""
        base_url, api_key, model, _, temperature, max_tokens = self._args
        raw, estimated = self._estimate(messages, tools)
        diag.note(f"[req] stream {self.stream_id} request≈{estimated} tok. (raw estimate {raw}) limit={self.context_limit} "
                  f"{diag.memory_summary()}")
        if not self._fits(messages, tools):
            raise self._overflow_error(messages, tools)
        for kind, payload in llm.stream_chat(
            base_url, api_key, model, messages, temperature, max_tokens, cancel=self._cancel, tools=tools,
        ):
            if kind == "usage":
                self.usage.emit(self.stream_id, payload["prompt_tokens"], payload["completion_tokens"], raw)
                continue
            yield kind, payload

    def _loop(self):
        messages = list(self._args[3])
        use_tools = bool(self._tools and self._router)
        stuck = None
        for _step in range(self.max_steps):
            content = reasoning = ""
            calls = None
            for kind, payload in self._request(messages, self._tools if use_tools else None):
                if self._cancel.is_set():
                    return
                if kind == "tool_calls":
                    calls = payload
                    continue
                if kind == "content":
                    content += payload
                elif kind == "reasoning":
                    reasoning += payload
                self.delta.emit(self.stream_id, kind, payload)
            if self._cancel.is_set():
                return
            if not calls or not use_tools:
                self.completed.emit(self.stream_id)
                return
            api_calls = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls
            ]
            results = []
            for c in calls:
                if self._cancel.is_set():
                    return
                self.tool_running.emit(self.stream_id, c["name"], c["arguments"])
                results.append({
                    "tool_call_id": c["id"], "name": c["name"],
                    "content": self._router.handle(c["name"], c["arguments"]),
                })
            if self._cancel.is_set():
                return  # the round is not finished: calls without results are not saved
            messages.append({"role": "assistant", "content": content, "tool_calls": api_calls})
            messages += [{"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]} for r in results]
            self.tool_round.emit(
                self.stream_id, {"content": content, "reasoning": reasoning, "tool_calls": api_calls}, results
            )
            stuck = getattr(self._router, "stuck", None)  # a tool blocked because of a repeating error
            if stuck:
                break
        # The step limit (or a loop on a broken tool). Without a final word the user would be left
        # with nothing, so the model is asked to sum up without tools. This message does not go into the chat history.
        # (Context overflow is caught by _request: it stops the reply with a clear error before any request
        # that would not fit.)
        if stuck:
            self.delta.emit(self.stream_id, "notice", f"Tool {stuck} keeps returning the same error, continuing without tools")
            messages.append({"role": "user", "content": STUCK_NUDGE.format(tool=stuck)})
        else:
            self.delta.emit(self.stream_id, "notice", f"Reached the tool step limit ({self.max_steps})")
            messages.append({"role": "user", "content": LIMIT_NUDGE})
        for kind, payload in self._request(messages, None):
            if self._cancel.is_set():
                return
            if kind in ("content", "reasoning", "notice"):
                self.delta.emit(self.stream_id, kind, payload)
        if not self._cancel.is_set():
            self.completed.emit(self.stream_id)
