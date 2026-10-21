import json, sys, time, pathlib, tempfile
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t33.db"
from ui.main_window import MainWindow

MODEL, MAXTOK = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 8000
app = QApplication([])
def wait(cond, timeout=300.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.01)
    app.processEvents(); return cond()

# the same structure as in the user's log
r1 = tmp / "SDK" / "ClaudeAPI" / "cli_project_COMPLETE"; r1.mkdir(parents=True)
(r1 / "README.md").write_text("# MCP Chat\n\nMCP Chat is a command-line interface application.\n\n## Setup\nRun `uv run main.py`.\n", encoding="utf-8")
(r1 / "main.py").write_text("print('hello')\n", encoding="utf-8")
r2 = tmp / "Wlad"; (r2 / "KeyboardEscape").mkdir(parents=True)
(r2 / "KeyboardEscape" / "requirements.txt").write_text("pygame-ce>=2.5\n\nyellow sausage\n", encoding="utf-8")
(r2 / "notes.txt").write_text("just a note\n", encoding="utf-8")
db.add_root(str(r1)); db.add_root(str(r2))
req_file = r2 / "KeyboardEscape" / "requirements.txt"
before = req_file.read_text(encoding="utf-8")

asked = []
QMessageBox.question = staticmethod(lambda p, t, text, *a, **k: (asked.append(text), QMessageBox.Yes)[1])
w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; v = w.chat_view; ctx = w.context
wait(lambda: tp.states and not tp.connect_req.busy, 40)
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1234"); c.save_btn.click()
b = ctx.builder; wait(lambda: not b.models_req.busy)
b.name.setText("p"); b.base_model.setCurrentText(MODEL); b.max_tokens.setValue(MAXTOK); b.temperature.setValue(0.7); b.save_btn.click()
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
print(f"\n##### model: {MODEL} (max_tokens {MAXTOK}); in the request ≈{tp.prompt_tokens()} tok. #####")

def ask(question):
    n0 = len(db.list_messages(w.chat_id)) if w.chat_id else 0
    t0 = time.time(); v.input.setPlainText(question); v.send_btn.click()
    done = wait(lambda: w.stream is None)
    rows = db.list_messages(w.chat_id)[n0:]
    calls = [x for r in rows if r["role"] == "assistant" and r["tool_calls"] for x in json.loads(r["tool_calls"])]
    names = [json.loads(x["function"]["arguments"] or "{}").get("tool") or x["function"]["name"] for x in calls]
    errors = [r for r in rows if r["role"] == "tool" and r["content"].startswith("ERROR")]
    final = next((r for r in reversed(rows) if r["role"] == "assistant" and not r["tool_calls"]), None)
    print(f"\n=== {question!r}\n    {time.time()-t0:.0f} s, finished: {done}; calls: {len(calls)} ({', '.join(names)}); describe_tool: {sum(1 for x in calls if x['function']['name'] == 'describe_tool')}; tool errors: {len(errors)}")
    for x in calls:
        a = json.loads(x["function"]["arguments"] or "{}")
        print("    ▸", a.get("tool") or x["function"]["name"], json.dumps(a.get("arguments", a), ensure_ascii=False)[:130])
    for r in rows:
        if r["role"] == "tool" and r["content"].startswith("ERROR"): print("    error:", r["content"][:150].replace("\n", " "))
        if r["role"] == "system": print("    system:", r["content"])
    print("    answer:", (final["content"] if final else "(none)")[:420].replace("\n", " "))

ask("what is in the folder?")
ask("show the contents of the file readme.md")
ask("find this string in my files: yellow sausage. tell me where you found it. if you cannot find the full string, find just sausage")
ask("great! replace yellow sausage with brown bear")
after = req_file.read_text(encoding="utf-8")
print("\nthe file requirements.txt changed:", after != before, "| confirmation requests:", len(asked))
w.close()
