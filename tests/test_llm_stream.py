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
        {"choices": [{"delta": {"role": "assistant"}}]},          # пустая дельта с ролью
        d(reasoning_content="Думаю "), d(reasoning_content="про 17*3"),
        d(reasoning="альт. поле"),                                  # вариант с delta.reasoning
        d(content="Ответ: "), d(content="51"),
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": []},                                             # usage-чанк без choices
    ), headers={"content-type": "text/event-stream"})
out = run(ok_handler, key="k")
check("разделение reasoning и content", out == [("reasoning", "Думаю "), ("reasoning", "про 17*3"),
      ("reasoning", "альт. поле"), ("content", "Ответ: "), ("content", "51")])
check("URL и тело запроса", seen["url"] == "http://x/v1/chat/completions" and seen["body"]["stream"] is True
      and seen["body"]["model"] == "m" and seen["body"]["temperature"] == 0.5 and seen["body"]["max_tokens"] == 100)
check("ключ -> Bearer", seen["auth"] == "Bearer k")
run(ok_handler); check("без ключа нет заголовка", seen["auth"] is None)

out = run(lambda r: httpx.Response(200, content=b"data: not-json\n\n: comment\n\ndata: " +
        json.dumps(d(content="ok")).encode() + b"\n\n"))
check("мусорные строки пропускаются, поток без [DONE] заканчивается", out == [("content", "ok")])

def expect_err(handler, needle):
    try: run(handler); return False
    except llm.LLMError as e: return needle in str(e)
check("HTTP 400 с телом ошибки", expect_err(lambda r: httpx.Response(400, json={"error": {"message": "model not found"}}), "model not found"))
check("HTTP 400 с error-строкой", expect_err(lambda r: httpx.Response(400, json={"error": "bad"}), "bad"))
check("HTTP 401", expect_err(lambda r: httpx.Response(401, json={}), "отклонил ключ (401)"))
check("HTTP 500 без тела", expect_err(lambda r: httpx.Response(500, content=b"oops"), "Ошибка сервера: 500"))
check("ошибка внутри потока", expect_err(lambda r: httpx.Response(200, content=sse(d(content="a"), {"error": {"message": "out of memory"}})), "out of memory"))
def refuse(r): raise httpx.ConnectError("refused")
check("нет соединения", expect_err(refuse, "Нет связи"))

cancel = threading.Event(); got = []
def gen():
    real = httpx.stream
    llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(ok_handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
    try:
        for ev in llm.stream_chat("http://x/v1", "", "m", [], 0.5, 10, cancel):
            got.append(ev); cancel.set()
    finally: llm.httpx.stream = real
gen()
check("cancel обрывает поток после текущего чанка", len(got) == 1)

out = run(lambda r: httpx.Response(200, content=sse(
    {"model": "other", "choices": [{"delta": {"content": "a"}}]},
    {"model": "other", "choices": [{"delta": {}, "finish_reason": "length"}]})))
check("подмена модели и обрыв по лимиту -> notice (подмена один раз)",
      out == [("notice", "Ответила модель «other» вместо «m»"), ("content", "a"),
              ("notice", "Лимит max tokens исчерпан, ответ оборван")])
out = run(lambda r: httpx.Response(200, content=sse({"model": "m", "choices": [{"delta": {"content": "a"}, "finish_reason": "stop"}]})))
check("та же модель и stop -> без notice", out == [("content", "a")])

# context_builder
prof = {"system_prompt": " Ты помощник ", "memory": "Любит чай"}
hist = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1", "reasoning": "секрет"},
        {"role": "user", "content": "q2"}, {"role": "assistant", "content": ""}]
m = build_messages(prof, hist)
check("system = prompt + память", m[0] == {"role": "system", "content": "Ты помощник\n\nПамять:\nЛюбит чай"})
check("история без размышлений и пустых", [x["role"] for x in m[1:]] == ["user", "assistant", "user"]
      and all(set(x) == {"role", "content"} for x in m))
check("без system/памяти нет system-сообщения", build_messages({"system_prompt": "", "memory": ""}, hist[:1]) == [hist[0]])
print("ALL OK" if ok_all else "SOME FAILED")
