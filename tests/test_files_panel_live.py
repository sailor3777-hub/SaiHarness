import os, sys, time, pathlib, tempfile, shutil
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from core import db
db.DB_PATH = pathlib.Path(sys.argv[1])
from ui.files_panel import FilesPanel, PATH_ROLE, KIND_ROLE, COLOR_WARN, COLOR_OVER, COLOR_NORMAL
from ui.main_window import MainWindow
from core.files import Selection

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

app = QApplication([])
def wait(cond, timeout=60.0):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents(); time.sleep(0.005)
    app.processEvents()
    return cond()

tmp = tempfile.mkdtemp(); root = os.path.join(tmp, "proj")
def w(rel, data, mode="w"):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, mode, **({} if "b" in mode else {"encoding": "utf-8"})).write(data)
w("notes.txt", "The project's secret word: PURPLE-MAMMOTH-42\n"); w("sub/code.py", "x = 1\n")
w("sub/deep/more.md", "# doc\n"); w("img.png", b"\x89PNG\x00", "wb"); w(".git/config", "x"); w("zeta.txt", "z")
P = lambda *p: os.path.join(root, *p)

def children(item): return [item.child(i) for i in range(item.childCount())]
def find(item, name): return next((c for c in children(item) if c.data(0, PATH_ROLE) and os.path.basename(c.data(0, PATH_ROLE)) == name), None)

# ---------- the panel ----------
panel = FilesPanel()
check("panel: no roots, a hint", panel.tree.topLevelItemCount() == 0 and "No files selected" in panel.info.text())
check("adding a root", panel.add_root(root) and panel.tree.topLevelItemCount() == 1 and db.list_roots() == [root])
check("a nested root is refused", not panel.add_root(P("sub")) and "already inside" in panel.info.text() and db.list_roots() == [root])
check("a parent root is refused", not panel.add_root(tmp) and "is already added inside" in panel.info.text())
r = panel.tree.topLevelItem(0)
check("root: lazy loading (a placeholder)", r.childCount() == 1 and r.child(0).data(0, KIND_ROLE) == "placeholder")
r.setExpanded(True)
names = [c.text(0) for c in children(r)]
check("content: folders first, .git hidden, the picture is marked", names == ["sub", "img.png  (image: the path goes into the request)", "notes.txt", "zeta.txt"], names)
img = next(c for c in children(r) if c.text(0).startswith("img.png"))
check("a picture can be checked", bool(img.flags() & Qt.ItemIsUserCheckable))
changed = []; panel.selection_changed.connect(lambda: changed.append(1))
r.setCheckState(0, Qt.Checked)
check("checking the root: selection and signal", panel.selection.include == {root} and changed == [1])
sub = find(r, "sub"); sub.setExpanded(True)
check("descendants are shown checked", sub.checkState(0) == Qt.Checked and find(r, "notes.txt").checkState(0) == Qt.Checked
      and find(sub, "code.py").checkState(0) == Qt.Checked)
check("the picture is checked together with the folder", img.checkState(0) == Qt.Checked)
find(sub, "code.py").setCheckState(0, Qt.Unchecked)
check("unchecking a file: an exclusion, parents partial", panel.selection.exclude == {P("sub", "code.py")}
      and sub.checkState(0) == Qt.PartiallyChecked and r.checkState(0) == Qt.PartiallyChecked and len(changed) == 2)
check("the summary is computed in the background", wait(lambda: panel._summary is not None), panel.info.text())
check("summary: files and tokens", panel._summary["files"] == 4 and panel._summary["tokens"] > 0, panel._summary)  # notes, more.md, zeta, img.png
check("the summary in the text", "Files: 4" in panel.info.text() and "tok." in panel.info.text(), panel.info.text())
panel.set_extra_tokens(1000); tokens_total = 1000 + panel._summary["tokens"]
check("\"whole request\" = files + the rest", f"≈{tokens_total:,}".replace(",", " ") in panel.info.text(), panel.info.text())
panel.set_limit((tokens_total * 10, True, None)); check("a limit with room: the normal color", COLOR_NORMAL in panel.info.styleSheet() and " of " in panel.info.text())
panel.set_limit((int(tokens_total / 0.9), True, None)); check("over 80% of the limit: yellow", COLOR_WARN in panel.info.styleSheet())
panel.set_limit((tokens_total - 1, True, None)); check("over the limit: red", COLOR_OVER in panel.info.styleSheet())
panel.set_limit((None, False, 262144)); check("the model is not loaded: an honest note", "limit unknown" in panel.info.text() and "262 144" in panel.info.text(), panel.info.text())
panel.set_limit(None); check("no limit data: no note", " of " not in panel.info.text() and "limit" not in panel.info.text())

