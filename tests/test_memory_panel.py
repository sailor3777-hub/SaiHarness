"""The Memory tab: list, filter, search, editing, creating, deleting, re-reading when opened."""
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
a, _ = mem.save("trips", "Makkum", "We went camping to Makkum in September.")
b, _ = mem.save("trips", "Baikal", "In summer I was at Lake Baikal.")
c, _ = mem.save("project", "Decision", "Memory is shared by all profiles.")

app = QApplication([])
from ui.memory_panel import MemoryPanel
panel = MemoryPanel()

def rows():
    return [(panel.tree.topLevelItem(i).text(0), panel.tree.topLevelItem(i).text(1)) for i in range(panel.tree.topLevelItemCount())]

check("three entries in the list", len(rows()) == 3, rows())
check("topic filter: all + two topics", panel.topic_filter.count() == 3 and "trips (2)" in [panel.topic_filter.itemText(i) for i in range(3)])
check("info: number of entries", "entries: 3" in panel.info.text(), panel.info.text())
check("nothing to delete while nothing is selected", not panel.delete_btn.isEnabled())

panel.topic_filter.setCurrentIndex(panel.topic_filter.findData("project"))
check("filter by topic", rows() == [("project", "Decision")], rows())
panel.topic_filter.setCurrentIndex(0)

panel.query.setText("camps")
check("search by another word form", rows() == [("trips", "Makkum")], rows())
panel.query.clear()
check("clearing the search brings everything back", len(rows()) == 3)

# --- selecting and editing ---
for i in range(panel.tree.topLevelItemCount()):
    if panel.tree.topLevelItem(i).text(1) == "Makkum":
        panel.tree.setCurrentItem(panel.tree.topLevelItem(i))
check("selecting shows the entry in the editor", panel.title.text() == "Makkum" and "September" in panel.text.toPlainText() and panel.topic.text() == "trips")
check("deleting is now available", panel.delete_btn.isEnabled())
panel.text.setPlainText("We went camping to Makkum in September, the three of us.")
panel.save_btn.click()
check("the edit is saved in the database", "three of us" in Memory(db.connect()).get(a)["text"])
check("after saving the entry stays selected", panel.current_id() == a and "Updated" in panel.status.text(), panel.status.text())
check("and the search finds the new text", Memory(db.connect()).search("three")[0]["id"] == a)

# --- unsaved text is not lost on re-reading ---
panel.text.setPlainText("an unsaved edit")
panel.reload()
check("reload does not overwrite the editor", panel.text.toPlainText() == "an unsaved edit" and panel.current_id() == a)
panel.text.setPlainText("We went camping to Makkum in September, the three of us.")

# --- moving to another topic ---
panel.topic.setText("holidays")
panel.save_btn.click()
check("changing the topic: the entry is visible, filters cleared", panel.current_id() == a and panel.topic_filter.currentIndex() == 0 and ("holidays", "Makkum") in rows(), rows())

# --- a new entry ---
panel.new_btn.click()
check("\"New entry\" clears the editor and the selection", panel.current_id() is None and panel.title.text() == "" and not panel.delete_btn.isEnabled())
panel.save_btn.click()
check("an empty entry is not saved, a clear error", "Not saved" in panel.status.text() and Memory(db.connect()).count() == 3, panel.status.text())
panel.topic.setText("ideas"); panel.title.setText("Memory tab"); panel.text.setPlainText("Build viewing and editing.")
panel.save_btn.click()
new_id = panel.current_id()
check("a new entry is added and selected", new_id is not None and Memory(db.connect()).get(new_id)["title"] == "Memory tab" and Memory(db.connect()).count() == 4)

# --- an entry from another process is visible after opening the tab ---
ext = sqlite3.connect(db.DB_PATH); Memory(ext).save("external", "From the tool", "Written by the model"); ext.close()
check("before refreshing it is not there", all(r[0] != "external" for r in rows()))
panel.showEvent(QShowEvent())
check("after showEvent it is in the list", ("external", "From the tool") in rows(), rows())

# --- deleting ---
answers = iter([QMessageBox.No, QMessageBox.Yes])
QMessageBox.question = staticmethod(lambda *args, **kw: next(answers))
before = Memory(db.connect()).count()
panel.delete_btn.click()
check("refusal: nothing deleted", Memory(db.connect()).count() == before and panel.current_id() == new_id)
panel.delete_btn.click()
check("confirmation: the entry is deleted, the editor is empty", Memory(db.connect()).get(new_id) is None and panel.current_id() is None and panel.title.text() == "", panel.status.text())
check("the list is updated", all(r[1] != "Memory tab" for r in rows()))

print("\nALL OK" if ok_all else "\nSOME FAILED")
