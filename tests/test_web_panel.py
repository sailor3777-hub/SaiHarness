"""Вкладка «Веб»: добавление сайта, фоновый обход настоящим краулером на локальном http-сервере, поиск, удаление."""
import http.server, os, pathlib, sqlite3, sys, tempfile, threading, time
os.environ["SAI_WEB_ALLOW_PRIVATE"] = "1"   # локальный сервер; в приложении это не включается
os.environ["SAI_WEB_DELAY"] = "0"
from PySide6.QtWidgets import QApplication, QMessageBox
from core import db
from core.web.index import WebIndex

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

db.DB_PATH = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(tempfile.mkdtemp()) / "t.db"
for f in db.DB_PATH.parent.glob(db.DB_PATH.name + "*"):
    f.unlink(missing_ok=True)
from ui.web_panel import WebPanel   # после подмены DB_PATH

LONG = " ".join(["подробный текст страницы"] * 20)
def page(title, body, links=()):
    return (f"<html><head><meta charset='utf-8'><title>{title}</title></head><body><main><h1>{title}</h1><p>{body} {LONG}</p>"
            + "".join(f"<a href='{l}'>x</a>" for l in links) + "</main></body></html>")
PAGES = {"/": page("Главная", "Начало.", ["/a", "/b"]), "/a": page("Страница А", "Слон идёт по лесу."),
         "/b": page("Страница Б", "Жираф ест листья.")}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if self.path == "/robots.txt" or self.path == "/sitemap.xml":
            body = None
        self.send_response(200 if body else 404); self.send_header("content-type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write((body or "нет").encode("utf-8"))
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/"

app = QApplication([])
def wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end and not cond():
        app.processEvents(); time.sleep(0.02)
    return cond()

w = WebPanel()
check("поле SearXNG пусто при старте", w.searx_url.text() == "")
w.searx_url.setText("127.0.0.1:1"); w.searx_check.click()
check("проверка недоступного SearXNG показывает ошибку", wait(lambda: not w.searx_probe.busy) and ("недоступен" in w.searx_status.text() or "не ответил" in w.searx_status.text()), w.searx_status.text())
check("адрес сохранён и нормализован", w.searx_url.text() == "http://127.0.0.1:1")
w2 = WebPanel()
check("адрес переживает пересоздание панели", w2.searx_url.text() == "http://127.0.0.1:1"); w2.wait_threads()
w.searx_url.setText("ftp://x"); w.searx_url.editingFinished.emit()
check("не-http адрес SearXNG отклонён", "Некорректный адрес" in w.searx_status.text(), w.searx_status.text())
w.searx_url.setText(""); w.searx_url.editingFinished.emit()
check("очистка поля стирает настройку", WebPanel().searx_url.text() == "")
check("панель пуста при старте", w.tree.topLevelItemCount() == 0 and not w.stop_btn.isEnabled())
changes = []; w.changed.connect(lambda: changes.append(1))
w.url.setText(base); w.depth.setValue(1); w.add_btn.click()
check("сайт появился и идёт обход", w.tree.topLevelItemCount() == 1 and w._crawling_id is not None and w.stop_btn.isEnabled())
check("обход завершился", wait(lambda: w._thread is None and w._crawling_id is None))
item = w.tree.topLevelItem(0)
check("в таблице 3 страницы и состояние «готово»", item.text(1) == "3" and item.text(3) == "готово", [item.text(i) for i in range(4)])
check("сигнал changed отправлен", len(changes) >= 2)
check("итог показан пользователю", "Готово" in w.info.text(), w.info.text())
conn = sqlite3.connect(db.DB_PATH); idx = WebIndex(conn)
check("поиск находит содержимое, собранное панелью", idx.search("жираф")[0]["title"] == "Страница Б")
w.url.setText("not a url ://"); w.add_btn.click()
check("мусорный адрес не ломает панель", w.tree.topLevelItemCount() >= 1)
w.url.setText("ftp://example.com"); w.add_btn.click()
check("не-http адрес отклонён с сообщением", "Некорректный адрес" in w.info.text() and idx.sources().__len__() == w.tree.topLevelItemCount(), w.info.text())

w.tree.topLevelItem(0).setSelected(True)
w.refresh_btn.click()
check("обновление ставит обход заново", wait(lambda: w._thread is None) and w.tree.topLevelItem(0).text(1) == "3")
w.tree.topLevelItem(0).setSelected(True)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
w.remove_btn.click()
check("отказ в подтверждении ничего не удаляет", w.tree.topLevelItemCount() == 1 and idx.page_count() == 3)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
w.tree.topLevelItem(0).setSelected(True); w.remove_btn.click()
check("удаление чистит список и индекс", w.tree.topLevelItemCount() == 0 and idx.page_count() == 0 and idx.search("жираф") == [])

# остановка обхода посреди работы
w.url.setText(base); w.depth.setValue(1); w.add_btn.click(); w.stop_btn.click()
check("остановка завершает поток", wait(lambda: w._thread is None))
w.wait_threads()
conn.close(); srv.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
