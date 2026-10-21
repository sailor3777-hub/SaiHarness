import sys, pathlib, time
from PySide6.QtWidgets import QApplication
from core import db
db.DB_PATH = pathlib.Path(sys.argv[1])  # a temporary database
from ui.main_window import MainWindow

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

app = QApplication([])
def wait(cond, timeout=90.0):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents(); time.sleep(0.01)
    app.processEvents()
    return cond()

w = MainWindow(); ctx = w.context; v = w.chat_view; sel = w.selector
def send(text):
    v.input.setPlainText(text); v.send_btn.click()
def msgs(chat_id=None):
    return [r for r in db.list_messages(chat_id or w.chat_id) if r["role"] != "system"]

# 1. no profile
send("hello")
check("no profile: a hint, the input is not lost, no chat created",
      "Choose a model" in v.status.text() and v.input.toPlainText() == "hello" and db.list_chats() == [])
v.input.clear()

# 2. connection and profile
c = ctx.connections; c.name.setText("lmstudio"); c.url.setText("localhost:1234"); c.save_btn.click()
b = ctx.builder
wait(lambda: not b.models_req.busy)
check("Builder loaded the models from the real server", b.base_model.count() >= 1 and "gemma-4-e4b-it-erotica" in
      [b.base_model.itemText(i) for i in range(b.base_model.count())])
