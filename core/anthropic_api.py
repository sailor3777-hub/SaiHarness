"""Claude models through the Anthropic Messages API (official `anthropic` SDK), no Qt.

The rest of the harness speaks the OpenAI chat format. core/llm.py sends a connection here when its address is
on anthropic.com (`is_anthropic`); this module converts the request (system messages -> `system`, tool calls ->
`tool_use`, tool results -> `tool_result`, data URLs -> image blocks) and turns the stream back into the same
('reasoning' | 'content' | 'notice' | 'usage' | 'tool_calls', ...) pairs as llm.stream_chat.

Thinking blocks must go back unchanged inside one tool loop, so the assistant turn that called tools is also
yielded as ('provider_blocks', [...]); AgentLoop stores it in the assistant message under PROVIDER_BLOCKS and this
module sends those blocks instead of rebuilding them. OpenAI servers never see that key (llm strips it).
"""
import json
import re
import threading
from typing import Iterator
from urllib.parse import urlsplit

from core import markers

DEFAULT_URL = "https://api.anthropic.com"
PROVIDER_BLOCKS = "provider_blocks"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# models that take server-side `fallbacks: "default"` (a safety decline is re-run on another model)
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")

http_client_factory = None  # tests put a client with a mock transport here


class AnthropicError(Exception):
    """A readable error; llm turns it into LLMError."""


def is_anthropic(base_url: str) -> bool:
    host = (urlsplit(base_url or "").hostname or "").lower()
    return host == "anthropic.com" or host.endswith(".anthropic.com")


def _sdk_base(base_url: str) -> str:
    """The SDK adds /v1/... itself. Connections store the address with /v1 (normalize_url), and a pasted
    endpoint such as https://api.anthropic.com/v1/messages is cut at /v1 too."""
    base = (base_url or DEFAULT_URL).rstrip("/")
    cut = re.search(r"/v1(/|$)", base)
    return base[:cut.start()] if cut else base


def preload():
    """Imports the SDK in the background (≈2-3 s, longer from a cold disk) so the first check or reply does not wait."""
    def load():
        try:
            import anthropic  # noqa: F401
        except ImportError:
            pass
    threading.Thread(target=load, name="anthropic-preload", daemon=True).start()


def _client(base_url: str, api_key: str):
    import anthropic
    if not api_key:
        raise AnthropicError("An Anthropic connection needs an API key: set it on the Connection tab.")
    return anthropic.Anthropic(
        api_key=api_key, base_url=_sdk_base(base_url), max_retries=2,
        timeout=anthropic.Timeout(300.0, connect=10.0),  # a long first token: thinking before the answer
        http_client=http_client_factory() if http_client_factory else None,
    )


def _version(model: str) -> tuple[int, int]:
    """claude-opus-4-6 -> (4, 6), claude-opus-5 -> (5, 0), claude-3-7-sonnet-20250219 -> (3, 7); unknown -> (99, 0)."""
    m = re.match(r"claude-(?:[a-z]+-)?(\d+)(?:-(\d{1,2}))?(?=-|$)", model or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (99, 0)


def adaptive_thinking(model: str) -> bool:
    """Adaptive thinking exists from the 4.6 models on; older ones (Haiku 4.5 and so on) answer without thinking."""
    return _version(model) >= (4, 6)


def _fallback(model: str) -> bool:
    return model in FALLBACK_MODELS


# --- request conversion ---

def _safe_id(tool_id: str) -> str:
    """Anthropic accepts tool ids of [A-Za-z0-9_-]; ids from other servers in the history may have other characters."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(tool_id or "")) or "call"


def _image_block(url: str):
    if url.startswith("data:") and "," in url:
        header, data = url.split(",", 1)
        return {"type": "image", "source": {"type": "base64", "media_type": header[5:].split(";")[0], "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None  # an unprepared placeholder: nothing to send


def _blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and (part.get("text") or "").strip():
            out.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            block = _image_block((part.get("image_url") or {}).get("url") or "")
            if block:
                out.append(block)
    return out


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text") or "" for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI messages -> (system text, Anthropic messages). Neighbouring turns of one role are merged,
    so all results of one tool round arrive in a single user message, as the API expects."""
    system, out = [], []

    def push(role, blocks):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            if _text(m.get("content")).strip():
                system.append(_text(m.get("content")))
        elif role == "user":
            push("user", _blocks(m.get("content")))
        elif role == "assistant":
            if m.get(PROVIDER_BLOCKS):  # this turn came from Claude in the current loop: send it unchanged
                push("assistant", m[PROVIDER_BLOCKS])
                continue
            blocks = _blocks(m.get("content"))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": _safe_id(tc.get("id")), "name": fn.get("name") or "",
                               "input": args if isinstance(args, dict) else {}})
            push("assistant", blocks)
        elif role == "tool":
            text = str(m.get("content") or "")
            block = {"type": "tool_result", "tool_use_id": _safe_id(m.get("tool_call_id")), "content": text or "(empty)"}
            if markers.is_error(text):
                block["is_error"] = True
            push("user", [block])
    return "\n\n".join(system), out


