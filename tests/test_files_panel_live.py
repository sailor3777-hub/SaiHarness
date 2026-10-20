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
w("notes.txt", "Секретное слово проекта: ФИОЛЕТОВЫЙ-МАМОНТ-42\n"); w("sub/code.py", "x = 1\n")
w("sub/deep/more.md", "# doc\n"); w("img.png", b"\x89PNG\x00", "wb"); w(".git/config", "x"); w("zeta.txt", "z")
P = lambda *p: os.path.join(root, *p)

def children(item): return [item.child(i) for i in range(item.childCount())]
def find(item, name): return next((c for c in children(item) if c.data(0, PATH_ROLE) and os.path.basename(c.data(0, PATH_ROLE)) == name), None)

# ---------- панель ----------
panel = FilesPanel()
check("панель: корней нет, подсказка", panel.tree.topLevelItemCount() == 0 and "Файлы не выбраны" in panel.info.text())
check("добавление корня", panel.add_root(root) and panel.tree.topLevelItemCount() == 1 and db.list_roots() == [root])
check("вложенный корень отклонён", not panel.add_root(P("sub")) and "уже входит" in panel.info.text() and db.list_roots() == [root])
check("родительский корень отклонён", not panel.add_root(tmp) and "Внутри уже добавлена" in panel.info.text())
r = panel.tree.topLevelItem(0)
check("корень: ленивая загрузка (заглушка)", r.childCount() == 1 and r.child(0).data(0, KIND_ROLE) == "placeholder")
r.setExpanded(True)
names = [c.text(0) for c in children(r)]
check("содержимое: папки первыми, .git скрыт, картинка помечена", names == ["sub", "img.png  (картинка: в запрос идёт путь)", "notes.txt", "zeta.txt"], names)
img = next(c for c in children(r) if c.text(0).startswith("img.png"))
check("картинку можно отметить", bool(img.flags() & Qt.ItemIsUserCheckable))
changed = []; panel.selection_changed.connect(lambda: changed.append(1))
r.setCheckState(0, Qt.Checked)
check("галочка корня: выбор и сигнал", panel.selection.include == {root} and changed == [1])
sub = find(r, "sub"); sub.setExpanded(True)
check("потомки показаны отмеченными", sub.checkState(0) == Qt.Checked and find(r, "notes.txt").checkState(0) == Qt.Checked
      and find(sub, "code.py").checkState(0) == Qt.Checked)
check("картинка отмечена вместе с папкой", img.checkState(0) == Qt.Checked)
find(sub, "code.py").setCheckState(0, Qt.Unchecked)
check("снятие файла: исключение, родители частично", panel.selection.exclude == {P("sub", "code.py")}
      and sub.checkState(0) == Qt.PartiallyChecked and r.checkState(0) == Qt.PartiallyChecked and len(changed) == 2)
check("сводка считается в фоне", wait(lambda: panel._summary is not None), panel.info.text())
check("сводка: файлов и токенов", panel._summary["files"] == 4 and panel._summary["tokens"] > 0, panel._summary)  # notes, more.md, zeta, img.png
check("сводка в тексте", "Файлов: 4" in panel.info.text() and "ток." in panel.info.text(), panel.info.text())
panel.set_extra_tokens(1000); tokens_total = 1000 + panel._summary["tokens"]
check("«запрос целиком» = файлы + остальное", f"≈{tokens_total:,}".replace(",", " ") in panel.info.text(), panel.info.text())
panel.set_limit((tokens_total * 10, True, None)); check("лимит с запасом: обычный цвет", COLOR_NORMAL in panel.info.styleSheet() and "из" in panel.info.text())
panel.set_limit((int(tokens_total / 0.9), True, None)); check("больше 80% лимита: жёлтый", COLOR_WARN in panel.info.styleSheet())
panel.set_limit((tokens_total - 1, True, None)); check("выше лимита: красный", COLOR_OVER in panel.info.styleSheet())
panel.set_limit((None, False, 262144)); check("модель не загружена: честная пометка", "лимит неизвестен" in panel.info.text() and "262 144" in panel.info.text(), panel.info.text())
panel.set_limit(None); check("нет данных о лимите: без пометки", "из " not in panel.info.text() and "лимит" not in panel.info.text())

# восстановление выбора из БД: раскрываются нужные папки
panel.set_selection(Selection({P("sub", "deep", "more.md")}))
deep = find(find(r, "sub"), "deep")
check("set_selection раскрывает путь до отмеченного файла", deep is not None and deep.isExpanded()
      and find(deep, "more.md").checkState(0) == Qt.Checked and r.checkState(0) == Qt.PartiallyChecked)
check("set_selection не шлёт selection_changed", len(changed) == 2)

