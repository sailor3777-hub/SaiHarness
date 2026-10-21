"""web_search and web_extract through the real toolbox MCP server (a separate process) and a local http server.
The Qt WebEngine renderer runs for real: the test is in the offline suite but takes a few seconds."""
import http.server, json, pathlib, sqlite3, tempfile, threading
from core import db
from core.mcp_manager import default_config
from model_view import McpManager  # errors with the prefix, as the model sees them
from core.tools import default_policy
from core.web.index import WebIndex

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

LONG = " ".join(["detailed page text"] * 20)
PAGES = {
    "/static": f"<html><head><meta charset='utf-8'><title>Static</title></head><body><main><h1>Static</h1><p>Access code: PURPLE-MAMMOTH-42. {LONG}</p><a href='/spa'>next</a></main></body></html>",
    "/spa": "<html><head><meta charset='utf-8'><title>SPA</title></head><body><div id=app></div>"
            "<script>document.getElementById('app').innerHTML='<h1>Drawn by JS</h1><p>JS secret: GREEN-ELEPHANT-7. " + LONG + "</p>';</script></body></html>",
    "/long": "<html><head><meta charset='utf-8'><title>Long</title></head><body><main>" + "".join(f"<p>paragraph number {i}</p>" for i in range(3000)) + "</main></body></html>",
}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/search"):
            payload = json.dumps({"results": [{"title": "Found A", "url": "https://a.test/", "content": "description A"},
                                              {"title": "Found B", "url": "https://b.test/", "content": "description B"}]}, ensure_ascii=False)
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(payload.encode("utf-8")); return
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("content-type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write((body or "none").encode("utf-8"))
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
             (sid, "https://docs.test/zoo", "Zoo", "A purple mammoth and many other animals live in the zoo. " + LONG, "2026-01-01T00:00:00"))
conn.commit(); conn.close()

def server(allow_private):
    cfg = default_config(db.DB_PATH)
    cfg["mcpServers"]["toolbox"]["env"]["SAI_WEB_ALLOW_PRIVATE"] = "1" if allow_private else "0"
    m = McpManager(log_dir=tmp / "logs"); m.connect_all(cfg)
    return m

m = server(False)
by = {t.name: t for t in m.states["toolbox"].tools}
check("web_search_internet runs without asking (the query goes to the user's SearXNG)", default_policy(by["web_search_internet"]) == "auto")
txt, err = m.call("toolbox", "web_search_internet", {"query": "anything"})
check("SearXNG not set up: a hint", "ERROR" in txt and "not set" in txt, txt)
from core.web import settings as web_settings
_c = sqlite3.connect(db.DB_PATH); web_settings.put(_c, web_settings.SEARXNG_URL, base); _c.close()
txt, err = m.call("toolbox", "web_search_internet", {"query": "python", "max_results": 1})
check("web_search_internet: results and the notice", "Found A" in txt and "https://a.test/" in txt and "Found B" not in txt and "not instructions" in txt, txt)
txt, err = m.call("toolbox", "web_search_internet", {"query": "python", "category": "it"})
check("web_search_internet: the category is accepted", "Found A" in txt, txt)
txt, err = m.call("toolbox", "web_search_internet", {"query": "python", "category": "??"})
check("web_search_internet: an unknown category, a clear error", "ERROR" in txt and "category" in txt, txt)
_c = sqlite3.connect(db.DB_PATH); web_settings.put(_c, web_settings.SEARXNG_URL, "http://127.0.0.1:9"); _c.close()
txt, err = m.call("toolbox", "web_search_internet", {"query": "python"})
check("SearXNG unavailable: a clear error, the address is re-read without a restart", "ERROR" in txt and "unavailable" in txt, txt)
check("web_search is read-only, no need to ask", by["web_search"].read_only is True and default_policy(by["web_search"]) == "auto")
check("web_extract goes online, asks by default", default_policy(by["web_extract"]) == "ask", (by["web_extract"].read_only,))
txt, err = m.call("toolbox", "web_search", {"query": "mammoth"})
check("web_search finds the page in the index", not err and "https://docs.test/zoo" in txt and "Zoo" in txt, txt)
check("the reply has the external data notice", "not instructions" in txt)
txt, err = m.call("toolbox", "web_search", {"query": "quantum chromodynamics"})
check("web_search: no matches, a clear reply", not err and "Nothing in the index" in txt, txt)
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/static"})
check("a local address is blocked by default", "ERROR" in txt and "non-public" in txt, txt)
txt, err = m.call("toolbox", "web_extract", {"url": "file:///C:/Windows/win.ini"})
check("file:// is blocked", "ERROR" in txt, txt)
m.shutdown()

m = server(True)
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/static"})
check("web_extract reads the page", "PURPLE-MAMMOTH-42" in txt and "Title: Static" in txt and f"{base}/spa" in txt, txt[:300])
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/spa"}, timeout=90)
check("a JS page is rendered automatically with Qt WebEngine", "GREEN-ELEPHANT-7" in txt and "rendered" in txt, txt[:300])
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "char_limit": 3000})
import re
check("a long page: the first chunk with an offset hint", "paragraph number 0" in txt and "paragraph number 2999" not in txt and "To continue: web_extract" in txt, txt[-200:])
off = int(re.search(r"offset=(\d+)", txt).group(1))
txt2, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "char_limit": 3000, "offset": off})
check("the second chunk continues from the right place without repeats", f"Showing characters {off}–" in txt2 and "Links on the page" not in txt2, txt2[-160:])
big, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "char_limit": 500000})
check("a huge char_limit does not blow the reply past the router's limit", len(big) < 12_000, len(big))
last, err = m.call("toolbox", "web_extract", {"url": f"{base}/long", "offset": 10**9})
check("offset past the end: a clear hint", "is past the text" in last, last[-120:])
txt, err = m.call("toolbox", "web_extract", {"url": f"{base}/nope"})
check("404: a clear error", "ERROR" in txt and "404" in txt, txt)
m.shutdown()
srv.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
