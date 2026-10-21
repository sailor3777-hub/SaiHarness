"""Context overflow protection: server usage, estimate correction, checks before sending and between rounds, the log."""
import json, os, pathlib, sys, tempfile, time
import httpx
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from core import db, diag, llm
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t43.db"
from core.context_builder import estimate_request_tokens
from core.tools import META_TOOLS, Catalog, ToolRouter
from worker import ChatStreamer, CONTEXT_SAFETY

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
app = QApplication([])
def wait(cond, timeout=40.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()
def sse(*c): return ("".join(f"data: {json.dumps(x, ensure_ascii=False)}\n\n" for x in c) + "data: [DONE]\n\n").encode()
def tc(i, n, a): return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": i, "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}]}}]}
def usage(prompt, completion=5): return {"model": "m", "choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}
class LM:
    def __init__(self): self.steps = []; self.requests = []
    def handler(self, r): self.requests.append(json.loads(r.content)); return httpx.Response(200, content=sse(*self.steps.pop(0)))
lm = LM(); real_stream = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))

# ---- usage from the stream ----
lm.steps = [[{"choices": [{"delta": {"content": "ok"}}]}, usage(1234, 7)]]
out = list(llm.stream_chat("http://x/v1", "", "m", [{"role": "user", "content": "q"}], 0.5, 10))
check("stream_chat yields the server's usage", ("usage", {"prompt_tokens": 1234, "completion_tokens": 7}) in out and ("content", "ok") in out, out)
check("the request asks for usage (stream_options)", lm.requests[0]["stream_options"] == {"include_usage": True})
lm.steps = [[{"choices": [{"delta": {"content": "ok"}}]}]]
check("server without usage: simply no event", [k for k, _ in llm.stream_chat("http://x/v1", "", "m", [], 0.5, 10)] == ["content"])
lm.steps = [[{"choices": [{"delta": {"content": "a"}}], "usage": None}, {"model": "m", "choices": [], "usage": {"prompt_tokens": "many"}}]]
check("garbage in usage is ignored", [k for k, _ in llm.stream_chat("http://x/v1", "", "m", [], 0.5, 10)] == ["content"])

# ---- ChatStreamer: context check ----
def run_stream(limit, messages, *, scale=1.0, max_tokens=100, tools=None, router=None, steps=(), max_steps=None, wait_s=20):
    lm.steps = [list(s) for s in steps]; lm.requests = []
    th = ChatStreamer(5, "http://x/v1", "", "m", messages, 0.1, max_tokens, tools=tools, router=router, max_steps=max_steps, context_limit=limit, scale=scale)
    ev = {"failed": [], "done": [], "usage": [], "delta": [], "rounds": []}
    th.failed.connect(lambda s, m: ev["failed"].append(m)); th.completed.connect(lambda s: ev["done"].append(1))
    th.usage.connect(lambda s, p, c, e: ev["usage"].append((p, c, e))); th.delta.connect(lambda s, k, t: ev["delta"].append((k, t)))
    th.tool_round.connect(lambda s, a, r: ev["rounds"].append((a, r)))
    th.start(); t0 = time.time()
    while not th.isFinished() and time.time() - t0 < wait_s: app.processEvents(); time.sleep(0.005)
    th.wait(2000)
    for _ in range(20): app.processEvents()
    return ev, th
small = [{"role": "user", "content": "hello"}]
big = [{"role": "user", "content": "This is a long English text for checking the context guard. " * 600}]   # ≈ 6 thousand tokens
est_big = estimate_request_tokens(big); print(f"      big message: estimate {est_big} tok.")
ev, th = run_stream(2000, big, steps=[[{"choices": [{"delta": {"content": "must not be sent"}}]}]])
check("does not fit: the request does not go to the server", lm.requests == [] and not ev["done"], len(lm.requests))
check("a clear message: estimate, room for the reply and the limit", len(ev["failed"]) == 1 and "The model's context is full" in ev["failed"][0] and "2 000" in ev["failed"][0] and "new chat" in ev["failed"][0], ev["failed"])
ev, th = run_stream(100_000, big, steps=[[{"choices": [{"delta": {"content": "answer"}}]}, usage(est_big)]])
check("fits: the request went out, the answer arrived", len(lm.requests) == 1 and ev["done"] == [1] and not ev["failed"])
check("usage is passed on together with our estimate for calibration", ev["usage"] == [(est_big, 5, est_big)], ev["usage"])
ev, th = run_stream(None, big, steps=[[{"choices": [{"delta": {"content": "answer"}}]}]])
check("limit unknown: the guard does not get in the way", ev["done"] == [1] and len(lm.requests) == 1)

