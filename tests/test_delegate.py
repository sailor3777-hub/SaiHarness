"""Subagents: delegate(agent, task) in the router, core.subagent.run, the agents choice on the Tools tab, the window."""
import json, sys, time, pathlib, tempfile, threading
import httpx
from PySide6.QtWidgets import QApplication, QCheckBox, QWidgetAction
from core import db, llm
db.DB_PATH = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(tempfile.mkdtemp()) / "deleg.db"
from core import subagent
from core.mcp_manager import ToolInfo
from core.tools import Catalog, DELEGATE_TOOL, MAX_RESULT_CHARS, META_TOOLS, ToolRouter, WithLog, meta_tools_tokens

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

# ---------- database ----------
db.save_connection("lm", "LM Studio", "http://127.0.0.1:9/v1", "")
db.save_profile("Main", "lm", "main-model", "MAIN SYSTEM", 0.5, 500, "")
db.save_profile("Coder", "lm", "coder-model", "You are a careful coder.\nSecond line.", 0.2, 400, "", 4,
                agent_description="Writes and tests PowerShell scripts")
check("profile: agent description saved", db.get_profile("Coder")["agent_description"] == "Writes and tests PowerShell scripts")
db.save_profile("Coder", "lm", "coder-model", "You are a careful coder.\nSecond line.", 0.2, 400, "", 4)
check("profile: saving without a description keeps it", db.get_profile("Coder")["agent_description"] == "Writes and tests PowerShell scripts")
db.save_profile("Plain", "", "x", "  Checks texts   for mistakes.  ", 0.5, 100, "")
check("profile: a new profile gets an empty description", db.get_profile("Plain")["agent_description"] == "")
cid = db.create_chat()
db.save_chat_agents(cid, {"Coder", "Plain"}); check("chat agents: saved", db.load_chat_agents(cid) == {"Coder", "Plain"})
db.save_chat_agents(cid, set()); check("chat agents: cleared", db.load_chat_agents(cid) == set())
db.save_chat_agents(cid, {"Coder"}); db.delete_chat(cid); check("chat agents: deleted with the chat", db.load_chat_agents(cid) == set())

# ---------- catalog ----------
coder, plain = db.get_profile("Coder"), db.get_profile("Plain")
check("description: own text", subagent.description(coder) == "Writes and tests PowerShell scripts")
check("description: falls back to the system prompt, spaces collapsed", subagent.description(plain) == "Checks texts for mistakes.")
check("description: long text is cut", len(subagent.description({"agent_description": "x" * 500, "system_prompt": ""})) == subagent.DESCRIPTION_MAX)
block = subagent.catalog_block([coder, plain])
check("catalog block: head and one line per agent", block.startswith("Agents. delegate(agent, task)")
      and "- Coder — Writes and tests PowerShell scripts" in block and "- Plain — Checks texts for mistakes." in block, block)
check("catalog block: empty without agents", subagent.catalog_block([]) == "")
check("find_agent: exact and case-insensitive", subagent.find_agent([coder, plain], "coder")["name"] == "Coder" and subagent.find_agent([coder], "Nobody") is None)
check("delegate meta-tool costs tokens only when on", meta_tools_tokens(True) > meta_tools_tokens() and DELEGATE_TOOL["function"]["name"] == "delegate")

# ---------- router ----------
def T(server, name, ro=True, props=None, req=()):
    return ToolInfo(server, name, f"{name} tool.", {"type": "object", "properties": props or {}, "required": list(req)}, read_only=ro)
calc = T("tb", "calc", props={"expression": {"type": "string"}}, req=["expression"])
write = T("tb", "notes_write", ro=False, props={"name": {"type": "string"}}, req=["name"])
cat = Catalog([calc, write])
class FakeManager:
    def __init__(self): self.calls = []
    def call(self, server, name, args, timeout): self.calls.append((name, args)); return (f"{name} ok", False)
