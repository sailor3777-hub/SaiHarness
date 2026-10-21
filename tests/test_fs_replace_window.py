"""fs_replace through the window: confirmation with "was → will be", refusal, permission, policies, history."""
import json, sys, time, pathlib, tempfile
import httpx
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db, llm
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t35.db"
from ui.main_window import MainWindow
from ui.permission import format_arguments

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

# ---- window formatting ----
txt = format_arguments("toolbox.fs_replace", {"path": "C:/p/requirements.txt", "old": "yellow sausage", "new": "brown bear"})
check("fs_replace: the labels File / Was / Will be", txt == "File: C:/p/requirements.txt\n\nWas: yellow sausage\n\nWill be: brown bear", repr(txt))
txt = format_arguments("toolbox.fs_replace", {"path": "f", "old": "a\nb", "new": "x" * 800})
check("a multi-line and long value as a block, the long one cut", "Was:\n    a\n    b" in txt and "… (100 more chars)" in txt and txt.count("x") == 700, txt[:200])
check("an unknown tool: argument names as is, non-strings as JSON", format_arguments("s.t", {"n": 5, "d": {"a": 1}}) == 'n: 5\n\nd: {"a": 1}')
check("no arguments", format_arguments("s.t", {}) == "(no arguments)")

# ---- through the window ----
proj = tmp / "Wlad"; (proj / "KeyboardEscape").mkdir(parents=True)
req = proj / "KeyboardEscape" / "requirements.txt"; ORIGINAL = b"pygame-ce>=2.5\r\n\r\nyellow sausage\r\n"; req.write_bytes(ORIGINAL)
db.add_root(str(proj))
def sse(*c): return ("".join(f"data: {json.dumps(x, ensure_ascii=False)}\n\n" for x in c) + "data: [DONE]\n\n").encode()
def tc(i, n, a): return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": i, "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}]}}]}
class LM:
    steps = []; requests = []
    def handler(self, r): self.requests.append(json.loads(r.content)); return httpx.Response(200, content=sse(*self.steps.pop(0)))
lm = LM(); llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
asked = []
def answer(result):
    return staticmethod(lambda p, t, text, *a, **k: (asked.append(text), result)[1])
w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; v = w.chat_view; ctx = w.context
wait(lambda: tp.states and not tp.connect_req.busy, 40)
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1"); c.save_btn.click(); wait(lambda: not ctx.builder.models_req.busy)
b = ctx.builder; b.name.setText("p"); b.base_model.setCurrentText("fake"); b.save_btn.click()
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
check("fs_replace is in the catalog and \"asks\" by default", any(t.name == "fs_replace" and tp.policy_for(t) == "ask" for t in tp.tools) and any("fs_replace" in tp.tree.topLevelItem(0).child(i).text(0) and "[asks]" in tp.tree.topLevelItem(0).child(i).text(0) for i in range(tp.tree.topLevelItem(0).childCount())))
call = lambda: [tc("r1", "call_tool", {"tool": "toolbox.fs_replace", "arguments": {"path": "KeyboardEscape/requirements.txt", "old": "yellow sausage", "new": "brown bear"}})]
def baks(): return sorted(x.name for x in req.parent.iterdir() if ".bak" in x.name)

QMessageBox.question = answer(QMessageBox.No)
lm.steps = [call(), [{"choices": [{"delta": {"content": "understood, not changing it"}}]}]]
v.input.setPlainText("replace yellow sausage with brown bear"); v.send_btn.click(); wait(lambda: w.stream is None)
check("the window showed the file, was and will be", len(asked) == 1 and "toolbox.fs_replace" in asked[0] and "File: KeyboardEscape/requirements.txt" in asked[0]
      and "Was: yellow sausage" in asked[0] and "Will be: brown bear" in asked[0], asked)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("refusal: the model got \"The user declined\", the file and backups are untouched", "declined" in last["content"] and req.read_bytes() == ORIGINAL and baks() == [], last["content"])

QMessageBox.question = answer(QMessageBox.Yes); asked.clear()
lm.steps = [call(), [{"choices": [{"delta": {"content": "done"}}]}]]
v.input.setPlainText("yes, replace it"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("permission: the file is changed, CRLF is kept", req.read_bytes() == b"pygame-ce>=2.5\r\n\r\nbrown bear\r\n", req.read_bytes())
check("a backup with the original content is created", baks() == ["requirements.txt.bak"] and (req.parent / "requirements.txt.bak").read_bytes() == ORIGINAL, baks())
check("the result for the model: what was done and where the backup is", last["content"].startswith("Done: replaced 1 occurrence") and "(line 3)" in last["content"] and "requirements.txt.bak" in last["content"], last["content"])
check("the call and the result are visible in the chat", "toolbox.fs_replace" in v.history.toPlainText() and "Done: replaced" in v.history.toPlainText())

# a repeat on the already changed file: old is gone, an error the model can report to the user
asked.clear(); lm.steps = [call(), [{"choices": [{"delta": {"content": "already replaced"}}]}]]
v.input.setPlainText("once more"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("repeat: old not found, the file did not change, no new backups", last["content"].startswith("ERROR") and "not found" in last["content"] and baks() == ["requirements.txt.bak"], last["content"])
check("with an error the confirmation window still asked (the check runs after permission)", len(asked) == 1)

# the "auto" and "block" policies
tp.set_policy("toolbox.fs_replace", "deny"); asked.clear(); before = req.read_bytes()
lm.steps = [[tc("r2", "call_tool", {"tool": "toolbox.fs_replace", "arguments": {"path": "KeyboardEscape/requirements.txt", "old": "brown bear", "new": "x"}})], [{"choices": [{"delta": {"content": "ok"}}]}]]
v.input.setPlainText("blocked?"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("deny: no question and no write", asked == [] and "blocked" in last["content"] and req.read_bytes() == before)

# arguments are checked against the schema before confirmation
tp.set_policy("toolbox.fs_replace", None); asked.clear()
lm.steps = [[tc("r3", "call_tool", {"tool": "toolbox.fs_replace", "arguments": {"path": "x.txt", "old": "a"}})], [{"choices": [{"delta": {"content": "ok"}}]}]]
v.input.setPlainText("without new"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("argument new is missing: a schema error, no window", asked == [] and "'new' is a required property" in last["content"], last["content"])

w.close()
print("ALL OK" if ok_all else "SOME FAILED")
