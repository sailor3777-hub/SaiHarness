"""The Anthropic connector offline: request conversion, streaming, tool calls, errors (mock HTTP transport)."""
import json
import sys
import threading

import anthropic
import httpx2

from core import anthropic_api as aa, llm
from core.agent_loop import AgentLoop
from core.connection import KINDS, normalize_url

ok_all = True
def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name)

URL = "https://api.anthropic.com/v1"


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def reply(blocks, stop="end_turn", model="claude-opus-5-5", usage=None):
    """A streamed Messages reply: blocks are ('thinking', text) | ('text', text) | ('tool', id, name, input)."""
    ev = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": usage or {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 5,
                           "cache_creation_input_tokens": 0}}}]
    for i, b in enumerate(blocks):
        if b[0] == "thinking":
            ev.append({"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b[1]}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "sig"}})
        elif b[0] == "text":
            ev.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b[1]}})
        else:
            ev.append({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b[1], "name": b[2], "input": {}}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b[3])}})
        ev.append({"type": "content_block_stop", "index": i})
    ev.append({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 7}})
    ev.append({"type": "message_stop"})
    return httpx2.Response(200, content=sse(ev), headers={"content-type": "text/event-stream"})


requests = []
def use(*responses):
    queue = list(responses)
    def handler(req):
        requests.append({"url": str(req.url), "headers": dict(req.headers),
                         "body": json.loads(req.content) if req.content else None})
        r = queue.pop(0)
        return r(req) if callable(r) else r
    aa.http_client_factory = lambda: anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    requests.clear()


# --- detection and addresses ---
check("anthropic.com is detected", aa.is_anthropic(URL) and not aa.is_anthropic("http://localhost:1234/v1")
      and not aa.is_anthropic("https://notanthropic.com/v1"))
check("Anthropic connection type, https address", "Anthropic" in KINDS
      and normalize_url("api.anthropic.com", KINDS["Anthropic"]) == URL
      and normalize_url("https://api.anthropic.com", KINDS["Anthropic"]) == URL
      and normalize_url("192.168.1.5", KINDS["LM Studio"]) == "http://192.168.1.5:1234/v1")
check("SDK address: /v1 and anything after it is cut", aa._sdk_base(URL) == "https://api.anthropic.com"
      and aa._sdk_base("https://api.anthropic.com/v1/messages") == "https://api.anthropic.com"
      and aa._sdk_base("https://proxy.example/anthropic/v1/") == "https://proxy.example/anthropic")
check("model versions", aa._version("claude-opus-5-5") == (5, 5) and aa._version("claude-opus-5") == (5, 0)
      and aa._version("claude-3-7-sonnet-20250219") == (3, 7) and aa._version("claude-haiku-4-5") == (4, 5))
check("adaptive thinking from 4.6", aa.adaptive_thinking("claude-sonnet-4-6") and not aa.adaptive_thinking("claude-haiku-4-5"))

