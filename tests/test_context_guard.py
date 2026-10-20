"""Защита от переполнения контекста: usage сервера, поправка оценки, проверка до отправки и между раундами, лог."""
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

# ---- usage из потока ----
lm.steps = [[{"choices": [{"delta": {"content": "ок"}}]}, usage(1234, 7)]]
out = list(llm.stream_chat("http://x/v1", "", "m", [{"role": "user", "content": "q"}], 0.5, 10))
check("stream_chat отдаёт usage сервера", ("usage", {"prompt_tokens": 1234, "completion_tokens": 7}) in out and ("content", "ок") in out, out)
check("запрос просит usage (stream_options)", lm.requests[0]["stream_options"] == {"include_usage": True})
lm.steps = [[{"choices": [{"delta": {"content": "ок"}}]}]]
check("сервер без usage: просто нет события", [k for k, _ in llm.stream_chat("http://x/v1", "", "m", [], 0.5, 10)] == ["content"])
lm.steps = [[{"choices": [{"delta": {"content": "а"}}], "usage": None}, {"model": "m", "choices": [], "usage": {"prompt_tokens": "много"}}]]
check("мусор в usage игнорируется", [k for k, _ in llm.stream_chat("http://x/v1", "", "m", [], 0.5, 10)] == ["content"])

# ---- ChatStreamer: проверка контекста ----
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
small = [{"role": "user", "content": "привет"}]
big = [{"role": "user", "content": "Это длинный русский текст для проверки защиты контекста. " * 400}]   # ≈ 6 тыс. токенов
est_big = estimate_request_tokens(big); print(f"      большое сообщение: оценка {est_big} ток.")
ev, th = run_stream(2000, big, steps=[[{"choices": [{"delta": {"content": "не должно уйти"}}]}]])
check("не помещается: запрос на сервер не уходит", lm.requests == [] and not ev["done"], len(lm.requests))
check("сообщение понятное: оценка, запас на ответ и лимит", len(ev["failed"]) == 1 and "Контекст модели заполнен" in ev["failed"][0] and "2 000" in ev["failed"][0] and "новый чат" in ev["failed"][0], ev["failed"])
ev, th = run_stream(100_000, big, steps=[[{"choices": [{"delta": {"content": "ответ"}}]}, usage(est_big)]])
check("помещается: запрос ушёл, ответ получен", len(lm.requests) == 1 and ev["done"] == [1] and not ev["failed"])
check("usage пробрасывается вместе с нашей оценкой для калибровки", ev["usage"] == [(est_big, 5, est_big)], ev["usage"])
ev, th = run_stream(None, big, steps=[[{"choices": [{"delta": {"content": "ответ"}}]}]])
check("лимит неизвестен: защита не мешает", ev["done"] == [1] and len(lm.requests) == 1)

# граница с учётом поправки и запаса прочности
est = estimate_request_tokens(small); reserve = 100
limit_ok = int(est * 1.0 * CONTEXT_SAFETY) + reserve + 1
limit_tight = int(est * 1.0 * CONTEXT_SAFETY) + reserve - 1
ev, _ = run_stream(limit_ok * 4, small, max_tokens=100, steps=[[{"choices": [{"delta": {"content": "ок"}}]}]])
check("далеко от границы проходит", ev["done"] == [1])
th = ChatStreamer(1, "http://x/v1", "", "m", small, 0.1, 100, context_limit=400, scale=1.0)
check("запас на ответ = min(max_tokens, лимит/4)", th.reserve == 100 and ChatStreamer(1, "u", "", "m", small, 0.1, 8000, context_limit=1000).reserve == 250 and ChatStreamer(1, "u", "", "m", small, 0.1, 8000).reserve == 0)
th = ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=est_big + 500, scale=1.0)
check("запас прочности и поправка учитываются", th._fits(big, None) is False and ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=int(est_big * 1.3) + 200, scale=1.0)._fits(big, None) is True)
check("поправка 1.5 делает проверку строже", ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=int(est_big * 1.3) + 200, scale=1.5)._fits(big, None) is False)
check("поправка 0.7 (токенизатор экономнее) разрешает больше", ChatStreamer(1, "u", "", "m", big, 0.1, 100, context_limit=int(est_big * 0.9) + 200, scale=0.7)._fits(big, None) is True)

