"""Вкладка «Память»: список, фильтр, поиск, правка, создание, удаление, перечитывание при открытии."""
import os, pathlib, sqlite3, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from core import db
from core.memory import Memory

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

db.DB_PATH = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp() + "/t.db")
conn = db.connect()
mem = Memory(conn)
a, _ = mem.save("поездки", "Маккум", "Ездили в Маккум в сентябре.")
b, _ = mem.save("поездки", "Байкал", "Летом был на Байкале.")
c, _ = mem.save("проект", "Решение", "Память общая для всех профилей.")

app = QApplication([])
from ui.memory_panel import MemoryPanel
panel = MemoryPanel()

def rows():
    return [(panel.tree.topLevelItem(i).text(0), panel.tree.topLevelItem(i).text(1)) for i in range(panel.tree.topLevelItemCount())]

check("три записи в списке", len(rows()) == 3, rows())
check("фильтр тем: все + две темы", panel.topic_filter.count() == 3 and "поездки (2)" in [panel.topic_filter.itemText(i) for i in range(3)])
check("инфо: число записей", "записей: 3" in panel.info.text(), panel.info.text())
check("удалять нечего, пока ничего не выбрано", not panel.delete_btn.isEnabled())

panel.topic_filter.setCurrentIndex(panel.topic_filter.findData("проект"))
check("фильтр по теме", rows() == [("проект", "Решение")], rows())
panel.topic_filter.setCurrentIndex(0)

panel.query.setText("маккуме")
check("поиск по другой словоформе", rows() == [("поездки", "Маккум")], rows())
panel.query.clear()
check("очистка поиска возвращает всё", len(rows()) == 3)

# --- выбор и правка ---
for i in range(panel.tree.topLevelItemCount()):
    if panel.tree.topLevelItem(i).text(1) == "Маккум":
        panel.tree.setCurrentItem(panel.tree.topLevelItem(i))
check("выбор показывает запись в редакторе", panel.title.text() == "Маккум" and "сентябре" in panel.text.toPlainText() and panel.topic.text() == "поездки")
check("удаление теперь доступно", panel.delete_btn.isEnabled())
panel.text.setPlainText("Ездили в Маккум в сентябре втроём.")
panel.save_btn.click()
check("правка сохранена в базе", "втроём" in Memory(db.connect()).get(a)["text"])
check("после сохранения запись остаётся выбранной", panel.current_id() == a and "Обновлена" in panel.status.text(), panel.status.text())
check("и поиск находит новый текст", Memory(db.connect()).search("втроём")[0]["id"] == a)

# --- несохранённый текст не теряется при перечитывании ---
panel.text.setPlainText("несохранённая правка")
panel.reload()
check("reload не затирает редактор", panel.text.toPlainText() == "несохранённая правка" and panel.current_id() == a)
panel.text.setPlainText("Ездили в Маккум в сентябре втроём.")

# --- перенос в другую тему ---
panel.topic.setText("отдых")
panel.save_btn.click()
check("смена темы: запись видна, фильтры сняты", panel.current_id() == a and panel.topic_filter.currentIndex() == 0 and ("отдых", "Маккум") in rows(), rows())

# --- новая запись ---
panel.new_btn.click()
check("«Новая запись» очищает редактор и выбор", panel.current_id() is None and panel.title.text() == "" and not panel.delete_btn.isEnabled())
panel.save_btn.click()
check("пустая запись не сохраняется, ошибка понятная", "Не сохранено" in panel.status.text() and Memory(db.connect()).count() == 3, panel.status.text())
panel.topic.setText("идеи"); panel.title.setText("Вкладка памяти"); panel.text.setPlainText("Сделать просмотр и правку.")
panel.save_btn.click()
new_id = panel.current_id()
check("новая запись добавлена и выбрана", new_id is not None and Memory(db.connect()).get(new_id)["title"] == "Вкладка памяти" and Memory(db.connect()).count() == 4)

# --- запись из другого процесса видна после открытия вкладки ---
ext = sqlite3.connect(db.DB_PATH); Memory(ext).save("внешняя", "Из инструмента", "Записано моделью"); ext.close()
check("до обновления её нет", all(r[0] != "внешняя" for r in rows()))
panel.showEvent(QShowEvent())
check("после showEvent она в списке", ("внешняя", "Из инструмента") in rows(), rows())

# --- удаление ---
answers = iter([QMessageBox.No, QMessageBox.Yes])
QMessageBox.question = staticmethod(lambda *args, **kw: next(answers))
before = Memory(db.connect()).count()
panel.delete_btn.click()
check("отказ: ничего не удалено", Memory(db.connect()).count() == before and panel.current_id() == new_id)
panel.delete_btn.click()
check("подтверждение: запись удалена, редактор пуст", Memory(db.connect()).get(new_id) is None and panel.current_id() is None and panel.title.text() == "", panel.status.text())
check("список обновился", all(r[1] != "Вкладка памяти" for r in rows()))

print("\nALL OK" if ok_all else "\nЕСТЬ ОШИБКИ")
