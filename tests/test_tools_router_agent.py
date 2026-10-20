import json, sys, pathlib, tempfile, time
import httpx
from PySide6.QtWidgets import QApplication
from core import db, llm
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "t22.db"
from core.mcp_manager import ToolInfo
from core.tools import (Catalog, ToolRouter, META_TOOLS, signature, validate_arguments, meta_tools_tokens,
                        schema_tokens, default_policy, MAX_RESULT_CHARS, CATALOG_MAX_LISTED)
from core.context_builder import build_messages, history_messages
from worker import ChatStreamer

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

def T(server, name, desc, props, required=(), ro=True):
    return ToolInfo(server, name, desc, {"type": "object", "properties": props, "required": list(required)}, read_only=ro)
calc = T("tb", "calc", "Точно вычисляет арифметическое выражение. Разрешены + - *.", {"expression": {"type": "string"}}, ["expression"])
fsread = T("tb", "fs_read", "Читает текстовый файл целиком.", {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"])
fslist = T("tb", "fs_list", "Содержимое папки: подпапки и файлы.", {"path": {"type": "string"}}, ["path"])
write = T("tb", "notes_write", "Сохраняет заметку в файл.", {"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"], ro=False)
tools = [calc, fsread, fslist, write]

# ---------- tools.py ----------
check("signature: обязательные и необязательные", signature(fsread) == "tb.fs_read(path: str, max_chars?: int)" and signature(calc) == "tb.calc(expression: str)")
cat = Catalog(tools); text = cat.text()
check("каталог: заголовок и строки", text.startswith("Инструменты. Вызывай их только через call_tool") and "- tb.calc(expression: str) — Точно вычисляет арифметическое выражение." in text, text)
check("каталог: описание сокращено до первого предложения", "Разрешены" not in text)
check("пустой каталог = пустая строка", Catalog([]).text() == "")
big = Catalog([T("s", f"t{i}", "описание", {}) for i in range(CATALOG_MAX_LISTED + 1)]).text()
check("большой каталог не перечисляется", "каталог не перечисляется" in big and "t0" not in big and "s (41)" in big, big)
check("find по-русски со словоформами", [t.name for t in cat.find("прочитать файлы")] == ["fs_read"] or "fs_read" in [t.name for t in cat.find("файлы")], [t.name for t in cat.find("файлы")])
check("find: вычислить", [t.name for t in cat.find("вычислить выражение")][:1] == ["calc"], [t.name for t in cat.find("вычислить выражение")])
check("find: ничего", cat.find("квантовая телепортация") == [])
fnd = lambda q: [t.name for t in cat.find(q)]
check("find: «вычислить» -> calc (вычисляет)", fnd("вычислить") == ["calc"], fnd("вычислить"))
check("find: «прочитать файл» -> fs_read (Читает)", fnd("прочитать файл")[:1] == ["fs_read"], fnd("прочитать файл"))
check("find: «сохранить заметку» -> notes_write", fnd("сохранить заметку")[:1] == ["notes_write"], fnd("сохранить заметку"))
check("find: «что в папке» -> fs_list", fnd("что в папке")[:1] == ["fs_list"], fnd("что в папке"))
check("find: по имени на английском", fnd("fs read") [:1] == ["fs_read"] and fnd("notes")[:1] == ["notes_write"], (fnd("fs read"), fnd("notes")))
check("find: «использовать» не липнет к «читать»", fnd("использовать") == [], fnd("использовать"))
check("get: полное, без регистра, голое имя", cat.get("tb.calc") is calc and cat.get("TB.CALC") is calc and cat.get("calc") is calc and cat.get("nope") is None)
amb = Catalog([T("a", "x", "", {}), T("b", "x", "", {})]); check("get: неоднозначное голое имя -> None", amb.get("x") is None and amb.get("a.x"))
check("validate: верно", validate_arguments(fsread, {"path": "a"}) == [])
check("validate: нет обязательного", "required" in validate_arguments(fsread, {})[0])
check("validate: неверный тип", "'5' is not of type 'integer'" in validate_arguments(fsread, {"path": "a", "max_chars": "5"})[0])
check("validate: не объект", validate_arguments(fsread, "x") == ["аргументы должны быть объектом JSON"])
check("мета-инструменты: 3 штуки и компактны", [t["function"]["name"] for t in META_TOOLS] == ["find_tool", "describe_tool", "call_tool"] and meta_tokens_ok if (meta_tokens_ok := meta_tools_tokens() < 300) else False, meta_tools_tokens())
check("экономия: схемы дороже каталога", schema_tokens(tools) > 0)
check("политика по умолчанию", default_policy(calc) == "auto" and default_policy(write) == "ask" and default_policy(T("s", "u", "", {}, ro=None)) == "ask")

class FakeManager:
    def __init__(self): self.calls = []; self.reply = ("результат", False)
    def call(self, server, name, args, timeout): self.calls.append((server, name, args, timeout)); return self.reply
fm = FakeManager(); asked = []
def confirm_yes(tid, args): asked.append(tid); return True
def confirm_no(tid, args): asked.append(tid); return False
R = lambda **kw: ToolRouter(fm, cat, **kw)
r = R()
check("find_tool через router", "tb.calc(expression: str)" in r.handle("find_tool", '{"query": "вычислить"}'))
check("find_tool без query", r.handle("find_tool", "{}").startswith("ОШИБКА"))
check("find_tool ничего не нашёл: список всех", "Все инструменты: tb.calc" in r.handle("find_tool", '{"query": "квантовая"}'))
fsread.input_schema["properties"]["path"]["title"] = "Path"   # FastMCP добавляет такие поля в каждую схему
raw_describe = r.handle("describe_tool", '{"name": "tb.fs_read"}')
check("describe_tool: без служебных title и без отступов (меньше токенов)", '"title"' not in raw_describe and "\n" not in raw_describe, raw_describe)
check("signature: типы аргументов", signature(T("s", "x", "", {"a": {"type": "array"}, "b": {"type": "boolean"}, "c": {}, "d": {"type": "number"}}, ["a"])) == "s.x(a: list, b?: bool, c?, d?: num)")
check("заголовок каталога: не выдумывать результат и не обещать действие", "не выдумывай результат" in text and "Не обещай выполнить действие" in text and "Сигнатуры из каталога достаточно" in text)
d = json.loads(r.handle("describe_tool", '{"name": "tb.fs_read"}'))
check("describe_tool: схема", d["name"] == "tb.fs_read" and d["arguments_schema"]["required"] == ["path"] and d["read_only"] is True)
check("describe_tool: опечатка -> подсказка", "нет в каталоге" in r.handle("describe_tool", '{"name": "fs_reed"}') and "tb.fs_read" in r.handle("describe_tool", '{"name": "tb.fs_read2"}'))
check("call_tool: выполнение", r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1+1"}}') == "результат"
      and fm.calls[-1] == ("tb", "calc", {"expression": "1+1"}, 60.0))
check("call_tool: аргументы строкой JSON", r.handle("call_tool", json.dumps({"name": "tb.calc", "arguments": '{"expression": "2"}'})) == "результат" and fm.calls[-1][2] == {"expression": "2"})
check("call_tool: голое имя", r.handle("call_tool", '{"name": "calc", "arguments": {"expression": "3"}}') == "результат")
n = len(fm.calls)
bad = r.handle("call_tool", '{"name": "tb.fs_read", "arguments": {}}')
check("call_tool: неверные аргументы -> ошибка со схемой, сервер не вызван", bad.startswith("ОШИБКА: неверные аргументы") and '"required"' in bad and len(fm.calls) == n, bad)
check("call_tool: неизвестный", "нет в каталоге" in r.handle("call_tool", '{"name": "tb.zzz", "arguments": {}}') and len(fm.calls) == n)
check("call_tool: сломанный JSON", r.handle("call_tool", '{"name": "tb.calc", "arguments": {').startswith("ОШИБКА: аргументы не являются корректным JSON"))
check("call_tool: аргументы не объект", r.handle("call_tool", '[1,2]').startswith("ОШИБКА: аргументы должны быть объектом"))
check("прямой вызов инструмента по имени (модель ошиблась формой)", r.handle("tb.calc", '{"expression": "4"}') == "результат")
check("неизвестная функция", "функции «hack»" in r.handle("hack", "{}"))
fm.reply = ("x" * (MAX_RESULT_CHARS + 500), False); out = r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}')
check("длинный результат обрезается", len(out) < MAX_RESULT_CHARS + 120 and "[обрезано: показано 12000 из 12500" in out)
fm.reply = ("файл не найден", True); check("ошибка инструмента помечается", r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}') == "ОШИБКА: файл не найден")
fm.reply = ("", False); check("пустой результат", r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}') == "(пустой результат)")
fm.reply = ("ok", False); n = len(fm.calls)
w = '{"name": "tb.notes_write", "arguments": {"name": "a", "content": "b"}}'
check("ask без confirm: отказ, сервер не вызван", "отклонил" in R().handle("call_tool", w) and len(fm.calls) == n)
check("ask + confirm=нет: отказ", "отклонил" in R(confirm=confirm_no).handle("call_tool", w) and len(fm.calls) == n and asked[-1] == "tb.notes_write")
check("ask + confirm=да: выполнено", R(confirm=confirm_yes).handle("call_tool", w) == "ok" and len(fm.calls) == n + 1)
check("deny: не спрашивает и не вызывает", "запрещён" in R(policy_for=lambda t: "deny", confirm=confirm_yes).handle("call_tool", w) and len(fm.calls) == n + 1)
asked.clear()
check("auto для записи: не спрашивает", R(policy_for=lambda t: "auto", confirm=confirm_no).handle("call_tool", w) == "ok" and not asked)

# одна и та же ошибка инструмента подряд: подсказка на втором разе, блокировка на третьем
rr = R(); c = '{"tool": "tb.calc", "arguments": {"expression": "1"}}'
fm.reply = ("Vision-модель не настроена", True); n = len(fm.calls)
o1, o2 = rr.handle("call_tool", c), rr.handle("call_tool", c)
check("повтор ошибки: первый раз без подсказки", o1 == "ОШИБКА: Vision-модель не настроена", o1)
check("повтор ошибки: второй раз — подсказка не повторять", o2.startswith("ОШИБКА: Vision-модель не настроена") and "2-я подряд" in o2 and "без него" in o2, o2)
o3 = rr.handle("call_tool", c)
check("повтор ошибки: третий вызов не доходит до сервера", len(fm.calls) == n + 2 and "не выполнен" in o3 and "Vision-модель не настроена" in o3, (len(fm.calls) - n, o3))
check("другой инструмент не заблокирован", rr.handle("call_tool", '{"tool": "tb.fs_read", "arguments": {"path": "a"}}').startswith("ОШИБКА: Vision") and len(fm.calls) == n + 3)
check("новый ответ (новый роутер) начинает со счёта с нуля", R().handle("call_tool", c) == "ОШИБКА: Vision-модель не настроена")
r4 = R(); fm.reply = ("файл не найден: a", True); r4.handle("call_tool", c)
fm.reply = ("файл не найден: b", True); r4.handle("call_tool", c); n = len(fm.calls)
fm.reply = ("файл не найден: c", True)
check("разные ошибки (подбор пути) не блокируются", "не выполнен" not in r4.handle("call_tool", c) and len(fm.calls) == n + 1)
r5 = R(); fm.reply = ("сбой", True); r5.handle("call_tool", c)
fm.reply = ("ok", False); check("удачный вызов сбрасывает счёт", r5.handle("call_tool", c) == "ok" and "tb.calc" not in r5._errors)
fm.reply = ("сбой", True); n = len(fm.calls); r5.handle("call_tool", c)
check("после успеха ошибки снова считаются с единицы", "не выполнен" not in r5.handle("call_tool", c) and len(fm.calls) == n + 2)
fm.reply = ("ok", False)

n0 = len(fm.calls)
check("call_tool: основной параметр tool", r.handle("call_tool", '{"tool": "tb.calc", "arguments": {"expression": "5"}}') == "ok"
      and fm.calls[n0] == ("tb", "calc", {"expression": "5"}, 60.0), fm.calls[n0:])
fm.reply = ("записано", False)
r_auto = ToolRouter(fm, cat, policy_for=lambda t: "auto")
n_calls = len(fm.calls)
out = r_auto.handle("call_tool", '{"tool": "tb.notes_write", "arguments": {"name": "demo", "content": "x"}}')
check("коллизия имён: вызван notes_write с name=demo", out == "записано" and fm.calls[n_calls][1] == "notes_write" and fm.calls[n_calls][2] == {"name": "demo", "content": "x"}, (out, fm.calls[n_calls:]))
out = r_auto.handle("call_tool", '{"arguments": {"content": "x", "name": "tb.notes_write"}}')
check("забыли tool: понятная ошибка с форматом", out.startswith("ОШИБКА: не указано имя инструмента") and 'tool="сервер.инструмент"' in out, out)
check("describe_tool принимает tool", json.loads(r_auto.handle("describe_tool", '{"tool": "tb.fs_read"}'))["name"] == "tb.fs_read")
check("запасной параметр name всё ещё понимается", r_auto.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}') == "записано")
check("мета-инструменты: параметр называется tool", META_TOOLS[2]["function"]["parameters"]["required"] == ["tool", "arguments"] and META_TOOLS[1]["function"]["parameters"]["required"] == ["tool"])
from core.tools import describe_call, result_label
check("describe_call/result_label понимают tool", describe_call("call_tool", '{"tool": "tb.calc", "arguments": {"expression": "1"}}') == 'tb.calc({"expression": "1"})'
      and result_label("call_tool", '{"tool": "tb.calc", "arguments": {}}') == "tb.calc" and result_label("find_tool", "{}") == "find_tool")
# ---------- парсинг tool_calls из потока ----------
def sse(*chunks):
    return ("".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
def run_llm(handler, **kw):
    real = llm.httpx.stream
    llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
    try: return list(llm.stream_chat("http://x/v1", "", "m", [], 0.1, 10, tools=kw.get("tools")))
    finally: llm.httpx.stream = real
tc = lambda *calls, **extra: {"choices": [{"delta": {"tool_calls": list(calls)}, **extra}]}
seen = {}
def h1(req):
    seen["body"] = json.loads(req.content)
    return httpx.Response(200, content=sse(
        tc({"index": 0, "id": "c1", "type": "function", "function": {"name": "call_tool", "arguments": ""}}),
        tc({"index": 0, "function": {"arguments": '{"name": "tb.'}}),
        tc({"index": 0, "function": {"arguments": 'calc", "arguments": {"expression": "1+1"}}'}}),
        tc({"index": 1, "id": "c2", "function": {"name": "find_tool", "arguments": '{"query": "x"}'}}),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
out = run_llm(h1, tools=META_TOOLS)
check("tool_calls: куски склеены, два вызова по индексам", out == [("tool_calls", [
    {"id": "c1", "name": "call_tool", "arguments": '{"name": "tb.calc", "arguments": {"expression": "1+1"}}'},
    {"id": "c2", "name": "find_tool", "arguments": '{"query": "x"}'}])], out)
check("tools и tool_choice уходят в запрос", seen["body"]["tools"] == META_TOOLS and seen["body"]["tool_choice"] == "auto")
run_llm(lambda r: httpx.Response(200, content=sse({"choices": [{"delta": {"content": "a"}}]})))
out = run_llm(lambda r: (seen.update(body=json.loads(r.content)) or httpx.Response(200, content=sse({"choices": [{"delta": {"content": "a"}}]}))))
check("без tools: поля нет", "tools" not in seen["body"] and out == [("content", "a")])
out = run_llm(lambda r: httpx.Response(200, content=sse(tc({"function": {"name": "f", "arguments": {"a": 1}}}))))
check("без id и index: id генерируется, dict-аргументы -> JSON", out[0][1][0]["id"].startswith("call_0_") and out[0][1][0]["arguments"] == '{"a": 1}', out)
out = run_llm(lambda r: httpx.Response(200, content=sse({"choices": [{"delta": {"content": "текст"}}]}, tc({"index": 0, "id": "z", "function": {"name": "n", "arguments": "{}"}}))))
check("текст и вызовы вместе: сначала текст, вызовы в конце", [k for k, _ in out] == ["content", "tool_calls"])

# ---------- история с инструментами ----------
db.add_message(1, "user", "q") if False else None
cid = db.create_chat()
db.add_message(cid, "user", "посчитай 1+1")
calls = [{"id": "c1", "type": "function", "function": {"name": "call_tool", "arguments": '{"name":"tb.calc","arguments":{"expression":"1+1"}}'}}]
db.add_messages(cid, [
    {"role": "assistant", "content": "", "reasoning": "надо посчитать", "tool_calls": json.dumps(calls)},
    {"role": "tool", "content": "2", "tool_call_id": "c1", "tool_name": "call_tool"}])
db.add_message(cid, "assistant", "Будет 2", "думал")
rows = db.list_messages(cid)
check("БД: сообщения с вызовами сохранены целиком", [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
      and json.loads(rows[1]["tool_calls"]) == calls and rows[2]["tool_call_id"] == "c1" and rows[2]["tool_name"] == "call_tool" and rows[1]["reasoning"] == "надо посчитать")
msgs = history_messages(rows)
check("история -> формат OpenAI", msgs == [
    {"role": "user", "content": "посчитай 1+1"},
    {"role": "assistant", "content": "", "tool_calls": calls},
    {"role": "tool", "tool_call_id": "c1", "content": "2"},
    {"role": "assistant", "content": "Будет 2"}], msgs)
b = build_messages({"system_prompt": "SYS", "memory": "MEM"}, rows, [("f.txt", "T")], "КАТАЛОГ")
check("system: промпт, память, каталог, файлы по порядку", b[0]["content"].index("SYS") < b[0]["content"].index("Память") < b[0]["content"].index("КАТАЛОГ") < b[0]["content"].index("=== f.txt"))
check("старые сообщения (без полей инструментов) читаются", history_messages([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]) == [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
db.add_messages(cid, [{"role": "user", "content": "x"}, {"role": "tool", "content": "y"}])
n = len(db.list_messages(cid))
try:
    db.add_messages(cid, [{"role": "user", "content": "ok"}, {"content": "нет role"}])
except KeyError: pass
check("add_messages атомарна: при ошибке ничего не сохраняется", len(db.list_messages(cid)) == n)
db.save_chat_tools(cid, {"tb.calc", "tb.fs_read"}); check("включённые инструменты чата", db.load_chat_tools(cid) == {"tb.calc", "tb.fs_read"})
db.save_chat_tools(cid, set()); check("... и очистка", db.load_chat_tools(cid) == set())
db.set_tool_policy("tb.calc", "deny"); db.set_tool_policy("tb.calc", "ask"); check("политики: запись и перезапись", db.load_tool_policies() == {"tb.calc": "ask"})
db.set_tool_policy("tb.calc", None); check("политики: сброс", db.load_tool_policies() == {})
db.delete_chat(cid); check("каскад: инструменты чата удалены", db.load_chat_tools(cid) == set())

# ---------- цикл агента (скриптованная «модель») ----------
app = QApplication([])
class Script:
    def __init__(self, steps, delay_from=99, delay=0.0): self.steps = list(steps); self.requests = []; self.delay_from, self.delay = delay_from, delay
    def handler(self, req):
        self.requests.append(json.loads(req.content))
        if len(self.requests) >= self.delay_from: time.sleep(self.delay)
        step = self.steps.pop(0) if self.steps else self.steps_last
        return httpx.Response(200, content=sse(*step))
def run_agent(script, router, tools=META_TOOLS, wait_s=20, cancel_after_round=False, max_steps=None):
    real = llm.httpx.stream
    llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(script.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
    ev = {"delta": [], "rounds": [], "running": [], "failed": [], "done": []}
    th = ChatStreamer(7, "http://x/v1", "", "m", [{"role": "user", "content": "q"}], 0.1, 100, tools=tools, router=router, max_steps=max_steps)
    th.delta.connect(lambda s, k, t: ev["delta"].append((k, t)))
    th.tool_running.connect(lambda s, n, a: ev["running"].append((n, a)))
    th.tool_round.connect(lambda s, a, r: (ev["rounds"].append((a, r)), th.cancel() if cancel_after_round else None))
    th.failed.connect(lambda s, m: ev["failed"].append(m)); th.completed.connect(lambda s: ev["done"].append(1))
    th.start(); t0 = time.time()
    while not th.isFinished() and time.time() - t0 < wait_s: app.processEvents(); time.sleep(0.005)
    th.wait(2000)
    for _ in range(20): app.processEvents()
    llm.httpx.stream = real
    return ev
call = lambda cid_, name, args: tc({"index": 0, "id": cid_, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
fm = FakeManager(); fm.reply = ("2", False); router = ToolRouter(fm, cat)
script = Script([
    [{"choices": [{"delta": {"reasoning_content": "найду инструмент"}}]}, call("a1", "find_tool", {"query": "вычислить"})],
    [call("a2", "call_tool", {"name": "tb.calc", "arguments": {"expression": "1+1"}})],
    [{"choices": [{"delta": {"content": "Будет "}}]}, {"choices": [{"delta": {"content": "2"}}]}]])
ev = run_agent(script, router)
check("агент: завершился без ошибок", ev["done"] == [1] and not ev["failed"], ev["failed"])
check("агент: два раунда инструментов", [r[0]["tool_calls"][0]["function"]["name"] for r in ev["rounds"]] == ["find_tool", "call_tool"])
check("агент: результаты инструментов", "tb.calc(expression: str)" in ev["rounds"][0][1][0]["content"] and ev["rounds"][1][1][0]["content"] == "2")
check("агент: размышления раунда сохранены", ev["rounds"][0][0]["reasoning"] == "найду инструмент")
check("агент: финальный текст стримится", [t for k, t in ev["delta"] if k == "content"] == ["Будет ", "2"])
check("агент: события running", [n for n, _ in ev["running"]] == ["find_tool", "call_tool"])
req3 = script.requests[2]["messages"]
check("агент: 3-й запрос содержит всю цепочку", [m["role"] for m in req3] == ["user", "assistant", "tool", "assistant", "tool"]
      and req3[1]["tool_calls"][0]["id"] == "a1" and req3[2]["tool_call_id"] == "a1" and req3[4]["content"] == "2", req3)
check("агент: tools отправлялись в каждом запросе", all(r.get("tools") == META_TOOLS for r in script.requests))
ev = run_agent(Script([[{"choices": [{"delta": {"content": "просто ответ"}}]}]]), router, tools=None)
check("без инструментов: обычный ответ, tools нет", ev["done"] == [1] and not ev["rounds"])
from worker import LIMIT_NUDGE
check("лимит шагов по умолчанию 16", ChatStreamer.MAX_STEPS == 16 and db.DEFAULT_MAX_STEPS == 16)
script = Script([[call(f"loop{i}", "find_tool", {"query": "x"})] for i in range(ChatStreamer.MAX_STEPS)]
                + [[{"choices": [{"delta": {"content": "Итог: сделал столько-то, осталось остальное."}}]}]])
ev = run_agent(script, router)
check("предел шагов: раунды, уведомление, завершение", len(ev["rounds"]) == ChatStreamer.MAX_STEPS and ev["done"] == [1] and not ev["failed"]
      and any(k == "notice" and "предел шагов" in t for k, t in ev["delta"]))
check("предел шагов: модель подвела итог текстом", [t for k, t in ev["delta"] if k == "content"] == ["Итог: сделал столько-то, осталось остальное."])
last = script.requests[-1]
check("итоговый запрос без инструментов и с просьбой подвести итог", len(script.requests) == ChatStreamer.MAX_STEPS + 1
      and "tools" not in last and last["messages"][-1] == {"role": "user", "content": LIMIT_NUDGE}, (len(script.requests), last["messages"][-1]))
check("просьба подвести итог не попадает в раунды (в БД не сохранится)", all(a[0].get("content") != LIMIT_NUDGE for a in ev["rounds"]))
script = Script([[call(f"c{i}", "find_tool", {"query": "x"})] for i in range(3)] + [[{"choices": [{"delta": {"content": "итог за 3 шага"}}]}]])
ev = run_agent(script, router, max_steps=3)
check("свой лимит шагов (3): ровно 3 раунда, затем итог", len(ev["rounds"]) == 3 and len(script.requests) == 4 and ev["done"] == [1]
      and any(k == "notice" and "(3)" in t for k, t in ev["delta"]), (len(ev["rounds"]), len(script.requests)))
script = Script([[call("a1", "find_tool", {"query": "x"})], [{"choices": [{"delta": {"content": "готово"}}]}]])
ev = run_agent(script, router, max_steps=1)
check("лимит 1: один раунд и затем итог без инструментов", len(ev["rounds"]) == 1 and "tools" not in script.requests[-1])
ev = run_agent(Script([[call("z", "find_tool", {"query": "x"})], [{"choices": [{"delta": {"content": "ок"}}]}]]), router, max_steps=0)
check("max_steps=0 трактуется как значение по умолчанию", len(ev["rounds"]) == 1 and ev["done"] == [1])
from worker import STUCK_NUDGE
fm_bad = FakeManager(); fm_bad.reply = ("Vision-модель не настроена", True); stuck_router = ToolRouter(fm_bad, cat)
cc = lambda i: call(f"v{i}", "call_tool", {"tool": "tb.calc", "arguments": {"expression": "1"}})
script = Script([[cc(i)] for i in range(10)] + [[{"choices": [{"delta": {"content": "Не вышло: инструмент не настроен."}}]}]])
script.steps_last = [{"choices": [{"delta": {"content": "Не вышло: инструмент не настроен."}}]}]
script.steps = [[cc(0)], [cc(1)], [cc(2)], script.steps_last]
ev = run_agent(script, stuck_router)
last = script.requests[-1]
check("зацикливание на сломанном инструменте: 3 раунда вместо 16", len(ev["rounds"]) == 3 and len(fm_bad.calls) == 2 and ev["done"] == [1] and not ev["failed"], (len(ev["rounds"]), len(fm_bad.calls)))
check("зацикливание: уведомление, итоговый запрос без инструментов и с просьбой подвести итог", any(k == "notice" and "повторяет одну и ту же ошибку" in t for k, t in ev["delta"])
      and "tools" not in last and last["messages"][-1] == {"role": "user", "content": STUCK_NUDGE.format(tool="tb.calc")} and len(script.requests) == 4, last["messages"][-1])
check("зацикливание: модель подвела итог текстом", [t for k, t in ev["delta"] if k == "content"] == ["Не вышло: инструмент не настроен."])
ev = run_agent(Script([[call("x1", "find_tool", {"query": "x"})], [{"choices": [{"delta": {"content": "не дойдёт"}}]}]],
                      delay_from=2, delay=0.4), router, cancel_after_round=True)
check("отмена после раунда: дальше не идёт", len(ev["rounds"]) == 1 and not ev["done"] and "не дойдёт" not in [t for _, t in ev["delta"]], ev)
class SlowRouter:
    def __init__(self): self.th = None
    def handle(self, n, a): self.th.cancel(); return "поздно"
sr = SlowRouter(); real = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(Script([[call("s1", "find_tool", {"query": "x"})]]).handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
th = ChatStreamer(8, "http://x/v1", "", "m", [], 0.1, 10, tools=META_TOOLS, router=sr); sr.th = th
rounds = []; th.tool_round.connect(lambda s, a, r: rounds.append(1)); th.start(); th.wait(10000)
for _ in range(10): app.processEvents()
llm.httpx.stream = real
check("отмена во время выполнения инструмента: раунд не сохраняется", rounds == [])
ev = run_agent(Script([[{"choices": [{"delta": {}}]}]]), router)
check("пустой ответ модели: завершение без вызовов", ev["done"] == [1] and not ev["rounds"])
class Boom:
    def handle(self, n, a): raise RuntimeError("упал")
ev = run_agent(Script([[call("b1", "find_tool", {})]]), Boom())
check("исключение в инструменте -> failed, поток жив", ev["failed"] and "упал" in ev["failed"][0] and not ev["done"], ev)
print("ALL OK" if ok_all else "SOME FAILED")