# restoring the selection from the DB: the needed folders are expanded
panel.set_selection(Selection({P("sub", "deep", "more.md")}))
deep = find(find(r, "sub"), "deep")
check("set_selection expands the path to the checked file", deep is not None and deep.isExpanded()
      and find(deep, "more.md").checkState(0) == Qt.Checked and r.checkState(0) == Qt.PartiallyChecked)
check("set_selection does not send selection_changed", len(changed) == 2)

# "Refresh" picks up new files and keeps what is expanded
w("new_file.txt", "n"); panel.rebuild()
r = panel.tree.topLevelItem(0)
check("Refresh: the new file is visible, expanded folders are kept", find(r, "new_file.txt") is not None and find(find(r, "sub"), "deep").isExpanded())

# deleting a root clears the selection and sends a signal
panel.tree.setCurrentItem(r); n = len(changed); panel._remove_root()
check("deleting the root: list, selection, signal", panel.roots == [] and panel.selection.is_empty() and len(changed) == n + 1 and db.list_roots() == [])
check("files on disk are intact", os.path.exists(P("notes.txt")))
panel.add_root(root)

# ---------- the main window: selection per chat and a real request ----------
w_ = MainWindow(); fp = w_.files_panel; v = w_.chat_view; ctx = w_.context
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1234"); c.save_btn.click()
b = ctx.builder; wait(lambda: not b.models_req.busy)
b.name.setText("gemma"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica"); b.max_tokens.setValue(1500); b.temperature.setValue(0.2); b.save_btn.click()
check("window: the root from the DB is shown", fp.roots == [root])

# check a file BEFORE the chat is created, then send: the selection moves into the chat
fr = fp.tree.topLevelItem(0); fr.setExpanded(True)
find(fr, "notes.txt").setCheckState(0, Qt.Checked)
check("draft: no chat yet", w_.chat_id is None and fp.selection.include == {P("notes.txt")})
wait(lambda: fp._summary is not None); check("the estimate before sending", fp._summary["files"] == 1)
wait(lambda: not w_.limit_req.busy)
check("the model limit reached the panel", fp._limit_info and fp._limit_info[0] and " of " in fp.info.text(), (fp._limit_info, fp.info.text()))
v.input.setPlainText("What is the project's secret word given in the file? Answer in one word."); v.send_btn.click()
check("the chat is created, the selection is saved in it", w_.chat_id is not None and db.load_chat_context(w_.chat_id) == ({P("notes.txt")}, set()))
req = w_.last_requests[w_.chat_id]
check("the file went into the request (system)", "=== proj/notes.txt ===" in req["messages"][0]["content"] and "PURPLE-MAMMOTH-42" in req["messages"][0]["content"])
check("the status during generation: files 1", "files: 1" in v.status.text(), v.status.text())
check("the answer finished", wait(lambda: w_.stream is None, 120))
ans = db.list_messages(w_.chat_id)[-1]
text = ans["content"] + " " + ans["reasoning"]
print("      model answer:", repr(ans["content"][:150]))
check("the model read the file (the answer contains the secret word)", "MAMMOTH" in ans["content"].upper() or "MAMMOTH" in ans["reasoning"].upper(), text[:200])
first_chat = w_.chat_id

# a new chat starts without files; the previous one keeps its own
w_.new_chat()
check("new chat: the selection is empty", fp.selection.is_empty() and "No files selected" in fp.info.text())
check("in a new chat sending includes no files", w_.files_panel.load_bundle().files == [])
for i in range(w_.chat_list.list.count()):
    if w_.chat_list.list.item(i).data(0x100) == first_chat: w_.chat_list.list.setCurrentRow(i)
fr = fp.tree.topLevelItem(0)
check("back to the chat: the selection is restored in the tree", fp.selection.include == {P("notes.txt")} and find(fr, "notes.txt").checkState(0) == Qt.Checked)

# the "whole request" estimate includes the history
wait(lambda: fp._summary is not None)
check("the chat history is included in the estimate", fp._extra_tokens > 0)

# too large a selection is not sent
import core.files as cf
old = cf.MAX_FILES; cf.MAX_FILES = 1
fr.setCheckState(0, Qt.Checked)
v.input.setPlainText("hello"); v.send_btn.click()
cf.MAX_FILES = old
check("too large a selection: refused, the input is intact, the message is not saved",
      "Too many" in v.status.text() and v.input.toPlainText() == "hello" and len(db.list_messages(first_chat)) == 2 and w_.stream is None, v.status.text())
v.input.clear()

# a file deleted on disk does not break sending
fr.setCheckState(0, Qt.Unchecked); find(fr, "notes.txt").setCheckState(0, Qt.Checked)
os.remove(P("notes.txt"))
v.input.setPlainText("answer in one word: hello"); v.send_btn.click()
check("a vanished file: sending goes on, the skip is noted in the status", w_.stream is not None and "skipped: 1" in v.status.text(), v.status.text())
wait(lambda: w_.stream is None, 120)

w_.close(); shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK" if ok_all else "SOME FAILED")
