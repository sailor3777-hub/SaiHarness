"""Осторожный прогон на тяжёлой модели (контекст 70к): небольшие запросы, замеры usage против оценки."""
import json, sys, time, pathlib, tempfile
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t44.db"
from core.context_builder import estimate_request_tokens
from ui.main_window import MainWindow

MODEL = sys.argv[1]; MAXTOK = int(sys.argv[2])
app = QApplication([])
def wait(cond, timeout=900.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.01)
    app.processEvents(); return cond()

r1 = tmp / "SDK" / "ClaudeAPI" / "cli_project_COMPLETE"; r1.mkdir(parents=True)
(r1 / "README.md").write_text("# MCP Chat\n\nMCP Chat is a command-line interface application.\n\n## Setup\nRun `uv run main.py`.\n", encoding="utf-8")
(r1 / "main.py").write_text("print('hello')\n", encoding="utf-8")
r2 = tmp / "Wlad"; (r2 / "KeyboardEscape").mkdir(parents=True)
req = r2 / "KeyboardEscape" / "requirements.txt"; req.write_bytes(b"pygame-ce>=2.5\r\n\r\nyellow sausage\r\n")
(r2 / "notes.txt").write_text("первая строка\nвторая строка\nтретья строка\n", encoding="utf-8")
src = pathlib.Path("C:/SaiHarness/core")
db.add_root(str(r1)); db.add_root(str(r2)); db.add_root(str(src))
asked = []
QMessageBox.question = staticmethod(lambda p, t, text, *a, **k: (asked.append(text), QMessageBox.Yes)[1])

w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; v = w.chat_view; ctx = w.context; fp = w.files_panel
wait(lambda: tp.states and not tp.connect_req.busy, 40)
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1234"); c.save_btn.click()
b = ctx.builder; wait(lambda: not b.models_req.busy)
b.name.setText("p"); b.base_model.setCurrentText(MODEL); b.max_tokens.setValue(MAXTOK); b.temperature.setValue(0.7); b.max_steps.setValue(16); b.save_btn.click()
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
wait(lambda: not w.limit_req.busy, 20)
print(f"\n##### {MODEL}; max_tokens {MAXTOK}; лимит контекста по данным сервера: {w._limit_info}; каталог ≈{tp.prompt_tokens()} ток.")

usage_log = []
orig_usage = w._on_usage
def logging_usage(sid, prompt, completion, raw):
    usage_log.append((prompt, completion, raw)); orig_usage(sid, prompt, completion, raw)
w._on_usage = logging_usage
# thread.usage уже подключён к исходному bound-методу при создании потока: подменяем через обёртку ниже
orig_submit = w.on_submit
import worker
orig_init = worker.ChatStreamer.__init__
def patched_init(self, *a, **k):
    orig_init(self, *a, **k); self.usage.connect(lambda sid, p, c_, e: usage_log.append((p, c_, e)))
worker.ChatStreamer.__init__ = patched_init

def ask(question):
    usage_log.clear(); n0 = len(db.list_messages(w.chat_id)) if w.chat_id else 0
    t0 = time.time(); v.input.setPlainText(question); v.send_btn.click()
    done = wait(lambda: w.stream is None)
    rows = db.list_messages(w.chat_id)[n0:]
    calls = [x for r in rows if r["role"] == "assistant" and r["tool_calls"] for x in json.loads(r["tool_calls"])]
    names = [(json.loads(x["function"]["arguments"] or "{}").get("tool") or x["function"]["name"]) for x in calls]
    errors = [r for r in rows if r["role"] == "tool" and r["content"].startswith("ОШИБКА")]
    final = next((r for r in reversed(rows) if r["role"] == "assistant" and not r["tool_calls"]), None)
    sysrows = [r["content"] for r in rows if r["role"] == "system"]
    print(f"\n=== {question[:110]!r}\n    {time.time()-t0:.0f} с, завершилось: {done}; вызовов: {len(calls)} ({', '.join(names)}); ошибок инструментов: {len(errors)}")
    if usage_log:
        print("    токены запросов (сервер / наша сырая оценка): " + "; ".join(f"{p}/{e}" for p, _, e in usage_log) + f"; поправка ×{w._scale.get(w._limit_key, 1.0):.2f}")
    for r in errors: print("    ошибка:", r["content"][:140].replace("\n", " "))
    for s in sysrows: print("    система:", s)
    print("    ответ:", (final["content"] if final else "(нет)")[:380].replace("\n", " "))

ask("что находится в папке?")
ask("выведи содержимое файла readme.md")
ask("найди в моих файлах такой стринг: yellow sausage. скажи где ты нашёл это.")
ask("отлично! замени yellow sausage на brown bear")
print("    requirements.txt:", req.read_bytes(), "| подтверждений:", len(asked), "| .bak:", sorted(x.name for x in req.parent.iterdir() if '.bak' in x.name))
ask("Прочитай файл notes.txt и файл requirements.txt, посчитай через калькулятор общее число строк в них (считай пустые строки тоже) и назови число.")

# настоящий контекст из файлов: четыре исходника проекта, около 6 тыс. токенов
w.new_chat(); ctx.findChild(__import__("PySide6.QtWidgets", fromlist=["QTabWidget"]).QTabWidget).setCurrentWidget(fp)
for i in range(fp.tree.topLevelItemCount()):
    top = fp.tree.topLevelItem(i)
    if top.data(0, Qt.UserRole).endswith("core"):
        top.setExpanded(True)
        for j in range(top.childCount()):
            if top.child(j).text(0) in ("connection.py", "context_builder.py", "richtext.py", "diag.py"):
                top.child(j).setCheckState(0, Qt.Checked)
wait(lambda: fp._summary is not None, 30)
print(f"\n--- панель файлов до запроса:\n{fp.info.text()}")
tp.enabled.clear(); tp._sync(); w._on_tools_changed()
ask("Назови одним списком имена всех Python-файлов, содержимое которых ты видишь в контексте. Только имена, без пояснений.")
print(f"\n--- панель файлов после запроса:\n{fp.info.text()}")
print(f"\nитоговая поправка оценки для модели: ×{w._scale.get(w._limit_key, 1.0):.3f}; по чатам последний usage: {w._last_usage}")
w.close()
