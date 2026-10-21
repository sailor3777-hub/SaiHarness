import json, sys, time, pathlib, tempfile
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t24.db"
from ui.main_window import MainWindow

app = QApplication([])
def wait(cond, timeout=240.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.01)
    app.processEvents(); return cond()

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemma-4-e4b-it-erotica"
proj = tmp / "proj"; proj.mkdir()
(proj / "secret.txt").write_text("Access code: PURPLE-MAMMOTH-42\n", encoding="utf-8")
(proj / "readme.md").write_text("# Project\nA simple test project.\n", encoding="utf-8")
db.add_root(str(proj))
asked = []
QMessageBox.question = staticmethod(lambda p, t, text, *a, **k: (asked.append(text), QMessageBox.Yes)[1])

w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; v = w.chat_view; ctx = w.context
wait(lambda: tp.states and not tp.connect_req.busy, 40)
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1234"); c.save_btn.click()
b = ctx.builder; wait(lambda: not b.models_req.busy)
b.name.setText("p"); b.base_model.setCurrentText(MODEL); b.max_tokens.setValue(4000); b.temperature.setValue(0.2); b.save_btn.click()
tp.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
print(f"model: {MODEL}; tools enabled: {len(tp.enabled)}; in the request ≈{tp.prompt_tokens()} tok., full schemas ≈{__import__('core.tools', fromlist=['x']).schema_tokens(tp.enabled_tools())} tok.")

def ask(question):
    n0 = len(db.list_messages(w.chat_id)) if w.chat_id else 0
    t0 = time.time(); v.input.setPlainText(question); v.send_btn.click()
    done = wait(lambda: w.stream is None)
    rows = db.list_messages(w.chat_id)[n0:]
    print(f"\n=== {question!r}  ({time.time()-t0:.1f} s, finished: {done})")
    for r in rows:
        if r["role"] == "assistant" and r["tool_calls"]:
            for c_ in json.loads(r["tool_calls"]):
                print("   ▸", c_["function"]["name"], c_["function"]["arguments"][:160])
        elif r["role"] == "tool":
            print("   ◂", r["tool_name"], "->", r["content"][:140].replace("\n", " | "))
        elif r["role"] == "assistant":
            print("   answer:", r["content"][:300].replace("\n", " "))
    print("   status:", v.status.text())
    return rows

ask("What is 17*23? Use the tools for an exact calculation.")
ask("What time is it on the computer now?")
ask("What does the file secret.txt in my folder say?")
ask("Find the word \"Project\" in all my files and tell me which file it is in.")
ask("Write a note named demo with the text: Hello from Beta.")
print("\nconfirmation requests:", len(asked))
w.close()
