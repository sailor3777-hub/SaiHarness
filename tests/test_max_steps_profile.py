"""Лимит шагов инструментов в профиле модели: БД, миграция, Model Builder, сводка, передача в цикл."""
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
# ---- миграция базы старого формата ----
old = tmp / "old.db"
con = sqlite3.connect(old)
con.executescript("""CREATE TABLE model_profiles (name TEXT PRIMARY KEY, connection TEXT NOT NULL DEFAULT '', base_model TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0.7, max_tokens INTEGER NOT NULL DEFAULT 2048,
  memory TEXT NOT NULL DEFAULT ''); INSERT INTO model_profiles(name, base_model) VALUES('Старый','m');""")
con.commit(); con.close()
db.DB_PATH = old
p = db.list_profiles()[0]
check("миграция: старый профиль получил лимит по умолчанию", p["name"] == "Старый" and p["max_steps"] == 16, dict(p))

db.DB_PATH = tmp / "new.db"
db.save_profile("a", "c", "m", "", 0.5, 100, "")
check("save_profile без max_steps: по умолчанию 16", db.get_profile("a")["max_steps"] == 16)
db.save_profile("a", "c", "m", "", 0.5, 100, "", max_steps=40)
check("save_profile с max_steps обновляет профиль", db.get_profile("a")["max_steps"] == 40 and len(db.list_profiles()) == 1)

# ---- Model Builder / сводка ----
app = QApplication([])
def wait(cond, timeout=40.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()
from ui.main_window import MainWindow
w = MainWindow(mcp_config=tmp / "mcp.json"); ctx = w.context; b = ctx.builder; sel = ctx.model_selector; tp = w.tools_panel; v = w.chat_view
wait(lambda: tp.states and not tp.connect_req.busy, 40)
check("поле «Шагов инструментов»: по умолчанию 16, диапазон 1–100", b.max_steps.value() == 16 and b.max_steps.minimum() == 1 and b.max_steps.maximum() == 100)
check("есть подсказка про цену шагов", "контекст" in b.max_steps.toolTip())
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1"); c.save_btn.click(); wait(lambda: not b.models_req.busy)
b.name.setText("steps2"); b.base_model.setCurrentText("fake"); b.max_steps.setValue(2); b.save_btn.click()
check("сохранение из Builder", db.get_profile("steps2")["max_steps"] == 2)
check("сводка профиля показывает шаги", "Шагов инструментов: 2" in sel.summary.text(), sel.summary.text())
b.max_steps.setValue(99); b.name.setText("x"); sel.profile.setCurrentText("steps2"); sel.edit_btn.click()
check("«Изменить» загружает значение в Builder", b.max_steps.value() == 2 and b.name.text() == "steps2")
b.max_steps.setValue(100); b.save_btn.click(); check("граница 100 сохраняется", db.get_profile("steps2")["max_steps"] == 100)
b.max_steps.setValue(2); b.save_btn.click()

# ---- лимит из профиля доходит до цикла ----
def sse(*c): return ("".join(f"data: {json.dumps(x, ensure_ascii=False)}\n\n" for x in c) + "data: [DONE]\n\n").encode()
def tc(i, n, a): return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": i, "function": {"name": n, "arguments": json.dumps(a)}}]}}]}
class LM:
    steps = []; requests = []
    def handler(self, r): self.requests.append(json.loads(r.content)); return httpx.Response(200, content=sse(*self.steps.pop(0)))
lm = LM(); llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
sel.profile.setCurrentText("steps2"); sel.user_changed.emit("steps2")
lm.steps = [[tc(f"t{i}", "find_tool", {"query": "x"})] for i in range(2)] + [[{"choices": [{"delta": {"content": "итог после лимита"}}]}]]
lm.requests = []
v.input.setPlainText("многошаговая задача"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
rows = db.list_messages(w.chat_id)
rounds = sum(1 for r in rows if r["role"] == "assistant" and r["tool_calls"])
check("лимит профиля (2): ровно 2 раунда инструментов", rounds == 2, rounds)
check("затем итог текстом без инструментов", rows[-1]["role"] == "assistant" and rows[-1]["content"] == "итог после лимита" and "tools" not in lm.requests[-1] and len(lm.requests) == 3,
      ([(r["role"], (r["content"] or "")[:30]) for r in rows[-3:]], len(lm.requests), list(lm.requests[-1])))
check("предупреждение о лимите с числом из профиля", "Достигнут предел шагов с инструментами (2)" in v.status.text(), v.status.text())
check("реплика про лимит не сохранена в истории чата", not any("Лимит вызовов инструментов" in (r["content"] or "") for r in rows))

# профиль со 100 шагами: цикл не обрывается на 16
w.new_chat(); sel.profile.setCurrentText("steps2"); sel.user_changed.emit("steps2")
db.save_profile("steps2", "lm", "fake", "", 0.7, 100, "", max_steps=20)
lm.steps = [[tc(f"u{i}", "find_tool", {"query": "x"})] for i in range(18)] + [[{"choices": [{"delta": {"content": "дошёл до конца"}}]}]]
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked); lm.requests = []
v.input.setPlainText("длинная цепочка"); v.send_btn.click(); wait(lambda: w.stream is None, 90)
rows = db.list_messages(w.chat_id); rounds = sum(1 for r in rows if r["role"] == "assistant" and r["tool_calls"])
check("лимит 20 > 16: 18 раундов выполнены, ответ получен", rounds == 18 and rows[-1]["content"] == "дошёл до конца", rounds)
w.close(); print("ALL OK" if ok_all else "SOME FAILED")
