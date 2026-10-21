import json, threading
import httpx
from core import llm
from core.context_builder import build_messages

ok_all = True
def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name)

def sse(*chunks, done=True):
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    if done: lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()

def run(handler, **kw):
    real = httpx.stream
    def fake(method, url, **k):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return client.stream(method, url, json=k.get("json"), headers=k.get("headers"))
    llm.httpx.stream = fake
    try:
        return list(llm.stream_chat("http://x/v1", kw.get("key", ""), "m", [{"role": "user", "content": "hi"}], 0.5, 100, kw.get("cancel")))
    finally:
        llm.httpx.stream = real

d = lambda **delta: {"choices": [{"delta": delta}]}
seen = {}
def ok_handler(req):
    seen["body"] = json.loads(req.content); seen["auth"] = req.headers.get("authorization"); seen["url"] = str(req.url)
    return httpx.Response(200, content=sse(
        {"choices": [{"delta": {"role": "assistant"}}]},          # an empty delta with a role
        d(reasoning_content="Thinking "), d(reasoning_content="about 17*3"),
        d(reasoning="alt. field"),                                  # the delta.reasoning variant
        d(content="Answer: "), d(content="51"),
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": []},                                             # a usage chunk without choices
    ), headers={"content-type": "text/event-stream"})
out = run(ok_handler, key="k")
check("reasoning and content are separated", out == [("reasoning", "Thinking "), ("reasoning", "about 17*3"),
      ("reasoning", "alt. field"), ("content", "Answer: "), ("content", "51")])
check("URL and request body", seen["url"] == "http://x/v1/chat/completions" and seen["body"]["stream"] is True
      and seen["body"]["model"] == "m" and seen["body"]["temperature"] == 0.5 and seen["body"]["max_tokens"] == 100)
check("key -> Bearer", seen["auth"] == "Bearer k")
run(ok_handler); check("no header without a key", seen["auth"] is None)

out = run(lambda r: httpx.Response(200, content=b"data: not-json\n\n: comment\n\ndata: " +
        json.dumps(d(content="ok")).encode() + b"\n\n"))
check("junk lines are skipped, a stream without [DONE] ends", out == [("content", "ok")])

def expect_err(handler, needle):
    try: run(handler); return False
    except llm.LLMError as e: return needle in str(e)
check("HTTP 400 with an error body", expect_err(lambda r: httpx.Response(400, json={"error": {"message": "model not found"}}), "model not found"))
check("HTTP 400 with an error string", expect_err(lambda r: httpx.Response(400, json={"error": "bad"}), "bad"))
check("HTTP 401", expect_err(lambda r: httpx.Response(401, json={}), "rejected the key (401)"))
check("HTTP 500 without a body", expect_err(lambda r: httpx.Response(500, content=b"oops"), "Server error: 500"))
check("an error inside the stream", expect_err(lambda r: httpx.Response(200, content=sse(d(content="a"), {"error": {"message": "out of memory"}})), "out of memory"))
def refuse(r): raise httpx.ConnectError("refused")
check("no connection", expect_err(refuse, "Cannot reach"))

cancel = threading.Event(); got = []
def gen():
    real = httpx.stream
    llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(ok_handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
    try:
        for ev in llm.stream_chat("http://x/v1", "", "m", [], 0.5, 10, cancel):
            got.append(ev); cancel.set()
    finally: llm.httpx.stream = real
gen()
check("cancel ends the stream after the current chunk", len(got) == 1)

out = run(lambda r: httpx.Response(200, content=sse(
    {"model": "other", "choices": [{"delta": {"content": "a"}}]},
    {"model": "other", "choices": [{"delta": {}, "finish_reason": "length"}]})))
check("model substitution and a cut-off by the limit -> notice (substitution once)",
      out == [("notice", "Model \"other\" answered instead of \"m\""), ("content", "a"),
              ("notice", "Max tokens limit reached, the reply was cut off")])
out = run(lambda r: httpx.Response(200, content=sse({"model": "m", "choices": [{"delta": {"content": "a"}, "finish_reason": "stop"}]})))
check("the same model and stop -> no notice", out == [("content", "a")])

# context_builder
prof = {"system_prompt": " You are an assistant ", "memory": "Likes tea"}
hist = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1", "reasoning": "secret"},
        {"role": "user", "content": "q2"}, {"role": "assistant", "content": ""}]
m = build_messages(prof, hist)
check("system = prompt + memory", m[0] == {"role": "system", "content": "You are an assistant\n\nMemory:\nLikes tea"})
check("history without reasoning and empty messages", [x["role"] for x in m[1:]] == ["user", "assistant", "user"]
      and all(set(x) == {"role", "content"} for x in m))
check("without system/memory there is no system message", build_messages({"system_prompt": "", "memory": ""}, hist[:1]) == [hist[0]])
print("ALL OK" if ok_all else "SOME FAILED")
