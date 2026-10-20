import sys, pathlib
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db
from core.connection import normalize_url
db.DB_PATH = pathlib.Path(sys.argv[1])
from ui.context_panel import ContextPanel

ok_all = True
def check(name, cond):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name)

cases = {
    "192.168.1.5": "http://192.168.1.5:1234/v1",
    "localhost": "http://localhost:1234/v1",
    "localhost:5000": "http://localhost:5000/v1",
    " 10.0.0.2:1234/ ": "http://10.0.0.2:1234/v1",
    "http://host:8080": "http://host:8080/v1",
    "http://host:8080/v1/": "http://host:8080/v1",
    "https://example.com": "https://example.com/v1",
    "https://example.com/custom/v1/": "https://example.com/custom/v1",
    "[::1]": "http://[::1]:1234/v1",
}
for raw, want in cases.items():
    got = normalize_url(raw)
    check(f"normalize {raw!r} -> {got}", got == want)
for bad in ["", "   ", "ftp://host", "http://", "host:99999", "host:abc"]:
    try:
        normalize_url(bad); check(f"reject {bad!r}", False)
    except ValueError:
        check(f"reject {bad!r}", True)

app = QApplication([])
p = ContextPanel(); c = p.connections
emitted = []
c.changed.connect(lambda: emitted.append(1))
check("пустой список: кнопки выключены", not c.edit_btn.isEnabled() and not c.delete_btn.isEnabled())

c.name.setText("home"); c.url.setText("192.168.1.5"); c.api_key.setText("secret"); c.save_btn.click()
row = db.list_connections()[0]
check("сохранено с нормализацией", row["url"] == "http://192.168.1.5:1234/v1" and row["api_key"] == "secret")
check("в списке и сигнал", c.list.count() == 1 and emitted == [1] and c.edit_btn.isEnabled())

c.name.setText("second"); c._new(); c.name.setText("lab"); c.url.setText("lab.local:1235"); c.save_btn.click()
check("второе подключение", c.list.count() == 2)

c.name.setText("home"); c.save_btn.click()
check("дубль имени отклонён", len(db.list_connections()) == 2 and "занято" in c.status.text())

c._new(); c.name.setText("home"); c.url.setText("1.1.1.1"); c.save_btn.click()
check("дубль в режиме 'новое' отклонён", db.list_connections()[0]["url"] != "http://1.1.1.1:1234/v1")

for i in range(c.list.count()):
    if c.list.item(i).data(0x100) == "home":
        c.list.setCurrentRow(i)
c.edit_btn.click()
check("загрузка в форму", c.name.text() == "home" and c.url.text().endswith(":1234/v1") and c.api_key.text() == "secret")
c.name.setText("home2"); c.url.setText("192.168.1.9"); c.save_btn.click()
names = [r["name"] for r in db.list_connections()]
check("переименование без копии", names == ["home2", "lab"], )
check("url обновлён", db.list_connections()[0]["url"] == "http://192.168.1.9:1234/v1")

c.url.setText("http://"); c.save_btn.click()
check("невалидный URL: сообщение и без изменений", c.status.text() == "Не найден хост"
      and db.list_connections()[0]["url"] == "http://192.168.1.9:1234/v1")

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
c.delete_btn.click()
check("удаление", [r["name"] for r in db.list_connections()] == ["lab"] and c.name.text() == "")
c.list.setCurrentRow(0); c.delete_btn.click()
check("удалён последний: кнопки выключены", c.list.count() == 0 and not c.edit_btn.isEnabled())
print("ALL OK" if ok_all else "SOME FAILED")
