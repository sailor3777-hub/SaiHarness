"""The Web tab: adding a site, a background crawl with the real crawler on a local http server, search, deleting."""
import http.server, os, pathlib, sqlite3, sys, tempfile, threading, time
os.environ["SAI_WEB_ALLOW_PRIVATE"] = "1"   # a local server; the app never enables this
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
from ui.web_panel import WebPanel   # after DB_PATH is replaced

LONG = " ".join(["detailed page text"] * 20)
def page(title, body, links=()):
    return (f"<html><head><meta charset='utf-8'><title>{title}</title></head><body><main><h1>{title}</h1><p>{body} {LONG}</p>"
            + "".join(f"<a href='{l}'>x</a>" for l in links) + "</main></body></html>")
PAGES = {"/": page("Home", "Start.", ["/a", "/b"]), "/a": page("Page A", "An elephant walks through the forest."),
         "/b": page("Page B", "A giraffe eats leaves.")}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if self.path == "/robots.txt" or self.path == "/sitemap.xml":
            body = None
        self.send_response(200 if body else 404); self.send_header("content-type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write((body or "none").encode("utf-8"))
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
check("the SearXNG field is empty at start", w.searx_url.text() == "")
w.searx_url.setText("127.0.0.1:1"); w.searx_check.click()
check("checking an unavailable SearXNG shows an error", wait(lambda: not w.searx_probe.busy) and ("unavailable" in w.searx_status.text() or "did not answer" in w.searx_status.text()), w.searx_status.text())
check("the address is saved and normalized", w.searx_url.text() == "http://127.0.0.1:1")
w2 = WebPanel()
check("the address survives recreating the panel", w2.searx_url.text() == "http://127.0.0.1:1"); w2.wait_threads()
w.searx_url.setText("ftp://x"); w.searx_url.editingFinished.emit()
check("a non-http SearXNG address is refused", "Invalid" in w.searx_status.text(), w.searx_status.text())
w.searx_url.setText(""); w.searx_url.editingFinished.emit()
check("clearing the field erases the setting", WebPanel().searx_url.text() == "")
check("the panel is empty at start", w.tree.topLevelItemCount() == 0 and not w.stop_btn.isEnabled())
changes = []; w.changed.connect(lambda: changes.append(1))
w.url.setText(base); w.depth.setValue(1); w.add_btn.click()
check("the site appeared and the crawl is running", w.tree.topLevelItemCount() == 1 and w._crawling_id is not None and w.stop_btn.isEnabled())
check("the crawl finished", wait(lambda: w._thread is None and w._crawling_id is None))
item = w.tree.topLevelItem(0)
check("the table has 3 pages and the status \"done\"", item.text(1) == "3" and item.text(3) == "done", [item.text(i) for i in range(4)])
check("the changed signal was sent", len(changes) >= 2)
check("the result is shown to the user", "Done" in w.info.text(), w.info.text())
conn = sqlite3.connect(db.DB_PATH); idx = WebIndex(conn)
check("search finds the content collected by the panel", idx.search("giraffe")[0]["title"] == "Page B")
w.url.setText("not a url ://"); w.add_btn.click()
check("a junk address does not break the panel", w.tree.topLevelItemCount() >= 1)
w.url.setText("ftp://example.com"); w.add_btn.click()
check("a non-http address is refused with a message", "Invalid" in w.info.text() and idx.sources().__len__() == w.tree.topLevelItemCount(), w.info.text())

w.tree.topLevelItem(0).setSelected(True)
w.refresh_btn.click()
check("refreshing starts the crawl again", wait(lambda: w._thread is None) and w.tree.topLevelItem(0).text(1) == "3")
w.tree.topLevelItem(0).setSelected(True)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
w.remove_btn.click()
check("refusing the confirmation deletes nothing", w.tree.topLevelItemCount() == 1 and idx.page_count() == 3)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
w.tree.topLevelItem(0).setSelected(True); w.remove_btn.click()
check("deleting clears the list and the index", w.tree.topLevelItemCount() == 0 and idx.page_count() == 0 and idx.search("giraffe") == [])

# stopping a crawl in the middle
w.url.setText(base); w.depth.setValue(1); w.add_btn.click(); w.stop_btn.click()
check("stopping ends the thread", wait(lambda: w._thread is None))
w.wait_threads()
conn.close(); srv.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
