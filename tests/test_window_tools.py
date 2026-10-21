import json, os, sys, time, pathlib, tempfile, threading
import httpx
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget
from core import db, llm
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t23.db"
from ui.main_window import MainWindow
from ui.permission import PermissionBroker
from ui.tools_panel import ID_ROLE, KIND_ROLE
from core.tools import META_TOOLS

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

app = QApplication([])
def wait(cond, timeout=30.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()

proj = tmp / "proj"; proj.mkdir(); (proj / "secret.txt").write_text("Access code: PURPLE-MAMMOTH-42\n", encoding="utf-8")
db.add_root(str(proj))

# ---------- a scripted "model" ----------
def sse(*chunks): return ("".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
def tcall(i, name, args): return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": i, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}
def say(text, reasoning=""):
    ch = ([{"choices": [{"delta": {"reasoning_content": reasoning}}]}] if reasoning else []) + [{"choices": [{"delta": {"content": text}}]}]
    return ch
class LM:
    def __init__(self): self.steps = []; self.requests = []
    def script(self, *steps): self.steps = list(steps); self.requests = []
    def handler(self, req):
        self.requests.append(json.loads(req.content))
        return httpx.Response(200, content=sse(*self.steps.pop(0)))
lm = LM(); real_stream = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))

w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; v = w.chat_view; sel = w.selector; ctx = w.context
check("servers connect in the background", wait(lambda: tp.states and not tp.connect_req.busy, 40) and tp.states["toolbox"].status == "ready", tp.states)
top = tp.tree.topLevelItem(0)
check("tree: a server and 22 tools, all off", top.childCount() == 22 and top.checkState(0) == Qt.Unchecked
      and all(top.child(i).checkState(0) == Qt.Unchecked for i in range(22)) and "toolbox" in top.text(0))
names = {top.child(i).text(0) for i in range(22)}
check("permission tag on a write tool", any(n.startswith("notes_write") and "[asks]" in n for n in names) and "calc" in names, names)
check("info: no tools enabled", "No tools enabled" in tp.info.text() and tp.prompt_tokens() == 0, tp.info.text())

# profile and connection (the model server is faked, the address is fictitious)
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1"); c.save_btn.click()
b = ctx.builder; wait(lambda: not b.models_req.busy)
b.name.setText("p"); b.base_model.setCurrentText("fake-model"); b.max_tokens.setValue(500); b.save_btn.click()

# enable the whole server BEFORE the chat is created (draft)
top.setCheckState(0, Qt.Checked)
check("the server checkbox enables all tools", len(tp.enabled) == 22 and tp.enabled == {t.id for t in tp.tools} and w.chat_id is None)
check("info: catalog tokens and savings", "Enabled: 22" in tp.info.text() and "Full schemas would take" in tp.info.text() and tp.prompt_tokens() > 0, tp.info.text())
extra_tokens = w.files_panel._extra_tokens
check("\"whole request\" includes the catalog", extra_tokens >= tp.prompt_tokens() > 0)

# ---------- scenario 1: search -> call -> answer ----------
lm.script(
    say("", "Need to calculate") + [tcall("t1", "find_tool", {"query": "calculate"})],
    [tcall("t2", "call_tool", {"tool": "toolbox.calc", "arguments": {"expression": "17*3"}})],
    say("Answer: **51**"))
