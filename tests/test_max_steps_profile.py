"""The tool step limit in the model profile: DB, migration, Model Builder, summary, passing into the loop."""
import json, pathlib, sqlite3, sys, tempfile, time
import httpx
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db, llm

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
# ---- migrating an old-format database ----
old = tmp / "old.db"
con = sqlite3.connect(old)
con.executescript("""CREATE TABLE model_profiles (name TEXT PRIMARY KEY, connection TEXT NOT NULL DEFAULT '', base_model TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0.7, max_tokens INTEGER NOT NULL DEFAULT 2048,
  memory TEXT NOT NULL DEFAULT ''); INSERT INTO model_profiles(name, base_model) VALUES('Old','m');""")
con.commit(); con.close()
db.DB_PATH = old
p = db.list_profiles()[0]
check("migration: the old profile got the default limit", p["name"] == "Old" and p["max_steps"] == 16, dict(p))

db.DB_PATH = tmp / "new.db"
db.save_profile("a", "c", "m", "", 0.5, 100, "")
check("save_profile without max_steps: 16 by default", db.get_profile("a")["max_steps"] == 16)
db.save_profile("a", "c", "m", "", 0.5, 100, "", max_steps=40)
check("save_profile with max_steps updates the profile", db.get_profile("a")["max_steps"] == 40 and len(db.list_profiles()) == 1)

# ---- Model Builder / summary ----
app = QApplication([])
def wait(cond, timeout=40.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()
from ui.main_window import MainWindow
w = MainWindow(mcp_config=tmp / "mcp.json"); ctx = w.context; b = ctx.builder; sel = ctx.model_selector; tp = w.tools_panel; v = w.chat_view
wait(lambda: tp.states and not tp.connect_req.busy, 40)
check("the \"Tool steps\" field: 16 by default, range 1–100", b.max_steps.value() == 16 and b.max_steps.minimum() == 1 and b.max_steps.maximum() == 100)
check("there is a hint about the cost of steps", "context" in b.max_steps.toolTip())
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1"); c.save_btn.click(); wait(lambda: not b.models_req.busy)
b.name.setText("steps2"); b.base_model.setCurrentText("fake"); b.max_steps.setValue(2); b.save_btn.click()
check("saving from Builder", db.get_profile("steps2")["max_steps"] == 2)
check("the profile summary shows the steps", "Tool steps: 2" in sel.summary.text(), sel.summary.text())
b.max_steps.setValue(99); b.name.setText("x"); sel.profile.setCurrentText("steps2"); sel.edit_btn.click()
check("\"Edit\" loads the value into Builder", b.max_steps.value() == 2 and b.name.text() == "steps2")
b.max_steps.setValue(100); b.save_btn.click(); check("the 100 boundary is saved", db.get_profile("steps2")["max_steps"] == 100)
b.max_steps.setValue(2); b.save_btn.click()

# ---- the limit from the profile reaches the loop ----
def sse(*c): return ("".join(f"data: {json.dumps(x, ensure_ascii=False)}\n\n" for x in c) + "data: [DONE]\n\n").encode()
def tc(i, n, a): return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": i, "function": {"name": n, "arguments": json.dumps(a)}}]}}]}
class LM:
    steps = []; requests = []
    def handler(self, r): self.requests.append(json.loads(r.content)); return httpx.Response(200, content=sse(*self.steps.pop(0)))
lm = LM(); llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
sel.profile.setCurrentText("steps2"); sel.user_changed.emit("steps2")
lm.steps = [[tc(f"t{i}", "find_tool", {"query": "x"})] for i in range(2)] + [[{"choices": [{"delta": {"content": "summary after the limit"}}]}]]
lm.requests = []
v.input.setPlainText("a multi-step task"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
rows = db.list_messages(w.chat_id)
rounds = sum(1 for r in rows if r["role"] == "assistant" and r["tool_calls"])
check("the profile limit (2): exactly 2 tool rounds", rounds == 2, rounds)
check("then a text summary without tools", rows[-1]["role"] == "assistant" and rows[-1]["content"] == "summary after the limit" and "tools" not in lm.requests[-1] and len(lm.requests) == 3,
      ([(r["role"], (r["content"] or "")[:30]) for r in rows[-3:]], len(lm.requests), list(lm.requests[-1])))
check("a limit warning with the number from the profile", "Reached the tool step limit (2)" in v.status.text(), v.status.text())
check("the limit message is not saved in the chat history", not any("used up the tool call limit" in (r["content"] or "") for r in rows))

# a profile with 100 steps: the loop does not stop at 16
w.new_chat(); sel.profile.setCurrentText("steps2"); sel.user_changed.emit("steps2")
db.save_profile("steps2", "lm", "fake", "", 0.7, 100, "", max_steps=20)
lm.steps = [[tc(f"u{i}", "find_tool", {"query": "x"})] for i in range(18)] + [[{"choices": [{"delta": {"content": "made it to the end"}}]}]]
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked); lm.requests = []
v.input.setPlainText("a long chain"); v.send_btn.click(); wait(lambda: w.stream is None, 90)
rows = db.list_messages(w.chat_id); rounds = sum(1 for r in rows if r["role"] == "assistant" and r["tool_calls"])
check("limit 20 > 16: 18 rounds done, the answer arrived", rounds == 18 and rows[-1]["content"] == "made it to the end", rounds)
w.close(); print("ALL OK" if ok_all else "SOME FAILED")
