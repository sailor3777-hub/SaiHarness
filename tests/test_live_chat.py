import sys, pathlib, time
from PySide6.QtWidgets import QApplication
from core import db
db.DB_PATH = pathlib.Path(sys.argv[1])  # временная база
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

# 1. без профиля
send("привет")
check("без профиля: подсказка, ввод не потерян, чат не создан",
      "Выбери модель" in v.status.text() and v.input.toPlainText() == "привет" and db.list_chats() == [])
v.input.clear()

# 2. подключение и профиль
c = ctx.connections; c.name.setText("lmstudio"); c.url.setText("localhost:1234"); c.save_btn.click()
b = ctx.builder
wait(lambda: not b.models_req.busy)
check("Builder подтянул модели с реального сервера", b.base_model.count() >= 1 and "gemma-4-e4b-it-erotica" in
      [b.base_model.itemText(i) for i in range(b.base_model.count())])
b.name.setText("gemma"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.system_prompt.setPlainText("Отвечай по-русски, очень кратко."); b.temperature.setValue(0.2); b.max_tokens.setValue(300)
b.save_btn.click()
check("профиль создан и выбран", sel.profile.currentText() == "gemma")

# 3. первый ответ
send("Назови столицу Франции одним словом.")
check("во время генерации: busy, статус, чат создан", not v.send_btn.isEnabled() and v.stop_btn.isEnabled()
      and v.status.text() == "Генерация…" and w.chat_id is not None)
check("модель закреплена сразу", db.get_chat(w.chat_id)["profile"] == "gemma" and not sel.profile.isEnabled())
chat_a = w.chat_id
check("ответ завершился", wait(lambda: w.stream is None), "таймаут")
m = msgs()
check("в БД user + assistant", [x["role"] for x in m] == ["user", "assistant"] and m[1]["content"].strip() != "", repr([x["content"] for x in m]))
print("      ответ:", repr(m[1]["content"][:120]))
check("кнопки возвращены, статус = профиль · модель", v.send_btn.isEnabled() and not v.stop_btn.isEnabled()
      and v.status.text() == "gemma · gemma-4-e4b-it-erotica", v.status.text())
check("ответ виден в истории", m[1]["content"].strip()[:10] in v.history.toPlainText())

# 4. продолжение диалога (модель видит историю)
send("Повтори мой предыдущий вопрос дословно.")
wait(lambda: w.stream is None)
m = msgs()
check("диалог продолжается", len(m) == 4 and m[3]["content"].strip() != "")
print("      ответ:", repr(m[3]["content"][:160]))
check("модель увидела историю (упоминает Франц/столиц)", any(s in m[3]["content"].lower() for s in ("франц", "столиц")))

# профиль для длинных ответов (без команды отвечать кратко)
b.name.setText("story"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.system_prompt.setPlainText(""); b.temperature.setValue(0.7); b.max_tokens.setValue(3000); b.save_btn.click()
got = lambda: (len(w.stream["content"]) + len(w.stream["reasoning"])) if w.stream else 0

# 5. Стоп
w.new_chat(); chat_b = w.chat_id
sel.select("story"); sel.user_changed.emit("story")
check("в новом чате профиль разблокирован", sel.profile.isEnabled())
send("Напиши длинный подробный рассказ про море, не меньше 20 абзацев.")
check("пошли токены", wait(lambda: got() > 40, 60), "нет токенов")
w.chat_view.stop_btn.click()
check("стоп: сразу свободно, статус", w.stream is None and v.send_btn.isEnabled() and v.status.text() == "Остановлено")
m = msgs(chat_b)
check("частичный ответ сохранён (с размышлениями, если они были)", len(m) == 2 and m[1]["role"] == "assistant"
      and len(m[1]["content"]) + len(m[1]["reasoning"]) > 20)
print("      сохранено: content=%d симв., reasoning=%d симв." % (len(m[1]["content"]), len(m[1]["reasoning"])))
snap = (m[1]["content"], m[1]["reasoning"]); time.sleep(1.5); app.processEvents()
m2 = msgs(chat_b)
check("после стопа ничего не дописывается", len(m2) == 2 and (m2[1]["content"], m2[1]["reasoning"]) == snap)

# 6. переключение чата во время генерации
w.new_chat(); chat_c = w.chat_id
sel.select("story"); sel.user_changed.emit("story")
send("Перечисли 40 разных животных через запятую.")
wait(lambda: got() > 20, 60)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == chat_a:
        w.chat_list.list.setCurrentRow(i)
check("в другом чате: отправка заблокирована, статус",
      w.chat_id == chat_a and not v.send_btn.isEnabled() and "другом чате" in v.status.text())
check("и профиль у чата A закреплён", not sel.profile.isEnabled())
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == chat_c:
        w.chat_list.list.setCurrentRow(i)
snapshot = (w.stream["reasoning"] or w.stream["content"])[:15] if w.stream else ""
check("возврат в чат C: живой блок восстановлен", (not snapshot) or snapshot in v.history.toPlainText())
check("генерация C завершилась", wait(lambda: w.stream is None))
m = msgs(chat_c)
check("ответ C сохранён в C, а не в A", len(m) == 2 and (m[1]["content"].strip() or m[1]["reasoning"].strip())
      and len(msgs(chat_a)) == 4)

# 7a. сервер подменил модель (LM Studio отвечает загруженной на неизвестное имя)
w.new_chat()
b.name.setText("ghost"); b.base_model.setCurrentText("no-such-model"); b.max_tokens.setValue(50); b.save_btn.click()
sel.select("ghost"); sel.user_changed.emit("ghost")
send("Скажи: привет")
wait(lambda: w.stream is None, 60)
swapped = "вместо «no-such-model»" in v.status.text() and "gemma-4-e4b-it-erotica" in v.status.text()
rejected = "Invalid model identifier" in v.status.text() and "no-such-model" in v.status.text()  # новее LM Studio отвечает 400
check("неизвестная модель: либо предупреждение о подмене, либо ошибка сервера", swapped or rejected, v.status.text())
print("      статус:", v.status.text())

# 7b. недоступное подключение
c._new(); c.name.setText("dead"); c.url.setText("localhost:1"); c.save_btn.click()
wait(lambda: not b.models_req.busy)
b.name.setText("deadp"); b.connection.setCurrentText("dead"); b.base_model.setCurrentText("whatever"); b.save_btn.click()
w.new_chat(); sel.select("deadp"); sel.user_changed.emit("deadp")
send("привет")
wait(lambda: w.stream is None, 60)
check("ошибка связи в статусе, ассистента в БД нет", "Нет связи" in v.status.text() and [x["role"] for x in msgs()] == ["user"], v.status.text())
check("после ошибки можно отправлять снова", v.send_btn.isEnabled())
check("после ошибки чат закреплён за профилем", db.get_chat(w.chat_id)["profile"] == "deadp")

# 7c. обрыв по лимиту токенов
b.name.setText("tiny"); b.connection.setCurrentText("lmstudio"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica")
b.max_tokens.setValue(25); b.save_btn.click()
w.new_chat(); sel.select("tiny"); sel.user_changed.emit("tiny")
send("Напиши длинный подробный рассказ про море.")
wait(lambda: w.stream is None, 60)
check("обрыв по лимиту: предупреждение", "Лимит max tokens" in v.status.text(), v.status.text())
m = msgs()
check("обрыв по лимиту: частичный ответ сохранён", len(m) == 2 and (m[1]["content"] or m[1]["reasoning"]))

# 8. удаление чата во время генерации
from PySide6.QtWidgets import QMessageBox
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
w.new_chat(); sel.select("story"); sel.user_changed.emit("story")
send("Напиши очень длинную историю про космос.")
wait(lambda: got() > 20, 60)
doomed = w.chat_id
w.chat_list._delete(w.chat_list.list.currentItem())
check("удаление чата останавливает генерацию", w.stream is None and v.send_btn.isEnabled())
time.sleep(1.0); app.processEvents()
check("в БД нет осиротевших сообщений", all(db.get_chat(r["chat_id"]) for r in db.connect().execute("SELECT DISTINCT chat_id FROM messages")))

w.close()
print("ALL OK" if ok_all else "SOME FAILED")