# the boundary with the correction and the safety margin
est = estimate_request_tokens(small); reserve = 100
limit_ok = int(est * 1.0 * CONTEXT_SAFETY) + reserve + 1
limit_tight = int(est * 1.0 * CONTEXT_SAFETY) + reserve - 1
ev, _ = run_stream(limit_ok * 4, small, max_tokens=100, steps=[[{"choices": [{"delta": {"content": "ok"}}]}]])
check("far from the boundary it passes", ev["done"] == [1])
th = ChatStreamer(1, "http://x/v1", "", "m", small, 0.1, 100, context_limit=400, scale=1.0)
check("room for the reply = min(max_tokens, limit/4)", th.reserve == 100 and ChatStreamer(1, "u", "", "m", small, 0.1, 8000, context_limit=1000).reserve == 250 and ChatStreamer(1, "u", "", "m", small, 0.1, 8000).reserve == 0)
th = ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=est_big + 500, scale=1.0)
check("the safety margin and the correction are applied", th._fits(big, None) is False and ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=int(est_big * 1.3) + 200, scale=1.0)._fits(big, None) is True)
check("a 1.5 correction makes the check stricter", ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=int(est_big * 1.3) + 200, scale=1.5)._fits(big, None) is False)
check("a 0.7 correction (a more economical tokenizer) allows more", ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=int(est_big * 0.9) + 200, scale=0.7)._fits(big, None) is True)

# ---- between rounds: tool results fill the context ----
class Fake:
    calls = 0
    def call(self, server, name, args, timeout): Fake.calls += 1; return ("Result of reading a big file. " * 420, False)   # ≈ 2.3 thousand tokens
from core.mcp_manager import ToolInfo
tool = ToolInfo("tb", "read", "reads", {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]}, read_only=True)
router = ToolRouter(Fake(), Catalog([tool]))
call = lambda i: [tc(i, "call_tool", {"tool": "tb.read", "arguments": {"p": "x"}})]
msgs = [{"role": "system", "content": Catalog([tool]).text()}, {"role": "user", "content": "read it several times"}]
ev, th = run_stream(6000, msgs, tools=META_TOOLS, router=router, max_tokens=500, steps=[call("a"), call("b"), call("c"), call("d"), [{"choices": [{"delta": {"content": "too late"}}]}]])
check("several rounds made it, then a stop on context (not a model crash)", 1 <= len(ev["rounds"]) < 4 and len(ev["failed"]) == 1 and "The model's context is full" in ev["failed"][0], (len(ev["rounds"]), ev["failed"]))
check("no extra requests to the server after the overflow", len(lm.requests) == len(ev["rounds"]) + 0 or len(lm.requests) == len(ev["rounds"]), (len(lm.requests), len(ev["rounds"])))
check("completed rounds remain (they will be saved to the chat)", len(ev["rounds"]) >= 1 and Fake.calls == len(ev["rounds"]))

# ---- diagnostics log ----
check("memory_summary on Windows returns rss and free memory", "rss=" in diag.memory_summary() and "ram_free=" in diag.memory_summary(), diag.memory_summary())
log = tmp / "diag.log"; diag.install(log)
run_stream(100_000, small, steps=[[{"choices": [{"delta": {"content": "ok"}}]}, usage(30)]])
text = log.read_text(encoding="utf-8")
check("before a request a [req] line with the estimate, limit and memory is logged", "[req] stream 5 request≈" in text and "limit=100000" in text and "rss=" in text, text[-300:])
run_stream(1000, big, steps=[])
check("and on a context refusal the entry stays (it shows what happened last)", text.count("[req]") < log.read_text(encoding="utf-8").count("[req]"))

# ---- window: the check before sending and calibration ----
from ui.main_window import MainWindow
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
w = MainWindow(mcp_config=tmp / "mcp.json", connect_mcp=False); ctx = w.context; b = ctx.builder; sel = ctx.model_selector; v = w.chat_view
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1"); c.save_btn.click(); wait(lambda: not b.models_req.busy)
b.name.setText("p"); b.base_model.setCurrentText("fake"); b.max_tokens.setValue(1000); b.save_btn.click()
key = ("http://127.0.0.1:1/v1", "fake")
w._limit_key = key; w._limit_info = (3000, True, 262144)
v.input.setPlainText("This is a long English text for checking the context guard. " * 600); n_chats = len(db.list_chats())
v.send_btn.click(); app.processEvents()
check("too large a request: refused before writing to the history", "do not fit into the model's context (3 000)" in v.status.text() and len(db.list_chats()) == n_chats and w.stream is None, v.status.text())
check("the typed text is not lost", len(v.input.toPlainText()) > 20000)
check("the refusal has the estimate and the room for the reply", "a reply of up to 750" in v.status.text(), v.status.text())   # min(max_tokens 1000, 3000//4=750)
print("      ", v.status.text())
v.input.setPlainText("a short message")
lm.steps = [[{"choices": [{"delta": {"content": "answer"}}]}, usage(2400)]]
v.send_btn.click(); wait(lambda: w.stream is None, 20)
check("a short message passes the same guard", db.list_messages(w.chat_id)[-1]["content"] == "answer")
w._limit_key = None; w._limit_info = None
v.input.setPlainText("This is a long English text for checking the context guard. " * 600)
lm.steps = [[{"choices": [{"delta": {"content": "accepted"}}]}, usage(6000)]]
v.send_btn.click(); wait(lambda: w.stream is None, 20)
check("limit unknown (model not loaded): the guard does not block", db.list_messages(w.chat_id)[-1]["content"] == "accepted")

