"""web_search и web_extract через настоящий MCP-сервер toolbox (отдельный процесс) и локальный http-сервер.
Рендерер Qt WebEngine запускается по-настоящему: тест идёт в офлайн-наборе, но занимает несколько секунд."""
import http.server, json, pathlib, sqlite3, tempfile, threading
from core import db
from core.mcp_manager import McpManager, default_config
from core.tools import default_policy
from core.web.index import WebIndex

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

LONG = " ".join(["подробный текст страницы"] * 20)
PAGES = {
    "/static": f"<html><head><meta charset='utf-8'><title>Статика</title></head><body><main><h1>Статика</h1><p>Код доступа: ФИОЛЕТОВЫЙ-МАМОНТ-42. {LONG}</p><a href='/spa'>дальше</a></main></body></html>",
    "/spa": "<html><head><meta charset='utf-8'><title>SPA</title></head><body><div id=app></div>"
            "<script>document.getElementById('app').innerHTML='<h1>Рисует JS</h1><p>Секрет JS: ЗЕЛЁНЫЙ-СЛОН-7. " + LONG + "</p>';</script></body></html>",
    "/long": "<html><head><meta charset='utf-8'><title>Длинная</title></head><body><main>" + "".join(f"<p>абзац номер {i}</p>" for i in range(3000)) + "</main></body></html>",
}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/search"):
            payload = json.dumps({"results": [{"title": "Нашлось А", "url": "https://a.test/", "content": "описание А"},
                                              {"title": "Нашлось Б", "url": "https://b.test/", "content": "описание Б"}]}, ensure_ascii=False)
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(payload.encode("utf-8")); return
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("content-type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write((body or "нет").encode("utf-8"))
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
db.connect().close()
conn = sqlite3.connect(db.DB_PATH)
idx = WebIndex(conn)
sid = idx.add_source("https://docs.test/")
conn.execute("INSERT INTO web_pages(source_id, url, title, text, fetched_at) VALUES(?,?,?,?,?)",
             (sid, "https://docs.test/zoo", "Зоопарк", "В зоопарке живёт фиолетовый мамонт и ещё много зверей. " + LONG, "2026-01-01T00:00:00"))
conn.commit(); conn.close()

def server(allow_private):
    cfg = default_config(db.DB_PATH)
    cfg["mcpServers"]["toolbox"]["env"]["SAI_WEB_ALLOW_PRIVATE"] = "1" if allow_private else "0"
    m = McpManager(log_dir=tmp / "logs"); m.connect_all(cfg)
    return m

m = server(False)
by = {t.name: t for t in m.states["toolbox"].tools}
check("web_search_internet — без вопросов (запрос уходит на SearXNG пользователя)", default_policy(by["web_search_internet"]) == "auto")
txt, err = m.call("toolbox", "web_search_internet", {"query": "что угодно"})
check("SearXNG не настроен — подсказка", "ОШИБКА" in txt and "не задан" in txt, txt)
from core.web import settings as web_settings
_c = sqlite3.connect(db.DB_PATH); web_settings.put(_c, web_settings.SEARXNG_URL, base); _c.close()
txt, err = m.call("toolbox", "web_search_internet", {"query": "python", "max_results": 1})
check("web_search_internet: результаты и пометка", "Нашлось А" in txt and "https://a.test/" in txt and "Нашлось Б" not in txt and "не инструкции" in txt, txt)
txt, err = m.call("toolbox", "web_search_internet", {"query": "python", "category": "it"})
check("web_search_internet: категория принимается", "Нашлось А" in txt, txt)
txt, err = m.call("toolbox", "web_search_internet", {"query": "python", "category": "??"})
check("web_search_internet: неизвестная категория — понятная ошибка", "ОШИБКА" in txt and "категория" in txt, txt)
_c = sqlite3.connect(db.DB_PATH); web_settings.put(_c, web_settings.SEARXNG_URL, "http://127.0.0.1:9"); _c.close()
txt, err = m.call("toolbox", "web_search_internet", {"query": "python"})
check("SearXNG недоступен — понятная ошибка, адрес читается заново без перезапуска", "ОШИБКА" in txt and "недоступен" in txt, txt)
check("web_search — read-only, спрашивать не надо", by["web_search"].read_only is True and default_policy(by["web_search"]) == "auto")
check("web_extract выходит в сеть — по умолчанию спрашивает", default_policy(by["web_extract"]) == "ask", (by["web_extract"].read_only,))
txt, err = m.call("toolbox", "web_search", {"query": "мамонт"})
check("web_search находит страницу из индекса", not err and "https://docs.test/zoo" in txt and "Зоопарк" in txt, txt)
check("в ответе пометка про внешние данные", "не инструкции" in txt)
txt, err = m.call("toolbox", "web_search", {"query": "квантовая хромодинамика"})
check("web_search: нет совпадений — понятный ответ", not err and "ничего нет" in txt, txt)
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/static"})
check("локальный адрес закрыт по умолчанию", "ОШИБКА" in txt and "непубличный" in txt, txt)
txt, err = m.call("toolbox", "web_extract", {"url": "file:///C:/Windows/win.ini"})
check("file:// закрыт", "ОШИБКА" in txt, txt)
m.shutdown()

m = server(True)
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/static"})
check("web_extract читает страницу", "ФИОЛЕТОВЫЙ-МАМОНТ-42" in txt and "Заголовок: Статика" in txt and f"{base}/spa" in txt, txt[:300])
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/spa"}, timeout=90)
check("JS-страница отрисована автоматически через Qt WebEngine", "ЗЕЛЁНЫЙ-СЛОН-7" in txt and "отрисована" in txt, txt[:300])
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "char_limit": 3000})
import re
check("длинная страница: первый кусок с подсказкой offset", "абзац номер 0" in txt and "абзац номер 2999" not in txt and "Продолжение: web_extract" in txt, txt[-200:])
off = int(re.search(r"offset=(\d+)", txt).group(1))
txt2, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "char_limit": 3000, "offset": off})
check("второй кусок продолжает с нужного места без повтора", f"Показаны символы {off}–" in txt2 and "Ссылки на странице" not in txt2, txt2[-160:])
big, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "char_limit": 500000})
check("огромный char_limit не раздувает ответ сверх лимита роутера", len(big) < 12_000, len(big))
last, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "offset": 10**9})
check("offset за концом — понятная подсказка", "за пределами" in last, last[-120:])
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/nope"})
check("404 — понятная ошибка", "ОШИБКА" in txt and "404" in txt, txt)
m.shutdown()
srv.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