# «Обновить» подхватывает новые файлы и сохраняет раскрытое
w("new_file.txt", "n"); panel.rebuild()
r = panel.tree.topLevelItem(0)
check("Обновить: новый файл виден, раскрытое сохранено", find(r, "new_file.txt") is not None and find(find(r, "sub"), "deep").isExpanded())

# удаление корня чистит выбор и шлёт сигнал
panel.tree.setCurrentItem(r); n = len(changed); panel._remove_root()
check("удаление корня: список, выбор, сигнал", panel.roots == [] and panel.selection.is_empty() and len(changed) == n + 1 and db.list_roots() == [])
check("файлы на диске целы", os.path.exists(P("notes.txt")))
panel.add_root(root)

# ---------- главное окно: выбор по чатам и боевой запрос ----------
w_ = MainWindow(); fp = w_.files_panel; v = w_.chat_view; ctx = w_.context
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1234"); c.save_btn.click()
b = ctx.builder; wait(lambda: not b.models_req.busy)
b.name.setText("gemma"); b.base_model.setCurrentText("gemma-4-e4b-it-erotica"); b.max_tokens.setValue(1500); b.temperature.setValue(0.2); b.save_btn.click()
check("окно: корень из БД показан", fp.roots == [root])

# отмечаем файл ДО создания чата, затем отправляем: выбор переходит в чат
fr = fp.tree.topLevelItem(0); fr.setExpanded(True)
find(fr, "notes.txt").setCheckState(0, Qt.Checked)
check("черновик: чата ещё нет", w_.chat_id is None and fp.selection.include == {P("notes.txt")})
wait(lambda: fp._summary is not None); check("оценка до отправки", fp._summary["files"] == 1)
wait(lambda: not w_.limit_req.busy)
check("лимит модели подтянулся в панель", fp._limit_info and fp._limit_info[0] and "из" in fp.info.text(), (fp._limit_info, fp.info.text()))
v.input.setPlainText("Какое секретное слово проекта указано в файле? Ответь одним словом."); v.send_btn.click()
check("чат создан, выбор сохранён в нём", w_.chat_id is not None and db.load_chat_context(w_.chat_id) == ({P("notes.txt")}, set()))
req = w_.last_requests[w_.chat_id]
check("файл ушёл в запрос (system)", "=== proj/notes.txt ===" in req["messages"][0]["content"] and "ФИОЛЕТОВЫЙ-МАМОНТ-42" in req["messages"][0]["content"])
check("статус во время генерации: файлов 1", "файлов: 1" in v.status.text(), v.status.text())
check("ответ завершился", wait(lambda: w_.stream is None, 120))
ans = db.list_messages(w_.chat_id)[-1]
text = ans["content"] + " " + ans["reasoning"]
print("      ответ модели:", repr(ans["content"][:150]))
check("модель прочитала файл (ответ содержит секретное слово)", "МАМОНТ" in ans["content"].upper() or "МАМОНТ" in ans["reasoning"].upper(), text[:200])
first_chat = w_.chat_id

# новый чат начинается без файлов; прежний хранит свои
w_.new_chat()
check("новый чат: выбор пуст", fp.selection.is_empty() and "Файлы не выбраны" in fp.info.text())
check("в новом чате отправка не включает файлов", w_.files_panel.load_bundle().files == [])
for i in range(w_.chat_list.list.count()):
    if w_.chat_list.list.item(i).data(0x100) == first_chat: w_.chat_list.list.setCurrentRow(i)
fr = fp.tree.topLevelItem(0)
check("возврат в чат: выбор восстановлен в дереве", fp.selection.include == {P("notes.txt")} and find(fr, "notes.txt").checkState(0) == Qt.Checked)

# оценка «запрос целиком» учитывает историю
wait(lambda: fp._summary is not None)
check("в оценке учтена история чата", fp._extra_tokens > 0)

# слишком большой выбор не отправляется
import core.files as cf
old = cf.MAX_FILES; cf.MAX_FILES = 1
fr.setCheckState(0, Qt.Checked)
v.input.setPlainText("привет"); v.send_btn.click()
cf.MAX_FILES = old
check("слишком большой выбор: отказ, ввод цел, сообщение не сохранено",
      "слишком много" in v.status.text() and v.input.toPlainText() == "привет" and len(db.list_messages(first_chat)) == 2 and w_.stream is None, v.status.text())
v.input.clear()

# удалённый на диске файл не роняет отправку
fr.setCheckState(0, Qt.Unchecked); find(fr, "notes.txt").setCheckState(0, Qt.Checked)
os.remove(P("notes.txt"))
v.input.setPlainText("ответь одним словом: привет"); v.send_btn.click()
check("пропавший файл: отправка идёт, пропуск отмечен в статусе", w_.stream is not None and "пропущено: 1" in v.status.text(), v.status.text())
wait(lambda: w_.stream is None, 120)

w_.close(); shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK" if ok_all else "SOME FAILED")
