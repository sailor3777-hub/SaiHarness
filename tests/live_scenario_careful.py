"""A careful run on a heavy model (70k context): small requests, usage measurements against the estimate."""
import json, sys, time, pathlib, tempfile
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t44.db"
from core.context_builder import estimate_request_tokens
from ui.main_window import MainWindow
ROOT = str(pathlib.Path(__file__).resolve().parents[1])  # this checkout, not a hardcoded copy

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
(r2 / "notes.txt").write_text("first line\nsecond line\nthird line\n", encoding="utf-8")
src = pathlib.Path(ROOT) / "core"
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
print(f"\n##### {MODEL}; max_tokens {MAXTOK}; context limit reported by the server: {w._limit_info}; catalog ≈{tp.prompt_tokens()} tok.")

usage_log = []
orig_usage = w._on_usage
def logging_usage(sid, prompt, completion, raw):
    usage_log.append((prompt, completion, raw)); orig_usage(sid, prompt, completion, raw)
w._on_usage = logging_usage
# thread.usage is already connected to the original bound method when the thread is created: it is replaced through the wrapper below
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
    errors = [r for r in rows if r["role"] == "tool" and r["content"].startswith("ERROR")]
    final = next((r for r in reversed(rows) if r["role"] == "assistant" and not r["tool_calls"]), None)
    sysrows = [r["content"] for r in rows if r["role"] == "system"]
    print(f"\n=== {question[:110]!r}\n    {time.time()-t0:.0f} s, finished: {done}; calls: {len(calls)} ({', '.join(names)}); tool errors: {len(errors)}")
    if usage_log:
        print("    request tokens (server / our raw estimate): " + "; ".join(f"{p}/{e}" for p, _, e in usage_log) + f"; correction ×{w._scale.get(w._limit_key, 1.0):.2f}")
    for r in errors: print("    error:", r["content"][:140].replace("\n", " "))
    for s in sysrows: print("    system:", s)
    print("    answer:", (final["content"] if final else "(none)")[:380].replace("\n", " "))

ask("what is in the folder?")
ask("show the contents of the file readme.md")
ask("find this string in my files: yellow sausage. tell me where you found it.")
ask("great! replace yellow sausage with brown bear")
print("    requirements.txt:", req.read_bytes(), "| confirmations:", len(asked), "| .bak:", sorted(x.name for x in req.parent.iterdir() if '.bak' in x.name))
ask("Read the file notes.txt and the file requirements.txt, count the total number of lines in them with the calculator (count empty lines too) and give the number.")

# a real context from files: four project source files, about 6 thousand tokens
w.new_chat(); ctx.findChild(__import__("PySide6.QtWidgets", fromlist=["QTabWidget"]).QTabWidget).setCurrentWidget(fp)
for i in range(fp.tree.topLevelItemCount()):
    top = fp.tree.topLevelItem(i)
    if top.data(0, Qt.UserRole).endswith("core"):
        top.setExpanded(True)
        for j in range(top.childCount()):
            if top.child(j).text(0) in ("connection.py", "context_builder.py", "richtext.py", "diag.py"):
                top.child(j).setCheckState(0, Qt.Checked)
wait(lambda: fp._summary is not None, 30)
print(f"\n--- the files panel before the request:\n{fp.info.text()}")
tp.enabled.clear(); tp._sync(); w._on_tools_changed()
ask("List the names of all the Python files whose content you see in the context. Names only, no explanations.")
print(f"\n--- the files panel after the request:\n{fp.info.text()}")
print(f"\nthe final estimate correction for the model: ×{w._scale.get(w._limit_key, 1.0):.3f}; last usage per chat: {w._last_usage}")
w.close()