v.input.setPlainText("What is 17*3?"); v.send_btn.click()
check("chat created, tools carried over from the draft", w.chat_id is not None and db.load_chat_tools(w.chat_id) == tp.enabled)
check("the model finished", wait(lambda: w.stream is None, 60))
rows = db.list_messages(w.chat_id)
check("in the DB: user, (call, result)×2, answer", [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant", "tool", "assistant"], [r["role"] for r in rows])
check("the first call is saved with its reasoning", json.loads(rows[1]["tool_calls"])[0]["function"]["name"] == "find_tool" and rows[1]["reasoning"] == "Need to calculate")
check("the search finds calc by an English query", "toolbox.calc(expression: str)" in rows[2]["content"] and rows[2]["tool_name"] == "find_tool", rows[2]["content"])
check("calc result from the real MCP server", rows[4]["content"] == "51" and rows[4]["tool_name"] == "toolbox.calc", rows[4]["content"])
check("final answer", rows[5]["content"] == "Answer: **51**")
req1, req3 = lm.requests[0], lm.requests[2]
check("the request has only 3 meta-tools", req1["tools"] == META_TOOLS and len(req1["tools"]) == 3)
sysmsg = req1["messages"][0]["content"]
check("catalog in the system message", "toolbox.calc(expression: str)" in sysmsg and "toolbox.fs_read(path: str, max_chars?: int, offset?: int)" in sysmsg and "toolbox.notes_write(name: str, content: str)" in sysmsg, sysmsg[:300])
check("3rd request: the whole chain of calls and results", [m["role"] for m in req3["messages"]] == ["system", "user", "assistant", "tool", "assistant", "tool"]
      and req3["messages"][5]["content"] == "51")
hist = v.history.toPlainText()
check("calls and results are visible in the chat", "tool search: \"calculate\"" in hist and "toolbox.calc({\"expression\": \"17*3\"})" in hist and "51" in hist, hist)
check("the round's reasoning is visible", "Need to calculate" in hist)
check("status after finishing", "tool calls" not in v.status.text() and w.stream is None and v.send_btn.isEnabled(), v.status.text())
check("\"Request\" keeps the list of functions", w.last_requests[w.chat_id]["tools"] == ["find_tool", "describe_tool", "call_tool"])

# ---------- reading a file from an allowed folder; a foreign file is unavailable ----------
outside = tmp / "outside.txt"; outside.write_text("secret outside the roots", encoding="utf-8")
lm.script([tcall("f1", "call_tool", {"tool": "toolbox.fs_read", "arguments": {"path": str(proj / "secret.txt")}})],
          [tcall("f2", "call_tool", {"tool": "toolbox.fs_read", "arguments": {"path": str(outside)}})],
          say("done"))
v.input.setPlainText("read the files"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
tools_rows = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-2:]
check("fs_read reads a file from the root", "PURPLE-MAMMOTH-42" in tools_rows[0]["content"], tools_rows[0]["content"])
check("fs_read outside the roots is refused by the server", "outside the allowed" in tools_rows[1]["content"] and "secret outside the roots" not in tools_rows[1]["content"], tools_rows[1]["content"])

# ---------- invalid arguments: an error with the schema, the model can correct itself ----------
lm.script([tcall("e1", "call_tool", {"tool": "toolbox.calc", "arguments": {}})],
          [tcall("e2", "call_tool", {"tool": "toolbox.calc", "arguments": {"expression": "2+2"}})], say("4"))
v.input.setPlainText("calculate"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
tr = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-2:]
check("invalid arguments: a hint with the schema, then success", tr[0]["content"].startswith("ERROR: invalid arguments") and '"required"' in tr[0]["content"] and tr[1]["content"] == "4", tr)
check("the error is highlighted in the chat", "ERROR: invalid arguments" in v.history.toPlainText())

# ---------- permissions: ask -> refuse / allow; auto; deny ----------
notes = tmp / "notes" / "idea.md"
asked = []
def answer(result):
    def q(parent, title, text, *a, **k): asked.append(text); return result
    return staticmethod(q)
write = lambda: [tcall("w1", "call_tool", {"tool": "toolbox.notes_write", "arguments": {"name": "idea", "content": "my note"}})]
QMessageBox.question = answer(QMessageBox.No)
lm.script(write(), say("ok")); v.input.setPlainText("write it down"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("write: asked, the user refused, no file", len(asked) == 1 and "toolbox.notes_write" in asked[0] and "Text: my note" in asked[0] and "Note: idea" in asked[0]
      and "declined" in last["content"] and not notes.exists(), (asked, last["content"]))
QMessageBox.question = answer(QMessageBox.Yes); asked.clear()
lm.script(write(), say("ok")); v.input.setPlainText("write it down again"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("write: allowed, the file is created", len(asked) == 1 and notes.exists() and notes.read_text(encoding="utf-8") == "my note" and '"saved"' in last["content"], last["content"])
tp.set_policy("toolbox.notes_write", "auto"); asked.clear(); notes.unlink()
lm.script(write(), say("ok")); v.input.setPlainText("and again"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
check("auto: no question", asked == [] and notes.exists())
tp.set_policy("toolbox.notes_write", "deny"); notes.unlink()
lm.script(write(), say("ok")); v.input.setPlainText("and once more"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("deny: no question, blocked, no file", asked == [] and "blocked" in last["content"] and not notes.exists(), last["content"])
check("the policy is saved in the DB and shown in the tree", db.load_tool_policies() == {"toolbox.notes_write": "deny"}
      and any("[blocked]" in tp.tree.topLevelItem(0).child(i).text(0) for i in range(22)))
tp.set_policy("toolbox.notes_write", None); check("policy reset", db.load_tool_policies() == {} and tp.policy_for(next(t for t in tp.tools if t.name == "notes_write")) == "ask")

# ---------- auto-approve: "ask" runs without the window, "deny" stays blocked ----------
from ui.tools_panel import AUTO_APPROVE_KEY
check("auto-approve is off by default, no warning", not tp.auto_approve.isChecked() and tp.auto_warning.isHidden() and db.get_setting(AUTO_APPROVE_KEY) == "")
QMessageBox.question = answer(QMessageBox.No); asked.clear()
tp.auto_approve.setChecked(True)
check("auto-approve: saved in the DB, warning shown", db.get_setting(AUTO_APPROVE_KEY) == "1" and not tp.auto_warning.isHidden())
lm.script(write(), say("ok")); v.input.setPlainText("note with auto-approve"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
check("auto-approve: no question, the file is written", asked == [] and notes.exists() and notes.read_text(encoding="utf-8") == "my note")
check("auto-approve does not change the stored policy or the tree tag", tp.policy_for(next(t for t in tp.tools if t.name == "notes_write")) == "ask"
      and any(tp.tree.topLevelItem(0).child(i).text(0).startswith("notes_write") and "[asks]" in tp.tree.topLevelItem(0).child(i).text(0) for i in range(22)))
tp.set_policy("toolbox.notes_write", "deny"); notes.unlink()
lm.script(write(), say("ok")); v.input.setPlainText("blocked with auto-approve"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("auto-approve: deny stays blocked", asked == [] and "blocked" in last["content"] and not notes.exists(), last["content"])
tp.set_policy("toolbox.notes_write", None); tp.auto_approve.setChecked(False)
check("auto-approve off: the setting is removed, the warning hidden", db.get_setting(AUTO_APPROVE_KEY) == "" and tp.auto_warning.isHidden())
lm.script(write(), say("ok")); v.input.setPlainText("note without auto-approve"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
check("auto-approve off: asks again", len(asked) == 1 and not notes.exists())

# ---------- the model calls a tool that is not in the chat ----------
tp.enabled.discard("toolbox.time_now"); tp._sync(); w._on_tools_changed()
lm.script([tcall("n1", "call_tool", {"tool": "toolbox.time_now", "arguments": {}})], say("no"))
v.input.setPlainText("what time is it?"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("a disabled tool is unavailable to the model", "in the catalog" in last["content"], last["content"])
check("the catalog in the request has no disabled tool", "time_now" not in lm.requests[0]["messages"][0]["content"])

# ---------- second chat: tools are off, no tools go into the request ----------
w.new_chat(); chat2 = w.chat_id
check("new chat: tools are off", tp.enabled == set() and "No tools enabled" in tp.info.text())
lm.script(say("hello!")); v.input.setPlainText("hello"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
check("without tools: no tools and no catalog in the request", "tools" not in lm.requests[0] and "toolbox" not in json.dumps(lm.requests[0], ensure_ascii=False)
      and w.last_requests[chat2]["tools"] == [])
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) != chat2: w.chat_list.list.setCurrentRow(i)
check("back to the first chat: the tool set is restored", len(tp.enabled) == 21 and "toolbox.time_now" not in tp.enabled
      and tp.tree.topLevelItem(0).checkState(0) == Qt.PartiallyChecked)
check("chat history with calls is shown after reloading from the DB", "tool search" in v.history.toPlainText() and "◂ toolbox.calc" in v.history.toPlainText())

# ---------- a bad config and a restart ----------
cfg = json.loads((tmp / "mcp.json").read_text(encoding="utf-8"))
cfg["mcpServers"]["broken"] = {"command": sys.executable, "args": ["-c", "import sys; print('boom', file=sys.stderr); sys.exit(2)"]}
cfg["mcpServers"]["off"] = {"command": "x", "disabled": True}
(tmp / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
tp.reconnect_btn.click(); check("the button is disabled while connecting", not tp.reconnect_btn.isEnabled())
check("the restart finished", wait(lambda: not tp.connect_req.busy and "broken" in tp.states, 60))
texts = [tp.tree.topLevelItem(i).text(0) for i in range(tp.tree.topLevelItemCount())]
check("in the tree: working, broken, disabled", any(t.startswith("●") for t in texts) and any(t.startswith("✗ broken") for t in texts) and any(t.startswith("○ off") for t in texts), texts)
broken = next(tp.tree.topLevelItem(i) for i in range(tp.tree.topLevelItemCount()) if tp.tree.topLevelItem(i).text(0).startswith("✗"))
check("the broken one shows the error text", broken.childCount() == 1 and broken.child(0).text(0), broken.child(0).text(0))
check("enabled tools survived the restart", len(tp.enabled) == 21 and len(tp.enabled_tools()) == 21)
(tmp / "mcp.json").write_text("{ this is not json", encoding="utf-8"); tp.reconnect_btn.click(); wait(lambda: not tp.connect_req.busy)
check("broken config: a clear error, the app is alive", "Could not connect the servers" in tp.info.text(), tp.info.text())

# ---------- an error and a stop leave a "System" line in the chat ----------
tp.enabled = set(); tp._sync()            # no tools, to check only this
w.new_chat(); sys_chat = w.chat_id
healthy = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))).stream(m, u, json=k.get("json"), headers=k.get("headers"))
v.input.setPlainText("first question"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
rows = db.list_messages(sys_chat)
check("server error: a \"System\" line in the DB", [r["role"] for r in rows] == ["user", "system"] and rows[1]["content"] == "Server error 500: boom", [(r["role"], r["content"]) for r in rows])
check("the error is visible in the chat", "System" in v.history.toPlainText() and "Server error 500: boom" in v.history.toPlainText())
check("the status shows the error too", "boom" in v.status.text())
def slow(req):
    time.sleep(1.5); return httpx.Response(200, content=sse(say("too late")))
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(slow)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
v.input.setPlainText("second question"); v.send_btn.click(); wait(lambda: w.stream is not None and False, 0.3); w.stop_stream()
rows = db.list_messages(sys_chat)
check("stop before the first token: a \"Stopped\" line, no answer", [r["role"] for r in rows][-2:] == ["user", "system"] and rows[-1]["content"] == "Stopped", [(r["role"], r["content"]) for r in rows])
check("the chat shows \"Stopped\"", v.history.toPlainText().count("Stopped") == 1)
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
time.sleep(1.7); app.processEvents()
lm.script(say("answer after the errors")); v.input.setPlainText("third question"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
sent = lm.requests[0]["messages"]
check("\"System\" lines are not sent to the model", [m["role"] for m in sent] == ["user", "user", "user"], [m["role"] for m in sent])
check("after errors the dialog goes on", db.list_messages(sys_chat)[-1]["content"] == "answer after the errors")
reopened = MainWindow.__new__  # (a formality: reloading from the DB is checked below)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) != sys_chat: w.chat_list.list.setCurrentRow(i)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == sys_chat: w.chat_list.list.setCurrentRow(i)
lm.script([{"choices": [{"delta": {}}]}]); v.input.setPlainText("and now an empty reply"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
rows = db.list_messages(sys_chat)
check("empty model reply: a \"System\" line, not silence", rows[-1]["role"] == "system" and rows[-1]["content"] == "The model returned an empty reply", [(r["role"], r["content"]) for r in rows[-2:]])
check("the empty reply is visible in the chat", "The model returned an empty reply" in v.history.toPlainText())
lm.script([{"choices": [{"delta": {}, "finish_reason": "length"}]}]); v.input.setPlainText("and once more"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
check("empty reply because of the token limit: the reason is in the line", db.list_messages(sys_chat)[-1]["content"] == "The model returned an empty reply (Max tokens limit reached, the reply was cut off)", db.list_messages(sys_chat)[-1]["content"])
lm.script(say("a normal answer")); v.input.setPlainText("and a normal one"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
check("after empty replies the dialog goes on, no extra lines", [r["role"] for r in db.list_messages(sys_chat)][-2:] == ["user", "assistant"])
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) != sys_chat: w.chat_list.list.setCurrentRow(i)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == sys_chat: w.chat_list.list.setCurrentRow(i)
check("\"System\" lines are restored from the DB", v.history.toPlainText().count("Stopped") == 1 and "boom" in v.history.toPlainText())

# the answer arrived as reasoning with empty content (Qwen 3.5 in LM Studio): it is saved as the answer
check("answer_in_reasoning: clean cases", llm.answer_in_reasoning("", "text") and not llm.answer_in_reasoning("answer", "thoughts")
      and not llm.answer_in_reasoning("", "  ") and not llm.answer_in_reasoning("", "thoughts", llm.LENGTH_NOTICE) and not llm.answer_in_reasoning(" \n", ""))
lm.script([{"choices": [{"delta": {"reasoning_content": "Delivery code: KAPPA-731"}}]}]); v.input.setPlainText("answer in the reasoning"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
last = db.list_messages(sys_chat)[-1]
check("an answer in the reasoning is saved as the answer", last["role"] == "assistant" and last["content"] == "Delivery code: KAPPA-731" and last["reasoning"] == "", (last["role"], last["content"], last["reasoning"]))
check("and is visible in the chat without \"Reasoning\"", "KAPPA-731" in v.history.toPlainText() and "The model returned an empty reply" not in db.list_messages(sys_chat)[-1]["content"])
lm.script(say("the next answer")); v.input.setPlainText("what did you answer?"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
check("the model sees its previous answer in the history", any(m["role"] == "assistant" and m.get("content") == "Delivery code: KAPPA-731" for m in lm.requests[-1]["messages"]), lm.requests[-1]["messages"][-4:])
lm.script([{"choices": [{"delta": {"reasoning_content": "Started thinking and hit the limit"}, "finish_reason": "length"}]}]); v.input.setPlainText("cut-off reasoning"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
last = db.list_messages(sys_chat)[-1]
check("reasoning cut off by the limit is not an answer", last["role"] == "assistant" and last["content"] == "" and "hit the limit" in last["reasoning"], (last["role"], last["content"], last["reasoning"]))

# ---------- permission broker: cancel while waiting ----------
br = PermissionBroker(w); br._on_asked = lambda *a: None
br.asked.disconnect(); ev = threading.Event(); res = []
th = threading.Thread(target=lambda: res.append(br.ask("t", {}, ev))); th.start(); time.sleep(0.5); ev.set(); th.join(3)
check("broker: cancelling generation = refusal without hanging", res == [False] and not th.is_alive())
br2 = PermissionBroker(w, timeout=0.6); br2.asked.disconnect(); t0 = time.time()
check("broker: timeout = refusal", br2.ask("t", {}) is False and time.time() - t0 < 3)

llm.httpx.stream = real_stream
w.close()
print("ALL OK" if ok_all else "SOME FAILED")



