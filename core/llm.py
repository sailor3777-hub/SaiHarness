import json
import threading
import uuid
from typing import Iterator

import httpx


LENGTH_NOTICE = "Max tokens limit reached, the reply was cut off"


class LLMError(Exception):
    """An error talking to the model server; the text can be shown to the user."""


def answer_in_reasoning(content: str, reasoning: str, notice: str = "") -> bool:
    """The model's answer is in the reasoning: content is empty and the reasoning is complete.

    Qwen 3.5 in LM Studio, for example, does this: the answer arrives as reasoning_content while content stays empty.
    If the reply was cut off by the token limit, it is unfinished reasoning and does not count as an answer.
    """
    return not content.strip() and bool(reasoning.strip()) and LENGTH_NOTICE not in notice


def _error_text(response: httpx.Response) -> str:
    """Extracts the error message from the body of an OpenAI-compatible server's reply."""
    code = response.status_code
    try:
        err = response.json().get("error")
        message = err.get("message") if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        message = None
    if code in (401, 403):
        return f"The server rejected the key ({code})"
    return f"Server error {code}: {message}" if message else f"Server error: {code}"


def get_context_info(base_url: str, api_key: str, model: str) -> tuple[int | None, bool, int | None]:
    """The model's context size in LM Studio: (working limit, whether the model is loaded, maximum).

    The working limit is known only for a loaded model (loaded_context_length): when loading
    "on the fly", LM Studio may set a context much smaller than the model's maximum.
    Any failure gives (None, False, None): estimating the limit must not break anything.
    """
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    try:
        r = httpx.get(
            f"{root}/api/v0/models", headers=_headers(api_key), timeout=4.0, trust_env=False
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            if isinstance(m, dict) and m.get("id") == model:
                maximum = m.get("max_context_length")
                if m.get("state") == "loaded" and m.get("loaded_context_length"):
                    return int(m["loaded_context_length"]), True, maximum
                return None, False, maximum
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    return None, False, None


def model_type(base_url: str, api_key: str, model: str, timeout: float = 4.0) -> str | None:
    """Model type in LM Studio: 'vlm' (sees images), 'llm' and so on. Any failure or a different server gives None."""
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    try:
        r = httpx.get(f"{root}/api/v0/models", headers=_headers(api_key), timeout=timeout, trust_env=False)
        r.raise_for_status()
        for m in r.json().get("data", []):
            if isinstance(m, dict) and m.get("id") == model:
                return m.get("type") if isinstance(m.get("type"), str) else None
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    return None


def _accumulate_tool_call(calls: dict[int, dict], tc: dict):
    """Assembles a tool call from stream chunks (the name and arguments arrive in fragments by index)."""
    if not isinstance(tc, dict):
        return
    idx = tc.get("index") if isinstance(tc.get("index"), int) else len(calls)
    entry = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        entry["id"] = tc["id"]
    fn = tc.get("function") or {}
    entry["name"] += fn.get("name") or ""
    args = fn.get("arguments")
    if isinstance(args, dict):  # some servers send an object instead of a string
        args = json.dumps(args, ensure_ascii=False)
    entry["arguments"] += args or ""
    if not entry["id"]:
        entry["id"] = f"call_{idx}_{uuid.uuid4().hex[:8]}"


def stream_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    cancel: threading.Event | None = None,
    tools: list[dict] | None = None,
) -> Iterator[tuple[str, object]]:
    """Streams the model's reply. Yields pairs ('reasoning' | 'content', text).

    If the model decided to call tools, the end of the stream yields ('tool_calls', list) of dicts
    {id, name, arguments} (arguments is a JSON string assembled from chunks). If the server reported how many tokens
    the request took, ('usage', {prompt_tokens, completion_tokens}) is yielded.

    Reasoning comes from delta.reasoning_content (LM Studio, DeepSeek) or delta.reasoning.
    If cancel is set, generation stops at the next chunk from the server.
    Warnings are yielded as ('notice', text): the server answered with a different model than requested
    (LM Studio substitutes the loaded one for an unknown name), or the reply was cut off by the token limit
    (for reasoning models the reasoning itself uses up the limit).
    """
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},  # the server will send the real token counts of the request and reply
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    # the first token may wait for the model to load or for a long prompt to be processed
    timeout = httpx.Timeout(10.0, read=300.0)
    checked_model = False
    calls: dict[int, dict] = {}
    try:
        with httpx.stream(
            "POST", f"{base_url}/chat/completions", json=body,
            headers=_headers(api_key), timeout=timeout, trust_env=False,
        ) as r:
            if r.status_code >= 400:
                r.read()
                raise LLMError(_error_text(r))
            for line in r.iter_lines():
                if cancel is not None and cancel.is_set():
                    return
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk:
                    err = chunk["error"]
                    raise LLMError(str(err.get("message") if isinstance(err, dict) else err))
                usage = chunk.get("usage")
                if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
                    yield "usage", {"prompt_tokens": usage["prompt_tokens"],
                                    "completion_tokens": int(usage.get("completion_tokens") or 0)}
                actual = chunk.get("model")
                if not checked_model and isinstance(actual, str):
                    checked_model = True
                    if actual != model:
                        yield "notice", f"Model \"{actual}\" answered instead of \"{model}\""
                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason") == "length":
                        yield "notice", LENGTH_NOTICE
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield "reasoning", reasoning
                    content = delta.get("content")
                    if content:
                        yield "content", content
                    for tc in delta.get("tool_calls") or []:
                        _accumulate_tool_call(calls, tc)
        if calls:
            yield "tool_calls", [calls[i] for i in sorted(calls)]
    except (httpx.ConnectError, httpx.TimeoutException):
        raise LLMError(f"Cannot reach {base_url}") from None
    except httpx.HTTPError as e:
        raise LLMError(f"Request error: {e}") from None


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_models(base_url: str, api_key: str = "", timeout: float = 5.0) -> list[str]:
    """Returns the model ids from an OpenAI-compatible server (GET {base_url}/models)."""
    try:
        r = httpx.get(
            f"{base_url}/models", headers=_headers(api_key), timeout=timeout, trust_env=False
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.ConnectError, httpx.TimeoutException):
        raise LLMError(f"Cannot reach {base_url}") from None
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            raise LLMError(f"The server rejected the key ({code})") from None
        raise LLMError(f"Server error: {code}") from None
    except httpx.HTTPError as e:
        raise LLMError(f"Request error: {e}") from None
    except ValueError:
        raise LLMError("Invalid server reply") from None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LLMError("Invalid server reply")
    return sorted(m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str))
