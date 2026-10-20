"""fs_replace через окно: подтверждение с «было → станет», отказ, разрешение, права, история."""
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

# ---- форматирование окна ----
txt = format_arguments("toolbox.fs_replace", {"path": "C:/p/requirements.txt", "old": "yellow sausage", "new": "brown bear"})
check("fs_replace: подписи Файл / Было / Станет", txt == "Файл: C:/p/requirements.txt\n\nБыло: yellow sausage\n\nСтанет: brown bear", repr(txt))
txt = format_arguments("toolbox.fs_replace", {"path": "f", "old": "a\nb", "new": "x" * 800})
check("многострочное и длинное значение блоком, длинное обрезано", "Было:\n    a\n    b" in txt and "… (ещё 100 симв.)" in txt and txt.count("x") == 700, txt[:200])
check("неизвестный инструмент: имена аргументов как есть, не-строки через JSON", format_arguments("s.t", {"n": 5, "d": {"a": 1}}) == 'n: 5\n\nd: {"a": 1}')
check("без аргументов", format_arguments("s.t", {}) == "(без аргументов)")

# ---- через окно ----
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
check("fs_replace в каталоге и по умолчанию «спросит»", any(t.name == "fs_replace" and tp.policy_for(t) == "ask" for t in tp.tools) and any("fs_replace" in tp.tree.topLevelItem(0).child(i).text(0) and "[спросит]" in tp.tree.topLevelItem(0).child(i).text(0) for i in range(tp.tree.topLevelItem(0).childCount())))
call = lambda: [tc("r1", "call_tool", {"tool": "toolbox.fs_replace", "arguments": {"path": "KeyboardEscape/requirements.txt", "old": "yellow sausage", "new": "brown bear"}})]
def baks(): return sorted(x.name for x in req.parent.iterdir() if ".bak" in x.name)

QMessageBox.question = answer(QMessageBox.No)
lm.steps = [call(), [{"choices": [{"delta": {"content": "понял, не меняю"}}]}]]
v.input.setPlainText("замени yellow sausage на brown bear"); v.send_btn.click(); wait(lambda: w.stream is None)
check("окно показало файл, было и станет", len(asked) == 1 and "toolbox.fs_replace" in asked[0] and "Файл: KeyboardEscape/requirements.txt" in asked[0]
      and "Было: yellow sausage" in asked[0] and "Станет: brown bear" in asked[0], asked)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("отказ: модель получила «Пользователь отклонил», файл и копии нетронуты", "отклонил" in last["content"] and req.read_bytes() == ORIGINAL and baks() == [], last["content"])

QMessageBox.question = answer(QMessageBox.Yes); asked.clear()
lm.steps = [call(), [{"choices": [{"delta": {"content": "готово"}}]}]]
v.input.setPlainText("да, замени"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("разрешение: файл изменён, CRLF сохранён", req.read_bytes() == b"pygame-ce>=2.5\r\n\r\nbrown bear\r\n", req.read_bytes())
check("копия создана с исходным содержимым", baks() == ["requirements.txt.bak"] and (req.parent / "requirements.txt.bak").read_bytes() == ORIGINAL, baks())
check("результат для модели: что сделано и где копия", last["content"].startswith("Готово: заменено 1 вхождение") and "(строка 3)" in last["content"] and "requirements.txt.bak" in last["content"], last["content"])
check("в чате виден вызов и результат", "toolbox.fs_replace" in v.history.toPlainText() and "Готово: заменено" in v.history.toPlainText())

# повтор на уже изменённом файле: old больше нет — ошибка, модель может сообщить пользователю
asked.clear(); lm.steps = [call(), [{"choices": [{"delta": {"content": "уже заменено"}}]}]]
v.input.setPlainText("ещё раз"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("повтор: old не найден, файл не менялся, копий не прибавилось", last["content"].startswith("ОШИБКА") and "не найден" in last["content"] and baks() == ["requirements.txt.bak"], last["content"])
check("при ошибке окно подтверждения всё равно спросило (проверка идёт после разрешения)", len(asked) == 1)

# политика «авто» и «запретить»
tp.set_policy("toolbox.fs_replace", "deny"); asked.clear(); before = req.read_bytes()
lm.steps = [[tc("r2", "call_tool", {"tool": "toolbox.fs_replace", "arguments": {"path": "KeyboardEscape/requirements.txt", "old": "brown bear", "new": "x"}})], [{"choices": [{"delta": {"content": "ок"}}]}]]
v.input.setPlainText("запрещено?"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("deny: без вопроса и без записи", asked == [] and "запрещён" in last["content"] and req.read_bytes() == before)

# аргументы проверяются по схеме до подтверждения
tp.set_policy("toolbox.fs_replace", None); asked.clear()
lm.steps = [[tc("r3", "call_tool", {"tool": "toolbox.fs_replace", "arguments": {"path": "x.txt", "old": "a"}})], [{"choices": [{"delta": {"content": "ок"}}]}]]
v.input.setPlainText("без new"); v.send_btn.click(); wait(lambda: w.stream is None)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("не хватает аргумента new: ошибка схемы, окно не показывается", asked == [] and "'new' is a required property" in last["content"], last["content"])

w.close()
print("ALL OK" if ok_all else "SOME FAILED")