r = ToolRouter(FakeManager(), cat)
check("router: delegate without a delegator is an error", r.handle("delegate", '{"agent": "Coder", "task": "x"}').startswith("ERROR: delegate is not available"))
check("router: unknown function lists only the 3 meta-tools", "call_tool." in r.handle("nope", "{}"))
seen = []
r.delegate = lambda agent, task: (seen.append((agent, task)) or (WithLog("done", '{"agent": "Coder"}'), False))
out = r.handle("delegate", '{"agent": " Coder ", "task": " write it "}')
check("router: delegate passes trimmed agent and task", seen == [("Coder", "write it")] and out == "done")
check("router: the log stays attached to the result", getattr(out, "agent_log", "") == '{"agent": "Coder"}')
check("router: delegate without task is an error", r.handle("delegate", '{"agent": "Coder"}').startswith("ERROR: pass agent"))
check("router: unknown function lists delegate when it is on", "delegate." in r.handle("nope", "{}"))
r.delegate = lambda agent, task: (WithLog("x" * (MAX_RESULT_CHARS + 50), "LOG"), False)
out = r.handle("delegate", '{"agent": "Coder", "task": "t"}')
check("router: a long result is cut, the log is kept", len(out) < MAX_RESULT_CHARS + 100 and "[truncated" in out and out.agent_log == "LOG")
calls = []
r.delegate = lambda agent, task: (calls.append(1) or ("there is no agent", True))
outs = [r.handle("delegate", '{"agent": "Ghost", "task": "t"}') for _ in range(3)]
check("router: delegate errors get the prefix and the repeat hint", outs[0].startswith("ERROR: there is no agent") and "2 times in a row" in outs[1])
check("router: the third identical error is not called, the loop is told to stop", len(calls) == 2 and "was not called" in outs[2] and r.stuck == "delegate to Ghost")

