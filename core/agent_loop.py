"""The agent loop without Qt: request -> tool calls -> repeat, then a forced summary without tools.

ChatStreamer (worker.py) runs it for the chat and turns the callbacks into Qt signals; a subagent
(core/subagent.py) runs the same loop inside a delegate call with its own callbacks.
"""
import threading

from core import diag, llm
from core.context_builder import estimate_request_tokens
from core.db import DEFAULT_MAX_STEPS

CONTEXT_SAFETY = 1.15  # margin for token estimate error (on average it underestimates code by 15-20%)

LIMIT_NUDGE = ("You have used up the tool call limit and cannot call tools any more. "
               "Reply to the user in plain text: what has been done and what is still left to do.")
STUCK_NUDGE = ("The tool {tool} returned the same error several times in a row, so tools are now disabled. "
               "Reply to the user in plain text: what worked, what did not, and why.")


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _skip(*_args):
    pass


class AgentLoop:
    """One reply of a model with tools. run() returns True when the reply is complete, False when it was cancelled;
    model errors and context overflow raise llm.LLMError.

    Callbacks: on_delta(kind, text) with kind 'reasoning' | 'content' | 'notice'; on_tool_running(name, arguments);
    on_round(assistant, results) after every finished tool round; on_usage(prompt_tokens, completion_tokens, raw_estimate).
    After run(), content and reasoning hold the text of the last request (the final answer).
    """

    MAX_STEPS = DEFAULT_MAX_STEPS

    def __init__(self, base_url: str, api_key: str, model: str, temperature: float, max_tokens: int,
                 tools: list | None = None, router=None, max_steps: int | None = None,
                 context_limit: int | None = None, scale: float = 1.0, cancel: threading.Event | None = None,
                 on_delta=None, on_tool_running=None, on_round=None, on_usage=None, log_tag: str = ""):
        self.base_url, self.api_key, self.model = base_url, api_key, model
        self.temperature, self.max_tokens = temperature, max_tokens
        self.tools, self.router = tools, router
        self.max_steps = max_steps or self.MAX_STEPS  # how many "request, tool calls" rounds are allowed per reply
        # protection against overflowing the model's context: the limit is known only for a loaded model
        self.context_limit = context_limit
        self.reserve = min(max_tokens, context_limit // 4) if context_limit else 0  # room for the reply
        self.scale = scale  # correction of our estimate based on the server's measurements
        self.cancel = cancel or threading.Event()
        self.on_delta = on_delta or _skip
        self.on_tool_running = on_tool_running or _skip
        self.on_round = on_round or _skip
        self.on_usage = on_usage or _skip
        self.log_tag = log_tag
        self.content = self.reasoning = ""

    # --- context check ---

    def estimate(self, messages: list, tools: list | None) -> tuple[int, int]:
        """(raw request estimate, estimate with the correction and the safety margin)."""
        raw = estimate_request_tokens(messages, tools)
        return raw, int(raw * self.scale * CONTEXT_SAFETY)

    def fits(self, messages: list, tools: list | None) -> bool:
        return not self.context_limit or self.estimate(messages, tools)[1] + self.reserve <= self.context_limit

    def overflow_error(self, messages: list, tools: list | None) -> llm.LLMError:
        estimated = self.estimate(messages, tools)[1]
        return llm.LLMError(
            f"The model's context is full: the request ≈{_fmt(estimated)} tok. and a reply of up to {_fmt(self.reserve)} do not fit "
            f"into {_fmt(self.context_limit)}. Start a new chat or remove files and tools."
        )

    # --- the loop ---

    def _request(self, messages: list, tools: list | None):
        """One request to the model: context check, a log entry, real token measurements from the server."""
        raw, estimated = self.estimate(messages, tools)
        diag.note(f"[req] {self.log_tag} request≈{estimated} tok. (raw estimate {raw}) limit={self.context_limit} "
                  f"{diag.memory_summary()}")
        if not self.fits(messages, tools):
            raise self.overflow_error(messages, tools)
        for kind, payload in llm.stream_chat(
            self.base_url, self.api_key, self.model, messages, self.temperature, self.max_tokens,
            cancel=self.cancel, tools=tools,
        ):
            if kind == "usage":
                self.on_usage(payload["prompt_tokens"], payload["completion_tokens"], raw)
                continue
            yield kind, payload

    def run(self, messages: list) -> bool:
        messages = list(messages)
        use_tools = bool(self.tools and self.router)
        stuck = None
        for _step in range(self.max_steps):
            self.content = self.reasoning = ""
            calls = None
            for kind, payload in self._request(messages, self.tools if use_tools else None):
                if self.cancel.is_set():
                    return False
                if kind == "tool_calls":
                    calls = payload
                    continue
                if kind == "content":
                    self.content += payload
                elif kind == "reasoning":
                    self.reasoning += payload
                self.on_delta(kind, payload)
            if self.cancel.is_set():
                return False
            if not calls or not use_tools:
                return True
            api_calls = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls
            ]
            results = []
            for c in calls:
                if self.cancel.is_set():
                    return False
                self.on_tool_running(c["name"], c["arguments"])
                results.append({
                    "tool_call_id": c["id"], "name": c["name"],
                    "content": self.router.handle(c["name"], c["arguments"]),
                })
            if self.cancel.is_set():
                return False  # the round is not finished: calls without results are not saved
            messages.append({"role": "assistant", "content": self.content, "tool_calls": api_calls})
            messages += [{"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]} for r in results]
            self.on_round({"content": self.content, "reasoning": self.reasoning, "tool_calls": api_calls}, results)
            stuck = getattr(self.router, "stuck", None)  # a tool blocked because of a repeating error
            if stuck:
                break
        # The step limit (or a loop on a broken tool). Without a final word the user would be left
        # with nothing, so the model is asked to sum up without tools. This message does not go into the chat history.
        # (Context overflow is caught by _request: it stops the reply with a clear error before any request
        # that would not fit.)
        if stuck:
            self.on_delta("notice", f"Tool {stuck} keeps returning the same error, continuing without tools")
            messages.append({"role": "user", "content": STUCK_NUDGE.format(tool=stuck)})
        else:
            self.on_delta("notice", f"Reached the tool step limit ({self.max_steps})")
            messages.append({"role": "user", "content": LIMIT_NUDGE})
        self.content = self.reasoning = ""
        for kind, payload in self._request(messages, None):
            if self.cancel.is_set():
                return False
            if kind == "content":
                self.content += payload
            elif kind == "reasoning":
                self.reasoning += payload
            if kind in ("content", "reasoning", "notice"):
                self.on_delta(kind, payload)
        return not self.cancel.is_set()
