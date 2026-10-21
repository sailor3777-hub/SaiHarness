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
calc = T("tb", "calc", "Calculates an arithmetic expression exactly. Allowed: + - *.", {"expression": {"type": "string"}}, ["expression"])
fsread = T("tb", "fs_read", "Reads a whole text file.", {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"])
fslist = T("tb", "fs_list", "Lists a folder: subfolders and files.", {"path": {"type": "string"}}, ["path"])
write = T("tb", "notes_write", "Saves a note to a file.", {"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"], ro=False)
tools = [calc, fsread, fslist, write]

# ---------- tools.py ----------
check("signature: required and optional", signature(fsread) == "tb.fs_read(path: str, max_chars?: int)" and signature(calc) == "tb.calc(expression: str)")
cat = Catalog(tools); text = cat.text()
check("catalog: header and lines", text.startswith("Tools. Call them only through call_tool") and "- tb.calc(expression: str) — Calculates an arithmetic expression exactly." in text, text)
check("catalog: description cut to the first sentence", "Allowed" not in text)
check("empty catalog = empty string", Catalog([]).text() == "")
big = Catalog([T("s", f"t{i}", "description", {}) for i in range(CATALOG_MAX_LISTED + 1)]).text()
check("a large catalog is not listed", "the catalog is not listed" in big and "t0" not in big and "s (41)" in big, big)
check("find with word forms", [t.name for t in cat.find("read files")] == ["fs_read"] or "fs_read" in [t.name for t in cat.find("files")], [t.name for t in cat.find("files")])
check("find: calculate", [t.name for t in cat.find("calculate expression")][:1] == ["calc"], [t.name for t in cat.find("calculate expression")])
check("find: nothing", cat.find("quantum teleportation") == [])
fnd = lambda q: [t.name for t in cat.find(q)]
check("find: \"calculate\" -> calc (Calculates)", fnd("calculate") == ["calc"], fnd("calculate"))
check("find: \"read file\" -> fs_read (Reads)", fnd("read file")[:1] == ["fs_read"], fnd("read file"))
check("find: \"save a note\" -> notes_write", fnd("save a note")[:1] == ["notes_write"], fnd("save a note"))
check("find: \"what is in the folder\" -> fs_list", fnd("what is in the folder")[:1] == ["fs_list"], fnd("what is in the folder"))
check("find: by name", fnd("fs read") [:1] == ["fs_read"] and fnd("notes")[:1] == ["notes_write"], (fnd("fs read"), fnd("notes")))
check("find: a short shared part (\"not\") does not stick \"nothing\" to \"notes\"", fnd("nothing") == [], fnd("nothing"))
check("get: full, case-insensitive, bare name", cat.get("tb.calc") is calc and cat.get("TB.CALC") is calc and cat.get("calc") is calc and cat.get("nope") is None)
amb = Catalog([T("a", "x", "", {}), T("b", "x", "", {})]); check("get: ambiguous bare name -> None", amb.get("x") is None and amb.get("a.x"))
check("validate: valid", validate_arguments(fsread, {"path": "a"}) == [])
check("validate: required missing", "required" in validate_arguments(fsread, {})[0])
check("validate: wrong type", "'5' is not of type 'integer'" in validate_arguments(fsread, {"path": "a", "max_chars": "5"})[0])
check("validate: not an object", validate_arguments(fsread, "x") == ["arguments must be a JSON object"])
check("meta-tools: 3 of them and compact", [t["function"]["name"] for t in META_TOOLS] == ["find_tool", "describe_tool", "call_tool"] and meta_tokens_ok if (meta_tokens_ok := meta_tools_tokens() < 300) else False, meta_tools_tokens())
check("savings: schemas cost more than the catalog", schema_tokens(tools) > 0)
check("default policy", default_policy(calc) == "auto" and default_policy(write) == "ask" and default_policy(T("s", "u", "", {}, ro=None)) == "ask")

class FakeManager:
    def __init__(self): self.calls = []; self.reply = ("result", False)
    def call(self, server, name, args, timeout): self.calls.append((server, name, args, timeout)); return self.reply
fm = FakeManager(); asked = []
def confirm_yes(tid, args): asked.append(tid); return True
def confirm_no(tid, args): asked.append(tid); return False
R = lambda **kw: ToolRouter(fm, cat, **kw)
r = R()
check("find_tool through the router", "tb.calc(expression: str)" in r.handle("find_tool", '{"query": "calculate"}'))
check("find_tool without query", r.handle("find_tool", "{}").startswith("ERROR"))
check("find_tool found nothing: list of all", "All tools: tb.calc" in r.handle("find_tool", '{"query": "quantum"}'))
fsread.input_schema["properties"]["path"]["title"] = "Path"   # FastMCP adds such fields to every schema
raw_describe = r.handle("describe_tool", '{"name": "tb.fs_read"}')
check("describe_tool: no auxiliary title and no indentation (fewer tokens)", '"title"' not in raw_describe and "\n" not in raw_describe, raw_describe)
check("signature: argument types", signature(T("s", "x", "", {"a": {"type": "array"}, "b": {"type": "boolean"}, "c": {}, "d": {"type": "number"}}, ["a"])) == "s.x(a: list, b?: bool, c?, d?: num)")
check("catalog header: do not make up results and do not promise actions", "do not make up a result" in text and "Do not promise to do something" in text and "The catalog signature is enough" in text)
d = json.loads(r.handle("describe_tool", '{"name": "tb.fs_read"}'))
check("describe_tool: schema", d["name"] == "tb.fs_read" and d["arguments_schema"]["required"] == ["path"] and d["read_only"] is True)
check("describe_tool: typo -> hint", "in the catalog" in r.handle("describe_tool", '{"name": "fs_reed"}') and "tb.fs_read" in r.handle("describe_tool", '{"name": "tb.fs_read2"}'))
check("call_tool: runs", r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1+1"}}') == "result"
      and fm.calls[-1] == ("tb", "calc", {"expression": "1+1"}, 60.0))
check("call_tool: arguments as a JSON string", r.handle("call_tool", json.dumps({"name": "tb.calc", "arguments": '{"expression": "2"}'})) == "result" and fm.calls[-1][2] == {"expression": "2"})
check("call_tool: bare name", r.handle("call_tool", '{"name": "calc", "arguments": {"expression": "3"}}') == "result")
n = len(fm.calls)
bad = r.handle("call_tool", '{"name": "tb.fs_read", "arguments": {}}')
check("call_tool: invalid arguments -> error with the schema, server not called", bad.startswith("ERROR: invalid arguments") and '"required"' in bad and len(fm.calls) == n, bad)
check("call_tool: unknown", "in the catalog" in r.handle("call_tool", '{"name": "tb.zzz", "arguments": {}}') and len(fm.calls) == n)
check("call_tool: broken JSON", r.handle("call_tool", '{"name": "tb.calc", "arguments": {').startswith("ERROR: the arguments are not valid JSON"))
check("call_tool: arguments not an object", r.handle("call_tool", '[1,2]').startswith("ERROR: the arguments must be a JSON object"))
check("calling a tool directly by name (the model got the form wrong)", r.handle("tb.calc", '{"expression": "4"}') == "result")
check("unknown function", "function \"hack\"" in r.handle("hack", "{}"))
fm.reply = ("x" * (MAX_RESULT_CHARS + 500), False); out = r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}')
check("a long result is truncated", len(out) < MAX_RESULT_CHARS + 120 and "[truncated: showing 12000 of 12500" in out)
fm.reply = ("file not found", True); check("a tool error is marked", r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}') == "ERROR: file not found")
fm.reply = ("", False); check("empty result", r.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}') == "(empty result)")
fm.reply = ("ok", False); n = len(fm.calls)
w = '{"name": "tb.notes_write", "arguments": {"name": "a", "content": "b"}}'
check("ask without confirm: refused, server not called", "declined" in R().handle("call_tool", w) and len(fm.calls) == n)
check("ask + confirm=no: refused", "declined" in R(confirm=confirm_no).handle("call_tool", w) and len(fm.calls) == n and asked[-1] == "tb.notes_write")
check("ask + confirm=yes: done", R(confirm=confirm_yes).handle("call_tool", w) == "ok" and len(fm.calls) == n + 1)
check("deny: does not ask and does not call", "blocked" in R(policy_for=lambda t: "deny", confirm=confirm_yes).handle("call_tool", w) and len(fm.calls) == n + 1)
asked.clear()
check("auto for a write tool: does not ask", R(policy_for=lambda t: "auto", confirm=confirm_no).handle("call_tool", w) == "ok" and not asked)

# the same tool error in a row: a hint on the second time, blocking on the third
rr = R(); c = '{"tool": "tb.calc", "arguments": {"expression": "1"}}'
fm.reply = ("Vision model is not set up", True); n = len(fm.calls)
o1, o2 = rr.handle("call_tool", c), rr.handle("call_tool", c)
check("repeated error: no hint the first time", o1 == "ERROR: Vision model is not set up", o1)
check("repeated error: the second time a hint not to repeat", o2.startswith("ERROR: Vision model is not set up") and "2 times in a row" in o2 and "without it" in o2, o2)
o3 = rr.handle("call_tool", c)
check("repeated error: the third call does not reach the server", len(fm.calls) == n + 2 and "was not called" in o3 and "Vision model is not set up" in o3, (len(fm.calls) - n, o3))
check("another tool is not blocked", rr.handle("call_tool", '{"tool": "tb.fs_read", "arguments": {"path": "a"}}').startswith("ERROR: Vision") and len(fm.calls) == n + 3)
check("a new reply (a new router) starts counting from zero", R().handle("call_tool", c) == "ERROR: Vision model is not set up")
r4 = R(); fm.reply = ("file not found: a", True); r4.handle("call_tool", c)
fm.reply = ("file not found: b", True); r4.handle("call_tool", c); n = len(fm.calls)
fm.reply = ("file not found: c", True)
check("different errors (trying paths) are not blocked", "was not called" not in r4.handle("call_tool", c) and len(fm.calls) == n + 1)
r5 = R(); fm.reply = ("failure", True); r5.handle("call_tool", c)
fm.reply = ("ok", False); check("a successful call resets the count", r5.handle("call_tool", c) == "ok" and "tb.calc" not in r5._errors)
fm.reply = ("failure", True); n = len(fm.calls); r5.handle("call_tool", c)
check("after a success errors are counted from one again", "was not called" not in r5.handle("call_tool", c) and len(fm.calls) == n + 2)
fm.reply = ("ok", False)

n0 = len(fm.calls)
check("call_tool: the main parameter is tool", r.handle("call_tool", '{"tool": "tb.calc", "arguments": {"expression": "5"}}') == "ok"
      and fm.calls[n0] == ("tb", "calc", {"expression": "5"}, 60.0), fm.calls[n0:])
fm.reply = ("written", False)
r_auto = ToolRouter(fm, cat, policy_for=lambda t: "auto")
n_calls = len(fm.calls)
out = r_auto.handle("call_tool", '{"tool": "tb.notes_write", "arguments": {"name": "demo", "content": "x"}}')
check("name collision: notes_write called with name=demo", out == "written" and fm.calls[n_calls][1] == "notes_write" and fm.calls[n_calls][2] == {"name": "demo", "content": "x"}, (out, fm.calls[n_calls:]))
out = r_auto.handle("call_tool", '{"arguments": {"content": "x", "name": "tb.notes_write"}}')
check("forgot tool: a clear error with the format", out.startswith("ERROR: no tool name given") and 'tool="server.tool"' in out, out)
check("describe_tool accepts tool", json.loads(r_auto.handle("describe_tool", '{"tool": "tb.fs_read"}'))["name"] == "tb.fs_read")
check("the fallback name parameter is still understood", r_auto.handle("call_tool", '{"name": "tb.calc", "arguments": {"expression": "1"}}') == "written")
check("meta-tools: the parameter is called tool", META_TOOLS[2]["function"]["parameters"]["required"] == ["tool", "arguments"] and META_TOOLS[1]["function"]["parameters"]["required"] == ["tool"])
from core.tools import describe_call, result_label
check("describe_call/result_label understand tool", describe_call("call_tool", '{"tool": "tb.calc", "arguments": {"expression": "1"}}') == 'tb.calc({"expression": "1"})'
      and result_label("call_tool", '{"tool": "tb.calc", "arguments": {}}') == "tb.calc" and result_label("find_tool", "{}") == "find_tool")
# ---------- parsing tool_calls from the stream ----------
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
check("tool_calls: chunks joined, two calls by index", out == [("tool_calls", [
    {"id": "c1", "name": "call_tool", "arguments": '{"name": "tb.calc", "arguments": {"expression": "1+1"}}'},
    {"id": "c2", "name": "find_tool", "arguments": '{"query": "x"}'}])], out)
check("tools and tool_choice go into the request", seen["body"]["tools"] == META_TOOLS and seen["body"]["tool_choice"] == "auto")
run_llm(lambda r: httpx.Response(200, content=sse({"choices": [{"delta": {"content": "a"}}]})))
out = run_llm(lambda r: (seen.update(body=json.loads(r.content)) or httpx.Response(200, content=sse({"choices": [{"delta": {"content": "a"}}]}))))
check("without tools: no field", "tools" not in seen["body"] and out == [("content", "a")])
out = run_llm(lambda r: httpx.Response(200, content=sse(tc({"function": {"name": "f", "arguments": {"a": 1}}}))))
check("no id and index: id is generated, dict arguments -> JSON", out[0][1][0]["id"].startswith("call_0_") and out[0][1][0]["arguments"] == '{"a": 1}', out)
out = run_llm(lambda r: httpx.Response(200, content=sse({"choices": [{"delta": {"content": "text"}}]}, tc({"index": 0, "id": "z", "function": {"name": "n", "arguments": "{}"}}))))
check("text and calls together: text first, calls at the end", [k for k, _ in out] == ["content", "tool_calls"])

# ---------- history with tools ----------
db.add_message(1, "user", "q") if False else None
cid = db.create_chat()
db.add_message(cid, "user", "calculate 1+1")
calls = [{"id": "c1", "type": "function", "function": {"name": "call_tool", "arguments": '{"name":"tb.calc","arguments":{"expression":"1+1"}}'}}]
db.add_messages(cid, [
    {"role": "assistant", "content": "", "reasoning": "need to calculate", "tool_calls": json.dumps(calls)},
    {"role": "tool", "content": "2", "tool_call_id": "c1", "tool_name": "call_tool"}])
db.add_message(cid, "assistant", "It is 2", "thought")
rows = db.list_messages(cid)
check("DB: messages with calls saved in full", [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
      and json.loads(rows[1]["tool_calls"]) == calls and rows[2]["tool_call_id"] == "c1" and rows[2]["tool_name"] == "call_tool" and rows[1]["reasoning"] == "need to calculate")
msgs = history_messages(rows)
check("history -> OpenAI format", msgs == [
    {"role": "user", "content": "calculate 1+1"},
    {"role": "assistant", "content": "", "tool_calls": calls},
    {"role": "tool", "tool_call_id": "c1", "content": "2"},
    {"role": "assistant", "content": "It is 2"}], msgs)
b = build_messages({"system_prompt": "SYS", "memory": "MEM"}, rows, [("f.txt", "T")], "CATALOG")
check("system: prompt, memory, catalog, files in order", b[0]["content"].index("SYS") < b[0]["content"].index("Memory") < b[0]["content"].index("CATALOG") < b[0]["content"].index("=== f.txt"))
check("old messages (without tool fields) are read", history_messages([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]) == [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
db.add_messages(cid, [{"role": "user", "content": "x"}, {"role": "tool", "content": "y"}])
n = len(db.list_messages(cid))
try:
    db.add_messages(cid, [{"role": "user", "content": "ok"}, {"content": "no role"}])
except KeyError: pass
check("add_messages is atomic: nothing is saved on error", len(db.list_messages(cid)) == n)
db.save_chat_tools(cid, {"tb.calc", "tb.fs_read"}); check("tools enabled in the chat", db.load_chat_tools(cid) == {"tb.calc", "tb.fs_read"})
db.save_chat_tools(cid, set()); check("... and clearing", db.load_chat_tools(cid) == set())
db.set_tool_policy("tb.calc", "deny"); db.set_tool_policy("tb.calc", "ask"); check("policies: write and overwrite", db.load_tool_policies() == {"tb.calc": "ask"})
db.set_tool_policy("tb.calc", None); check("policies: reset", db.load_tool_policies() == {})
db.delete_chat(cid); check("cascade: the chat's tools are deleted", db.load_chat_tools(cid) == set())

# ---------- agent loop (a scripted "model") ----------
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
    [{"choices": [{"delta": {"reasoning_content": "I will find a tool"}}]}, call("a1", "find_tool", {"query": "calculate"})],
    [call("a2", "call_tool", {"name": "tb.calc", "arguments": {"expression": "1+1"}})],
    [{"choices": [{"delta": {"content": "It is "}}]}, {"choices": [{"delta": {"content": "2"}}]}]])
ev = run_agent(script, router)
check("agent: finished without errors", ev["done"] == [1] and not ev["failed"], ev["failed"])
check("agent: two tool rounds", [r[0]["tool_calls"][0]["function"]["name"] for r in ev["rounds"]] == ["find_tool", "call_tool"])
check("agent: tool results", "tb.calc(expression: str)" in ev["rounds"][0][1][0]["content"] and ev["rounds"][1][1][0]["content"] == "2")
check("agent: the round's reasoning is saved", ev["rounds"][0][0]["reasoning"] == "I will find a tool")
check("agent: the final text is streamed", [t for k, t in ev["delta"] if k == "content"] == ["It is ", "2"])
check("agent: running events", [n for n, _ in ev["running"]] == ["find_tool", "call_tool"])
req3 = script.requests[2]["messages"]
check("agent: the 3rd request contains the whole chain", [m["role"] for m in req3] == ["user", "assistant", "tool", "assistant", "tool"]
      and req3[1]["tool_calls"][0]["id"] == "a1" and req3[2]["tool_call_id"] == "a1" and req3[4]["content"] == "2", req3)
check("agent: tools were sent in every request", all(r.get("tools") == META_TOOLS for r in script.requests))
ev = run_agent(Script([[{"choices": [{"delta": {"content": "just an answer"}}]}]]), router, tools=None)
check("without tools: a plain answer, no tools", ev["done"] == [1] and not ev["rounds"])
from worker import LIMIT_NUDGE
check("default step limit is 16", ChatStreamer.MAX_STEPS == 16 and db.DEFAULT_MAX_STEPS == 16)
script = Script([[call(f"loop{i}", "find_tool", {"query": "x"})] for i in range(ChatStreamer.MAX_STEPS)]
                + [[{"choices": [{"delta": {"content": "Summary: did this much, the rest is left."}}]}]])
ev = run_agent(script, router)
check("step limit: rounds, notice, finish", len(ev["rounds"]) == ChatStreamer.MAX_STEPS and ev["done"] == [1] and not ev["failed"]
      and any(k == "notice" and "tool step limit" in t for k, t in ev["delta"]))
check("step limit: the model summed up in text", [t for k, t in ev["delta"] if k == "content"] == ["Summary: did this much, the rest is left."])
last = script.requests[-1]
check("the final request has no tools and asks for a summary", len(script.requests) == ChatStreamer.MAX_STEPS + 1
      and "tools" not in last and last["messages"][-1] == {"role": "user", "content": LIMIT_NUDGE}, (len(script.requests), last["messages"][-1]))
check("the summary request does not go into the rounds (it will not be saved in the DB)", all(a[0].get("content") != LIMIT_NUDGE for a in ev["rounds"]))
script = Script([[call(f"c{i}", "find_tool", {"query": "x"})] for i in range(3)] + [[{"choices": [{"delta": {"content": "summary after 3 steps"}}]}]])
ev = run_agent(script, router, max_steps=3)
check("custom step limit (3): exactly 3 rounds, then a summary", len(ev["rounds"]) == 3 and len(script.requests) == 4 and ev["done"] == [1]
      and any(k == "notice" and "(3)" in t for k, t in ev["delta"]), (len(ev["rounds"]), len(script.requests)))
script = Script([[call("a1", "find_tool", {"query": "x"})], [{"choices": [{"delta": {"content": "done"}}]}]])
ev = run_agent(script, router, max_steps=1)
check("limit 1: one round, then a summary without tools", len(ev["rounds"]) == 1 and "tools" not in script.requests[-1])
ev = run_agent(Script([[call("z", "find_tool", {"query": "x"})], [{"choices": [{"delta": {"content": "ok"}}]}]]), router, max_steps=0)
check("max_steps=0 is treated as the default", len(ev["rounds"]) == 1 and ev["done"] == [1])
from worker import STUCK_NUDGE
fm_bad = FakeManager(); fm_bad.reply = ("Vision model is not set up", True); stuck_router = ToolRouter(fm_bad, cat)
cc = lambda i: call(f"v{i}", "call_tool", {"tool": "tb.calc", "arguments": {"expression": "1"}})
script = Script([[cc(i)] for i in range(10)] + [[{"choices": [{"delta": {"content": "It did not work: the tool is not set up."}}]}]])
script.steps_last = [{"choices": [{"delta": {"content": "It did not work: the tool is not set up."}}]}]
script.steps = [[cc(0)], [cc(1)], [cc(2)], script.steps_last]
ev = run_agent(script, stuck_router)
last = script.requests[-1]
check("looping on a broken tool: 3 rounds instead of 16", len(ev["rounds"]) == 3 and len(fm_bad.calls) == 2 and ev["done"] == [1] and not ev["failed"], (len(ev["rounds"]), len(fm_bad.calls)))
check("looping: notice, the final request without tools and asking for a summary", any(k == "notice" and "keeps returning the same error" in t for k, t in ev["delta"])
      and "tools" not in last and last["messages"][-1] == {"role": "user", "content": STUCK_NUDGE.format(tool="tb.calc")} and len(script.requests) == 4, last["messages"][-1])
check("looping: the model summed up in text", [t for k, t in ev["delta"] if k == "content"] == ["It did not work: the tool is not set up."])
ev = run_agent(Script([[call("x1", "find_tool", {"query": "x"})], [{"choices": [{"delta": {"content": "will not get here"}}]}]],
                      delay_from=2, delay=0.4), router, cancel_after_round=True)
check("cancel after a round: it goes no further", len(ev["rounds"]) == 1 and not ev["done"] and "will not get here" not in [t for _, t in ev["delta"]], ev)
class SlowRouter:
    def __init__(self): self.th = None
    def handle(self, n, a): self.th.cancel(); return "too late"
sr = SlowRouter(); real = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(Script([[call("s1", "find_tool", {"query": "x"})]]).handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
th = ChatStreamer(8, "http://x/v1", "", "m", [], 0.1, 10, tools=META_TOOLS, router=sr); sr.th = th
rounds = []; th.tool_round.connect(lambda s, a, r: rounds.append(1)); th.start(); th.wait(10000)
for _ in range(10): app.processEvents()
llm.httpx.stream = real
check("cancel while a tool runs: the round is not saved", rounds == [])
ev = run_agent(Script([[{"choices": [{"delta": {}}]}]]), router)
check("empty model reply: finish without calls", ev["done"] == [1] and not ev["rounds"])
class Boom:
    def handle(self, n, a): raise RuntimeError("crashed")
ev = run_agent(Script([[call("b1", "find_tool", {})]]), Boom())
check("exception in a tool -> failed, the thread is alive", ev["failed"] and "crashed" in ev["failed"][0] and not ev["done"], ev)
print("ALL OK" if ok_all else "SOME FAILED")