# ---- между раундами: результаты инструментов заполняют контекст ----
class Fake:
    calls = 0
    def call(self, server, name, args, timeout): Fake.calls += 1; return ("Результат большого файла. " * 300, False)   # ≈ 2,5 тыс. токенов
from core.mcp_manager import ToolInfo
tool = ToolInfo("tb", "read", "читает", {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]}, read_only=True)
router = ToolRouter(Fake(), Catalog([tool]))
call = lambda i: [tc(i, "call_tool", {"tool": "tb.read", "arguments": {"p": "x"}})]
msgs = [{"role": "system", "content": Catalog([tool]).text()}, {"role": "user", "content": "прочитай несколько раз"}]
ev, th = run_stream(6000, msgs, tools=META_TOOLS, router=router, max_tokens=500, steps=[call("a"), call("b"), call("c"), call("d"), [{"choices": [{"delta": {"content": "поздно"}}]}]])
check("несколько раундов успели, потом остановка по контексту (а не падение модели)", 1 <= len(ev["rounds"]) < 4 and len(ev["failed"]) == 1 and "Контекст модели заполнен" in ev["failed"][0], (len(ev["rounds"]), ev["failed"]))
check("лишних запросов к серверу после переполнения нет", len(lm.requests) == len(ev["rounds"]) + 0 or len(lm.requests) == len(ev["rounds"]), (len(lm.requests), len(ev["rounds"])))
check("выполненные раунды остались (будут сохранены в чат)", len(ev["rounds"]) >= 1 and Fake.calls == len(ev["rounds"]))

# ---- лог диагностики ----
check("memory_summary на Windows возвращает rss и свободную память", "rss=" in diag.memory_summary() and "ram_свободно=" in diag.memory_summary(), diag.memory_summary())
log = tmp / "diag.log"; diag.install(log)
run_stream(100_000, small, steps=[[{"choices": [{"delta": {"content": "ок"}}]}, usage(30)]])
text = log.read_text(encoding="utf-8")
check("перед запросом в лог пишется [req] с оценкой, лимитом и памятью", "[req] поток 5 запрос≈" in text and "лимит=100000" in text and "rss=" in text, text[-300:])
run_stream(1000, big, steps=[])
check("и при отказе по контексту запись остаётся (видно, что было последним)", text.count("[req]") < log.read_text(encoding="utf-8").count("[req]"))

# ---- окно: проверка до отправки и калибровка ----
from ui.main_window import MainWindow
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
w = MainWindow(mcp_config=tmp / "mcp.json", connect_mcp=False); ctx = w.context; b = ctx.builder; sel = ctx.model_selector; v = w.chat_view
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1"); c.save_btn.click(); wait(lambda: not b.models_req.busy)
b.name.setText("p"); b.base_model.setCurrentText("fake"); b.max_tokens.setValue(1000); b.save_btn.click()
key = ("http://127.0.0.1:1/v1", "fake")
w._limit_key = key; w._limit_info = (3000, True, 262144)
v.input.setPlainText("Это длинный русский текст для проверки защиты контекста. " * 400); n_chats = len(db.list_chats())
v.send_btn.click(); app.processEvents()
check("слишком большой запрос: отказ до записи в историю", "не помещаются в контекст модели (3 000)" in v.status.text() and len(db.list_chats()) == n_chats and w.stream is None, v.status.text())
check("введённый текст не пропал", len(v.input.toPlainText()) > 20000)
check("в отказе: оценка и запас на ответ", "ответ до 750" in v.status.text(), v.status.text())   # min(max_tokens 1000, 3000//4=750)
print("      ", v.status.text())
v.input.setPlainText("короткое сообщение")
lm.steps = [[{"choices": [{"delta": {"content": "ответ"}}]}, usage(2400)]]
v.send_btn.click(); wait(lambda: w.stream is None, 20)
check("короткое сообщение проходит той же защитой", db.list_messages(w.chat_id)[-1]["content"] == "ответ")
w._limit_key = None; w._limit_info = None
v.input.setPlainText("Это длинный русский текст для проверки защиты контекста. " * 400)
lm.steps = [[{"choices": [{"delta": {"content": "принято"}}]}, usage(6000)]]
v.send_btn.click(); wait(lambda: w.stream is None, 20)
check("лимит неизвестен (модель не загружена): защита не блокирует", db.list_messages(w.chat_id)[-1]["content"] == "принято")