b.name.setText("gemma"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.system_prompt.setPlainText("Answer in English, very briefly."); b.temperature.setValue(0.2); b.max_tokens.setValue(300)
b.save_btn.click()
check("the profile is created and selected", sel.profile.currentText() == "gemma")

# 3. the first answer
send("Name the capital of France in one word.")
check("during generation: busy, status, chat created", not v.send_btn.isEnabled() and v.stop_btn.isEnabled()
      and v.status.text() == "Generating…" and w.chat_id is not None)
check("the model is locked at once", db.get_chat(w.chat_id)["profile"] == "gemma" and not sel.profile.isEnabled())
chat_a = w.chat_id
check("the answer finished", wait(lambda: w.stream is None), "timeout")
m = msgs()
check("in the DB user + assistant", [x["role"] for x in m] == ["user", "assistant"] and m[1]["content"].strip() != "", repr([x["content"] for x in m]))
print("      answer:", repr(m[1]["content"][:120]))
check("buttons are back, status = profile · model", v.send_btn.isEnabled() and not v.stop_btn.isEnabled()
      and v.status.text() == "gemma · gemma-4-e4b-it-erotica", v.status.text())
check("the answer is visible in the history", m[1]["content"].strip()[:10] in v.history.toPlainText())

# 4. continuing the dialog (the model sees the history)
send("Repeat my previous question word for word.")
wait(lambda: w.stream is None)
m = msgs()
check("the dialog goes on", len(m) == 4 and m[3]["content"].strip() != "")
print("      answer:", repr(m[3]["content"][:160]))
check("the model saw the history (mentions France/capital)", any(s in m[3]["content"].lower() for s in ("franc", "capital")))

# a profile for long answers (without the instruction to be brief)
b.name.setText("story"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.system_prompt.setPlainText(""); b.temperature.setValue(0.7); b.max_tokens.setValue(3000); b.save_btn.click()
got = lambda: (len(w.stream["content"]) + len(w.stream["reasoning"])) if w.stream else 0

# 5. Stop
w.new_chat(); chat_b = w.chat_id
sel.select("story"); sel.user_changed.emit("story")
check("in a new chat the profile is unlocked", sel.profile.isEnabled())
send("Write a long detailed story about the sea, at least 20 paragraphs.")
check("tokens started coming", wait(lambda: got() > 40, 60), "no tokens")
w.chat_view.stop_btn.click()
check("stop: free at once, status", w.stream is None and v.send_btn.isEnabled() and v.status.text() == "Stopped")
m = msgs(chat_b)
check("the partial answer is saved (with reasoning, if there was any)", len(m) == 2 and m[1]["role"] == "assistant"
      and len(m[1]["content"]) + len(m[1]["reasoning"]) > 20)
print("      saved: content=%d chars, reasoning=%d chars" % (len(m[1]["content"]), len(m[1]["reasoning"])))
snap = (m[1]["content"], m[1]["reasoning"]); time.sleep(1.5); app.processEvents()
m2 = msgs(chat_b)
check("nothing is added after the stop", len(m2) == 2 and (m2[1]["content"], m2[1]["reasoning"]) == snap)

# 6. switching chats during generation
w.new_chat(); chat_c = w.chat_id
sel.select("story"); sel.user_changed.emit("story")
send("List 40 different animals separated by commas.")
wait(lambda: got() > 20, 60)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == chat_a:
        w.chat_list.list.setCurrentRow(i)
check("in another chat: sending is blocked, status",
      w.chat_id == chat_a and not v.send_btn.isEnabled() and "another chat" in v.status.text())
check("and chat A has its profile locked", not sel.profile.isEnabled())
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == chat_c:
        w.chat_list.list.setCurrentRow(i)
snapshot = (w.stream["reasoning"] or w.stream["content"])[:15] if w.stream else ""
check("back to chat C: the live block is restored", (not snapshot) or snapshot in v.history.toPlainText())
check("generation in C finished", wait(lambda: w.stream is None))
m = msgs(chat_c)
check("the answer of C is saved in C, not in A", len(m) == 2 and (m[1]["content"].strip() or m[1]["reasoning"].strip())
      and len(msgs(chat_a)) == 4)

# 7a. the server substituted the model (LM Studio answers with the loaded one for an unknown name)
w.new_chat()
b.name.setText("ghost"); b.base_model.setCurrentText("no-such-model"); b.max_tokens.setValue(50); b.save_btn.click()
sel.select("ghost"); sel.user_changed.emit("ghost")
send("Say: hello")
wait(lambda: w.stream is None, 60)
swapped = "instead of \"no-such-model\"" in v.status.text() and "gemma-4-e4b-it-erotica" in v.status.text()
rejected = "Invalid model identifier" in v.status.text() and "no-such-model" in v.status.text()  # newer LM Studio answers 400
check("an unknown model: either a substitution warning or a server error", swapped or rejected, v.status.text())
print("      status:", v.status.text())

# 7b. an unreachable connection
c._new(); c.name.setText("dead"); c.url.setText("localhost:1"); c.save_btn.click()
wait(lambda: not b.models_req.busy)
b.name.setText("deadp"); b.connection.setCurrentText("dead"); b.base_model.setCurrentText("whatever"); b.save_btn.click()
w.new_chat(); sel.select("deadp"); sel.user_changed.emit("deadp")
send("hello")
wait(lambda: w.stream is None, 60)
check("a connection error in the status, no assistant in the DB", "Cannot reach" in v.status.text() and [x["role"] for x in msgs()] == ["user"], v.status.text())
check("after the error sending works again", v.send_btn.isEnabled())
check("after the error the chat is locked to the profile", db.get_chat(w.chat_id)["profile"] == "deadp")

# 7c. a cut-off by the token limit
b.name.setText("tiny"); b.connection.setCurrentText("lmstudio"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.max_tokens.setValue(25); b.save_btn.click()
w.new_chat(); sel.select("tiny"); sel.user_changed.emit("tiny")
send("Write a long detailed story about the sea.")
wait(lambda: w.stream is None, 60)
check("cut-off by the limit: a warning", "Max tokens limit" in v.status.text(), v.status.text())
m = msgs()
check("cut-off by the limit: the partial answer is saved", len(m) == 2 and (m[1]["content"] or m[1]["reasoning"]))

# 8. deleting a chat during generation
from PySide6.QtWidgets import QMessageBox
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
w.new_chat(); sel.select("story"); sel.user_changed.emit("story")
send("Write a very long story about space.")
wait(lambda: got() > 20, 60)
doomed = w.chat_id
w.chat_list._delete(w.chat_list.list.currentItem())
check("deleting the chat stops generation", w.stream is None and v.send_btn.isEnabled())
time.sleep(1.0); app.processEvents()
check("no orphaned messages in the DB", all(db.get_chat(r["chat_id"]) for r in db.connect().execute("SELECT DISTINCT chat_id FROM messages")))

w.close()
print("ALL OK" if ok_all else "SOME FAILED")