def convert_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function") or t
        out.append({"name": fn.get("name") or "", "description": fn.get("description") or "",
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def echo_blocks(content: list[dict]) -> list[dict]:
    """The assistant turn as it is sent back. After a fallback in the middle of a reply only the text of the
    declined part stays before the boundary (its thinking and tool calls are dropped), the marker itself is dropped."""
    last = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    before = [b for b in content[:last] if b.get("type") == "text"] if last >= 0 else []
    return before + content[last + 1:]


# --- calls ---

def _error(e: Exception, base_url: str) -> AnthropicError:
    import anthropic
    if isinstance(e, AnthropicError):
        return e
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return AnthropicError(f"The server rejected the key ({e.status_code})")
    if isinstance(e, anthropic.NotFoundError):
        return AnthropicError(f"Not found (404): {e.message}")
    if isinstance(e, anthropic.RateLimitError):
        return AnthropicError("Anthropic rate limit (429): wait a little and try again")
    if isinstance(e, anthropic.APIStatusError):
        return AnthropicError(f"Server error {e.status_code}: {e.message}")
    if isinstance(e, anthropic.APITimeoutError):
        return AnthropicError(f"{base_url} did not answer in time")
    if isinstance(e, anthropic.APIConnectionError):
        return AnthropicError(f"Cannot reach {base_url}")
    return AnthropicError(f"Request error: {e.__class__.__name__}: {e}")


def stream_chat(base_url: str, api_key: str, model: str, messages: list[dict], max_tokens: int,
                cancel: threading.Event | None = None, tools: list[dict] | None = None,
                length_notice: str = "") -> Iterator[tuple[str, object]]:
    """Same contract as llm.stream_chat. Temperature is not sent: current Claude models reject it."""
    import anthropic
    system, msgs = convert_messages(messages)
    params = {"model": model, "max_tokens": max_tokens, "messages": msgs,
              "cache_control": {"type": "ephemeral"}}  # the agent loop resends the same prefix: cache it
    if system:
        params["system"] = system
    if tools:
        params["tools"] = convert_tools(tools)
    if adaptive_thinking(model):
        params["thinking"] = {"type": "adaptive", "display": "summarized"}
    try:
        with _client(base_url, api_key) as client:
            if _fallback(model):
                api = client.beta.messages
                params.update(betas=[FALLBACK_BETA], fallbacks="default")
            else:
                api = client.messages
            with api.stream(**params) as stream:
                for event in stream:
                    if cancel is not None and cancel.is_set():
                        return
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta" and delta.text:
                            yield "content", delta.text
                        elif delta.type == "thinking_delta" and delta.thinking:
                            yield "reasoning", delta.thinking
                final = stream.get_final_message()
    except anthropic.AnthropicError as e:
        raise _error(e, base_url) from None
    except AnthropicError:
        raise

    usage = final.usage
    if usage is not None:
        prompt = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        yield "usage", {"prompt_tokens": prompt, "completion_tokens": usage.output_tokens or 0}
    if final.model and final.model != model:
        yield "notice", f"Model \"{final.model}\" answered instead of \"{model}\" (safety fallback)"
    if final.stop_reason == "max_tokens":
        yield "notice", length_notice
    elif final.stop_reason == "refusal":
        details = getattr(final, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        yield "notice", "Claude declined the request" + (f" ({category})" if category else "")
    content = echo_blocks(final.to_dict(mode="json").get("content") or [])
    calls = [{"id": b["id"], "name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}
             for b in content if b.get("type") == "tool_use"]
    if calls:
        yield PROVIDER_BLOCKS, content
        yield "tool_calls", calls


def list_models(base_url: str, api_key: str) -> list[str]:
    import anthropic
    try:
        with _client(base_url, api_key) as client:
            return sorted(m.id for m in client.models.list())
    except anthropic.AnthropicError as e:
        raise _error(e, base_url) from None


def context_info(base_url: str, api_key: str, model: str) -> tuple[int | None, bool, int | None]:
    """(context window, True, the same) from the Models API; any failure gives (None, False, None)."""
    try:
        with _client(base_url, api_key) as client:
            limit = getattr(client.models.retrieve(model), "max_input_tokens", None)
    except Exception:
        return None, False, None
    return (int(limit), True, int(limit)) if isinstance(limit, int) and limit > 0 else (None, False, None)