# калибровка: сервер насчитал в 1.5 раза больше нашей оценки
w._scale.clear(); w._limit_info = None
raw = estimate_request_tokens([{"role": "user", "content": "Слово " * 400}])     # пустой новый чат: в запросе только это сообщение
def fresh_send(text, prompt_tokens):
    w.new_chat(); lm.steps = [[{"choices": [{"delta": {"content": "ок"}}]}, usage(prompt_tokens)]]
    v.input.setPlainText(text); v.send_btn.click(); wait(lambda: w.stream is None, 20)
fresh_send("Слово " * 400, int(raw * 1.5))
check("первый замер: EMA = 0.5·1.0 + 0.5·1.5 = 1.25", abs(w._scale[key] - 1.25) < 0.01, w._scale[key])
fresh_send("Слово " * 400, int(raw * 1.5))
check("второй замер: 0.5·1.25 + 0.5·1.5 = 1.375", abs(w._scale[key] - 1.375) < 0.01, w._scale[key])
fmt_tok = lambda n: f"{n:,}".replace(",", " ")
info_text = w.files_panel.info.text()
check("панель показывает поправку и счёт сервера",
      f"Оценка скорректирована по замерам сервера (×{w._scale[key]:.2f})" in info_text
      and f"Последний запрос по счёту сервера: {fmt_tok(int(raw * 1.5))} ток." in info_text, info_text)
fresh_send("Слово " * 400, int(raw * 0.5))
check("токенизатор экономнее оценки: поправка уменьшается", w._scale[key] < 1.375, w._scale[key])
w._scale[key] = 1.0
for _ in range(10): fresh_send("Слово " * 400, 10_000_000)
check("поправка ограничена сверху (×2.0)", abs(w._scale[key] - 2.0) < 1e-9, w._scale[key])
w._scale[key] = 1.0
for _ in range(10): fresh_send("Слово " * 400, 1)
check("поправка ограничена снизу (×0.6)", abs(w._scale[key] - 0.6) < 1e-9, w._scale[key])
w._scale[key] = 1.0
fresh_send("коротко", 5)
check("короткий запрос (<300 ток.) поправку не меняет", w._scale[key] == 1.0, w._scale[key])
msg_tokens = estimate_request_tokens([{"role": "user", "content": "Слово " * 400}])
lim = 1100          # при ×1.0: ≈ msg·1.15 + запас min(1000, lim/4) проходит; при ×1.5 уже нет
w._limit_key = key; w._limit_info = (lim, True, None); w.new_chat()
w._scale[key] = 1.0; v.input.setPlainText("Слово " * 400)
check("при ×1.0 запрос проходит защиту", int(msg_tokens * 1.15) + min(1000, lim // 4) <= lim and w._context_overflow(db.get_connection("lm"), db.get_profile("p"), [{"role": "user", "content": "Слово " * 400}], None) == "")
w._scale[key] = 1.5
check("поправка ×1.5 делает защиту строже: тот же запрос теперь отклоняется",
      "не помещаются" in w._context_overflow(db.get_connection("lm"), db.get_profile("p"), [{"role": "user", "content": "Слово " * 400}], None))
v.send_btn.click(); app.processEvents()
check("и в окне: отказ в статусе, текст остался", "не помещаются в контекст модели (1 100)" in v.status.text() and v.input.toPlainText().startswith("Слово"), v.status.text())
conn_row, prof_row = db.get_connection("lm"), db.get_profile("p")
big_msg = [{"role": "user", "content": "Слово " * 4000}]; w._limit_key = key; w._limit_info = (5000, True, None)
verdicts = {}
for sc_value in (0.6, 1.0, 2.0):
    w._scale[key] = sc_value; verdicts[sc_value] = w._context_overflow(conn_row, prof_row, big_msg, None)
check("поправка <1 не ослабляет защиту (как ×1.0)", verdicts[0.6] == verdicts[1.0] and verdicts[1.0] != "", verdicts)
check("поправка ×2.0 ужесточает (оценка в запросе выше)", verdicts[2.0] != verdicts[1.0] and "не помещаются" in verdicts[2.0])
w.close(); llm.httpx.stream = real_stream
print("ALL OK" if ok_all else "SOME FAILED")