# calibration: the server counted 1.5 times more than our estimate
w._scale.clear(); w._limit_info = None
raw = estimate_request_tokens([{"role": "user", "content": "Hello " * 620}])     # an empty new chat: only this message is in the request
def fresh_send(text, prompt_tokens):
    w.new_chat(); lm.steps = [[{"choices": [{"delta": {"content": "ok"}}]}, usage(prompt_tokens)]]
    v.input.setPlainText(text); v.send_btn.click(); wait(lambda: w.stream is None, 20)
fresh_send("Hello " * 620, int(raw * 1.5))
check("first measurement: EMA = 0.5·1.0 + 0.5·1.5 = 1.25", abs(w._scale[key] - 1.25) < 0.01, w._scale[key])
fresh_send("Hello " * 620, int(raw * 1.5))
check("second measurement: 0.5·1.25 + 0.5·1.5 = 1.375", abs(w._scale[key] - 1.375) < 0.01, w._scale[key])
fmt_tok = lambda n: f"{n:,}".replace(",", " ")
info_text = w.files_panel.info.text()
check("the panel shows the correction and the server's count",
      f"Estimate corrected from the server's measurements (×{w._scale[key]:.2f})" in info_text
      and f"Last request as counted by the server: {fmt_tok(int(raw * 1.5))} tok." in info_text, info_text)
fresh_send("Hello " * 620, int(raw * 0.5))
check("a tokenizer more economical than the estimate: the correction goes down", w._scale[key] < 1.375, w._scale[key])
w._scale[key] = 1.0
for _ in range(10): fresh_send("Hello " * 620, 10_000_000)
check("the correction is capped from above (×2.0)", abs(w._scale[key] - 2.0) < 1e-9, w._scale[key])
w._scale[key] = 1.0
for _ in range(10): fresh_send("Hello " * 620, 1)
check("the correction is capped from below (×0.6)", abs(w._scale[key] - 0.6) < 1e-9, w._scale[key])
w._scale[key] = 1.0
fresh_send("briefly", 5)
check("a short request (<300 tok.) does not change the correction", w._scale[key] == 1.0, w._scale[key])
msg_tokens = estimate_request_tokens([{"role": "user", "content": "Hello " * 620}])
lim = 1100          # at ×1.0: ≈ msg·1.15 + room min(1000, lim/4) passes; at ×1.5 it no longer does
w._limit_key = key; w._limit_info = (lim, True, None); w.new_chat()
w._scale[key] = 1.0; v.input.setPlainText("Hello " * 620)
check("at ×1.0 the request passes the guard", int(msg_tokens * 1.15) + min(1000, lim // 4) <= lim and w._context_overflow(db.get_connection("lm"), db.get_profile("p"), [{"role": "user", "content": "Hello " * 620}], None) == "")
w._scale[key] = 1.5
check("a ×1.5 correction makes the guard stricter: the same request is now refused",
      "do not fit" in w._context_overflow(db.get_connection("lm"), db.get_profile("p"), [{"role": "user", "content": "Hello " * 620}], None))
v.send_btn.click(); app.processEvents()
check("and in the window: refusal in the status, the text stays", "do not fit into the model's context (1 100)" in v.status.text() and v.input.toPlainText().startswith("Hello"), v.status.text())
conn_row, prof_row = db.get_connection("lm"), db.get_profile("p")
big_msg = [{"role": "user", "content": "Hello " * 6200}]; w._limit_key = key; w._limit_info = (5000, True, None)
verdicts = {}
for sc_value in (0.6, 1.0, 2.0):
    w._scale[key] = sc_value; verdicts[sc_value] = w._context_overflow(conn_row, prof_row, big_msg, None)
check("a correction <1 does not weaken the guard (same as ×1.0)", verdicts[0.6] == verdicts[1.0] and verdicts[1.0] != "", verdicts)
check("a ×2.0 correction makes it stricter (a higher estimate in the request)", verdicts[2.0] != verdicts[1.0] and "do not fit" in verdicts[2.0])
w.close(); llm.httpx.stream = real_stream
print("ALL OK" if ok_all else "SOME FAILED")