# ---------- subagent.run with a scripted model ----------
def sse(*chunks): return ("".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
def tcall(i, name, args): return [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": i, "function": {"name": name, "arguments": json.dumps(args)}}]}}]}]
def say(text, reasoning=""):
    return ([{"choices": [{"delta": {"reasoning_content": reasoning}}]}] if reasoning else []) + ([{"choices": [{"delta": {"content": text}}]}] if text else [])
class LM:
    def __init__(self): self.steps = []; self.requests = []; self.on_request = None
    def script(self, *steps): self.steps = list(steps); self.requests = []
    def handler(self, req):
        self.requests.append(json.loads(req.content))
        if self.on_request: self.on_request(len(self.requests))
        if not self.steps: return httpx.Response(500, json={"error": "no more steps in the script"})
        step = self.steps.pop(0)
        if isinstance(step, httpx.Response): return step
        return httpx.Response(200, content=sse(*step))
lm = LM(); real_stream = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
conn = db.get_connection("lm")
asked, progress = [], []
def setup(catalog=cat, policy=None, cancel=None, manager=None):
    return subagent.Setup(manager=manager or FakeManager(), catalog=catalog, policy_for=policy or (lambda t: "auto" if t.read_only else "ask"),
                          confirm=lambda who, tid, args: (asked.append((who, tid)) or True),
                          tools_text=lambda p: f"CATALOG FOR {p['name']}", cancel=cancel or threading.Event(),
                          progress=progress.append, context_info=lambda u, k, m: (None, False, None))
lm.script(tcall("s1", "call_tool", {"tool": "tb.calc", "arguments": {"expression": "6*7"}}),
          tcall("s2", "call_tool", {"tool": "tb.notes_write", "arguments": {"name": "n"}}),
          say("The answer is 42.", "thinking"))
mgr = FakeManager()
text, err = subagent.run(coder, conn, "Compute 6*7", setup(manager=mgr))
req = lm.requests[0]
check("run: finished without error", not err and text == "Result from agent Coder:\nThe answer is 42.", text)
check("run: the agent's own system prompt, catalog for its profile, no chat history",
      [m["role"] for m in req["messages"]] == ["system", "user"] and req["messages"][0]["content"].startswith("You are a careful coder.")
      and "CATALOG FOR Coder" in req["messages"][0]["content"], req["messages"])
check("run: the task with the agent note", req["messages"][1]["content"].startswith("Compute 6*7") and subagent.TASK_NOTE in req["messages"][1]["content"])
check("run: the agent's model, temperature and token limit", req["model"] == "coder-model" and req["temperature"] == 0.2 and req["max_tokens"] == 400)
check("run: the agent gets the 3 meta-tools, not delegate", [t["function"]["name"] for t in req["tools"]] == ["find_tool", "describe_tool", "call_tool"])
check("run: the chat's tools are called through the shared manager", mgr.calls == [("calc", {"expression": "6*7"}), ("notes_write", {"name": "n"})])
check("run: confirmations name the agent", asked == [("Coder", "tb.notes_write")], asked)
check("run: progress shows the agent and its step", any(p.startswith("Coder: step 1/4") for p in progress), progress)
log = json.loads(text.agent_log)
check("run: the log has the task, steps with calls and results, the answer", log["task"] == "Compute 6*7" and len(log["steps"]) == 2
      and log["steps"][0]["calls"][0]["result"] == "calc ok" and log["final"] == "The answer is 42." and log["reasoning"] == "thinking", log)
md = subagent.render_log(text.agent_log)
check("render_log: Markdown with steps and the answer", "### Step 1" in md and "### Answer" in md and "calc ok" in md, md)
check("render_log: damaged log gives nothing", subagent.render_log("{oops") == "" and subagent.render_log("") == "")

asked.clear()
lm.script(tcall("d1", "call_tool", {"tool": "tb.notes_write", "arguments": {"name": "n"}}), say("Could not write."))
mgr = FakeManager()
text, err = subagent.run(coder, conn, "write", setup(manager=mgr, policy=lambda t: "deny" if not t.read_only else "auto"))
check("run: a blocked tool stays blocked for the agent", not mgr.calls and not asked and "blocked" in lm.requests[1]["messages"][-1]["content"])

lm.script(say("plain answer"))
text, err = subagent.run(coder, conn, "no tools", setup(catalog=None))
check("run: without the chat's tools the agent gets no tools", "tools" not in lm.requests[0] and "CATALOG" not in lm.requests[0]["messages"][0]["content"] and not err)

lm.script(say("", "The whole answer is here"))
text, err = subagent.run(coder, conn, "t", setup())
check("run: an answer that arrived as reasoning counts", not err and text.endswith("The whole answer is here"), text)
lm.script(say(""))
text, err = subagent.run(coder, conn, "t", setup())
check("run: an empty reply is an error", err and "empty reply" in text and text.agent_log)
lm.script(httpx.Response(400, json={"error": "model not found"}))
text, err = subagent.run(coder, conn, "t", setup())
check("run: a server error is returned as an error with a log", err and text.startswith("agent Coder failed") and json.loads(text.agent_log)["error"], text)
text, err = subagent.run(coder, None, "t", setup())
check("run: no connection", err and "no connection" in text)
stop = threading.Event()
lm.script(tcall("c1", "call_tool", {"tool": "tb.calc", "arguments": {"expression": "1"}}), say("never"))
lm.on_request = lambda n: stop.set() if n == 1 else None
text, err = subagent.run(coder, conn, "t", setup(cancel=stop))
lm.on_request = None
check("run: Stop (the chat's cancel event) stops the agent", err and "stopped" in text and len(lm.requests) == 1, text)
lm.script(say("too big"))
text, err = subagent.run(coder, conn, "t", subagent.Setup(catalog=None, context_info=lambda u, k, m: (80, True, 80)))
check("run: the agent's own context limit is checked", err and "context is full" in text, text)

# ---------- the window: agents menu, delegate end to end ----------
from ui.main_window import MainWindow
app = QApplication([])
def wait(cond, timeout=30.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()
tmp = pathlib.Path(tempfile.mkdtemp())
w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; v = w.chat_view
check("servers connect", wait(lambda: tp.states and not tp.connect_req.busy, 40) and tp.states["toolbox"].status == "ready", tp.states)
w.selector.reload(select="Main"); w._free_choice = "Main"; w._refresh_lock()
tp._fill_agents_menu()
boxes = {a.defaultWidget().text(): a.defaultWidget() for a in tp.agents_menu.actions() if isinstance(a, QWidgetAction)}
check("agents menu: every profile with a checkbox", set(boxes) == {"Coder", "Main", "Plain"} and not any(b.isChecked() for b in boxes.values()), boxes)
changed = []; tp.selection_changed.connect(lambda: changed.append(1))
boxes["Coder"].setChecked(True)
check("agents menu: a checkbox enables the agent", tp.agents == {"Coder"} and tp.agents_btn.text() == "Coder" and changed, tp.agents_btn.text())
check("agents: same server, another model: a reload warning", "LM Studio may reload" in tp.agents_status.text(), tp.agents_status.text())
check("agents: info and token estimate include the agents", "agents: 1" in tp.info.text() and tp.prompt_tokens() > 0, tp.info.text())
tp.set_enabled({"toolbox.calc"})
lm.script(tcall("m1", "delegate", {"agent": "coder", "task": "Compute 6*7 with calc"}),
          tcall("a1", "call_tool", {"tool": "toolbox.calc", "arguments": {"expression": "6*7"}}),
          say("6*7 = 42"),
          say("Coder says: 42"))
v.input.setPlainText("how much is 6*7? ask the coder"); v.send_btn.click()
check("chat created with the draft's agents", w.chat_id is not None and db.load_chat_agents(w.chat_id) == {"Coder"})
check("the reply finished", wait(lambda: w.stream is None, 60))
main1, sub1, sub2, main2 = lm.requests
check("main request: delegate among the tools, the agents block in the system message",
      [t["function"]["name"] for t in main1["tools"]] == ["find_tool", "describe_tool", "call_tool", "delegate"]
      and "- Coder — Writes and tests PowerShell scripts" in main1["messages"][0]["content"] and main1["model"] == "main-model")
check("subagent request: its model and prompt, no chat history, no delegate",
      sub1["model"] == "coder-model" and sub1["messages"][0]["content"].startswith("You are a careful coder.")
      and "toolbox.calc" in sub1["messages"][0]["content"] and "Agents." not in sub1["messages"][0]["content"]
      and [m["role"] for m in sub1["messages"]] == ["system", "user"] and "how much" not in json.dumps(sub1["messages"])
      and "delegate" not in [t["function"]["name"] for t in sub1["tools"]])
check("subagent: the real calc tool ran", sub2["messages"][-1]["content"] == "42", sub2["messages"][-1])
check("main model got only the agent's answer", main2["messages"][-1] == {"role": "tool", "tool_call_id": "m1", "content": "Result from agent Coder:\n6*7 = 42"}, main2["messages"][-1])
rows = db.list_messages(w.chat_id)
tool_row = next(r for r in rows if r["role"] == "tool")
check("chat: the delegate result is saved with the agent's log", tool_row["content"].endswith("6*7 = 42") and json.loads(tool_row["agent_log"])["final"] == "6*7 = 42")
check("chat: the final answer", rows[-1]["role"] == "assistant" and rows[-1]["content"] == "Coder says: 42")
check("chat view: an \"Agent's work\" link under the result", "agentlog:" in v.history.toHtml() and "Agent's work" in v.history.toPlainText())
check("Request window: delegate is listed", "delegate" in w.last_requests[w.chat_id]["tools"])
db.delete_profile("Coder")
check("a deleted agent profile disappears from the chat's agents", tp.enabled_agents() == [] and db.load_chat_agents(w.chat_id) == {"Coder"})
lm.script(say("ok"))
v.input.setPlainText("again"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
check("... and from the request", "delegate" not in [t["function"]["name"] for t in lm.requests[0].get("tools", [])]
      and "Agents." not in lm.requests[0]["messages"][0]["content"])
w.new_chat()
check("a new chat starts without agents", tp.agents == set() and db.load_chat_agents(w.chat_id) == set())
llm.httpx.stream = real_stream
w.close()
print("ALL OK" if ok_all else "SOME FAILED")