# --- request conversion ---
history = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": [{"type": "text", "text": "what is here?"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                                 {"type": "image_url", "image_url": {"url": "sai-image:C:/x.png"}}]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call.1", "type": "function", "function": {"name": "find_tool", "arguments": '{"query": "files"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "call_tool", "arguments": "not json"}}]},
    {"role": "tool", "tool_call_id": "call.1", "content": "found"},
    {"role": "tool", "tool_call_id": "call_2", "content": "ERROR: bad arguments"},
    {"role": "user", "content": "go on"},
    {"role": "assistant", "content": ""},  # an empty answer from old history
]
system, msgs = aa.convert_messages(history)
check("system goes to the system field", system == "Be brief." and msgs[0]["role"] == "user")
check("image -> base64 block, placeholder dropped", msgs[0]["content"][1] == {
    "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}} and len(msgs[0]["content"]) == 2)
tu = msgs[1]["content"]
check("tool calls -> tool_use with safe ids", [b["type"] for b in tu] == ["tool_use", "tool_use"]
      and tu[0]["id"] == "call_1" and tu[0]["input"] == {"query": "files"} and tu[1]["input"] == {})
res = msgs[2]
check("results of one round + next user text in one message", res["role"] == "user"
      and [b["type"] for b in res["content"]] == ["tool_result", "tool_result", "text"]
      and res["content"][0]["tool_use_id"] == "call_1" and res["content"][1].get("is_error") is True
      and "is_error" not in res["content"][0])
check("empty assistant turn is skipped", len(msgs) == 3)
tools = [{"type": "function", "function": {"name": "find_tool", "description": "d", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]
check("tools -> input_schema", aa.convert_tools(tools) == [{"name": "find_tool", "description": "d",
      "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}])
check("fallback boundary: only text before it", aa.echo_blocks([
    {"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "part"},
    {"type": "tool_use", "id": "a", "name": "x", "input": {}}, {"type": "fallback"}, {"type": "text", "text": "rest"}])
    == [{"type": "text", "text": "part"}, {"type": "text", "text": "rest"}])

# --- streaming a plain answer ---
use(reply([("thinking", "hmm"), ("text", "Hello")]))
out = list(llm.stream_chat(URL, "sk-test", "claude-opus-5-5", [{"role": "system", "content": "S"},
                                                                 {"role": "user", "content": "hi"}], 0.7, 1000))
body, headers = requests[0]["body"], requests[0]["headers"]
check("reasoning and content", ("reasoning", "hmm") in out and ("content", "Hello") in out)
check("usage counts cached tokens", ("usage", {"prompt_tokens": 15, "completion_tokens": 7}) in out)
check("no tool_calls for a plain answer", not any(k in ("tool_calls", llm.PROVIDER_BLOCKS) for k, _ in out))
check("request: /v1/messages, key header", requests[0]["url"].startswith("https://api.anthropic.com/v1/messages")
      and "sk-test" in (headers.get("x-api-key", "") + headers.get("authorization", "")))
check("request body: system, no temperature, adaptive thinking, cache", body["system"] == "S" and "temperature" not in body
      and body["thinking"] == {"type": "adaptive", "display": "summarized"} and body["max_tokens"] == 1000
      and body["cache_control"] == {"type": "ephemeral"} and body["stream"] is True)
check("Opus 5.5: default safety fallback", body.get("fallbacks") == "default"
      and aa.FALLBACK_BETA in headers.get("anthropic-beta", ""))

use(reply([("text", "x")], model="claude-haiku-4-5"))
list(llm.stream_chat(URL, "k", "claude-haiku-4-5", [{"role": "user", "content": "hi"}], 0.7, 100))
check("old model: no thinking, no fallback", "thinking" not in requests[0]["body"] and "fallbacks" not in requests[0]["body"])

use(reply([("text", "cut")], stop="max_tokens"))
out = list(llm.stream_chat(URL, "k", "claude-opus-5-5", [{"role": "user", "content": "hi"}], 0.7, 10))
check("max_tokens -> length notice", ("notice", llm.LENGTH_NOTICE) in out)
use(reply([], stop="refusal"))
out = list(llm.stream_chat(URL, "k", "claude-opus-5-5", [{"role": "user", "content": "hi"}], 0.7, 10))
check("refusal -> notice", any(k == "notice" and "declined" in v for k, v in out))
use(reply([("text", "x")], model="claude-opus-5"))
out = list(llm.stream_chat(URL, "k", "claude-opus-5-5", [{"role": "user", "content": "hi"}], 0.7, 10))
check("another model answered -> notice", any(k == "notice" and "claude-opus-5" in v for k, v in out))

# --- errors ---
def expect_err(response, needle, key="k"):
    use(response)
    try:
        list(llm.stream_chat(URL, key, "claude-opus-5-5", [{"role": "user", "content": "hi"}], 0.7, 10))
        return False
    except llm.LLMError as e:
        return needle in str(e)
err = lambda code, kind, msg: httpx2.Response(code, json={"type": "error", "error": {"type": kind, "message": msg}})
check("401 -> key rejected", expect_err(err(401, "authentication_error", "invalid x-api-key"), "rejected the key (401)"))
check("400 -> message shown", expect_err(err(400, "invalid_request_error", "bad model"), "bad model"))
check("no key -> clear error", expect_err(err(500, "x", "y"), "needs an API key", key=""))
def refuse(_req):
    raise httpx2.ConnectError("refused")
use(refuse, refuse, refuse)  # the SDK retries twice before giving up
try:
    list(llm.stream_chat(URL, "k", "claude-opus-5-5", [{"role": "user", "content": "hi"}], 0.7, 10)); got = ""
except llm.LLMError as e:
    got = str(e)
check("connection error after retries", "Cannot reach" in got)

# --- the agent loop: thinking blocks go back unchanged inside a tool round ---
class Router:
    stuck = None
    def handle(self, name, args):
        return f"result of {name} {args}"
use(reply([("thinking", "need a tool"), ("text", "Looking."), ("tool", "toolu_1", "find_tool", {"query": "x"})], stop="tool_use"),
    reply([("text", "Done.")]))
rounds = []
loop = AgentLoop(URL, "k", "claude-opus-5-5", 0.7, 1000, tools=tools, router=Router(),
                 on_round=lambda a, r: rounds.append((a, r)))
check("loop finishes", loop.run([{"role": "user", "content": "find x"}]) and loop.content == "Done.")
second = requests[1]["body"]["messages"]
check("second request: assistant turn with the original thinking block", second[1]["role"] == "assistant"
      and second[1]["content"][0]["type"] == "thinking" and second[1]["content"][0]["signature"] == "sig"
      and second[1]["content"][2]["type"] == "tool_use" and second[1]["content"][2]["id"] == "toolu_1")
check("second request: tool_result for the call", second[2]["content"][0]["type"] == "tool_result"
      and second[2]["content"][0]["tool_use_id"] == "toolu_1" and "find_tool" in second[2]["content"][0]["content"])
check("provider blocks do not reach the saved round", llm.PROVIDER_BLOCKS not in rounds[0][0])

# --- OpenAI servers never see the provider key ---
import httpx
seen = {}
def fake_stream(method, url, **k):
    seen["body"] = k.get("json")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"data: [DONE]\n\n")))
    return client.stream(method, url, json=k.get("json"))
real = llm.httpx.stream
llm.httpx.stream = fake_stream
try:
    list(llm.stream_chat("http://localhost:1234/v1", "", "m", [{"role": "assistant", "content": "a", llm.PROVIDER_BLOCKS: [1]}], 0.5, 10))
finally:
    llm.httpx.stream = real
check("provider key stripped for LM Studio", llm.PROVIDER_BLOCKS not in seen["body"]["messages"][0])

# --- models and context ---
use(httpx2.Response(200, json={"data": [{"type": "model", "id": "claude-sonnet-5-5", "display_name": "S", "created_at": "2026-01-01T00:00:00Z"},
                                        {"type": "model", "id": "claude-opus-5-5", "display_name": "O", "created_at": "2026-01-01T00:00:00Z"}],
                               "has_more": False, "first_id": "a", "last_id": "b"}))
check("model list", llm.list_models(URL, "k") == ["claude-opus-5-5", "claude-sonnet-5-5"])
use(httpx2.Response(200, json={"type": "model", "id": "claude-opus-5-5", "display_name": "O",
                               "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1000000, "max_tokens": 128000}))
check("context window from the Models API", llm.get_context_info(URL, "k", "claude-opus-5-5") == (1000000, True, 1000000))
use(err(404, "not_found_error", "no"))
check("context failure -> unknown", llm.get_context_info(URL, "k", "x") == (None, False, None))
check("Claude sees images", llm.model_type(URL, "k", "claude-opus-5-5") == "vlm")

aa.http_client_factory = None
print("ALL OK" if ok_all else "SOME FAILED")
