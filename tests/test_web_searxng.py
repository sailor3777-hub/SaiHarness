import json, pathlib, sqlite3, tempfile
import httpx
from core.web import searxng
from core.web import settings as ws

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

seen = []
def handler(req: httpx.Request):
    seen.append(req)
    if req.url.host == "down.lan":
        raise httpx.ConnectError("no connection")
    if req.url.host == "slow.lan":
        raise httpx.ReadTimeout("slow")
    if req.url.host == "nojson.lan":
        return httpx.Response(403, text="Forbidden")
    if req.url.host == "html.lan":
        return httpx.Response(200, text="<html>not json</html>", headers={"content-type": "text/html"})
    if req.url.host == "err.lan":
        return httpx.Response(500)
    results = [{"title": f"Result {i}", "url": f"https://r{i}.test/", "content": "word  " * 100 if i == 1 else f"description {i}"} for i in range(1, 8)]
    results.insert(2, {"title": "No address", "content": "x"})
    return httpx.Response(200, json={"query": req.url.params["q"], "results": results})
T = httpx.MockTransport(handler)

r = searxng.search("192.168.0.50:8080/", "python tests", 3, transport=T)
req = seen[-1]
check("a request to /search with format=json", req.url.path == "/search" and req.url.params["format"] == "json" and req.url.params["q"] == "python tests", str(req.url))
check("the default scheme is http, the slash is removed", str(req.url).startswith("http://192.168.0.50:8080/search"))
check("the result limit", len(r) == 3 and r[0]["url"] == "https://r1.test/")
check("entries without an address are skipped", all(x["url"] for x in searxng.search("x.lan", "q", 20, transport=T)))
check("the snippet is cut and has no extra spaces", len(r[0]["snippet"]) <= searxng.SNIPPET_MAX and "  " not in r[0]["snippet"])
check("the language is passed", searxng.search("x.lan", "q", 1, language="ru", transport=T) and seen[-1].url.params["language"] == "ru")

searxng.search("x.lan", "q", 1, transport=T); check("general: the categories parameter is not sent", "categories" not in seen[-1].url.params)
searxng.search("x.lan", "q", 1, category="it", transport=T); check("the it category is passed", seen[-1].url.params["categories"] == "it")
try:
    searxng.search("x.lan", "q", 1, category="porn", transport=T); bad_cat = ""
except searxng.SearxError as e:
    bad_cat = str(e)
check("an unknown category: an error with the list", "it" in bad_cat and "general" in bad_cat, bad_cat)

def err(url):
    try:
        searxng.search(url, "q", transport=T); return ""
    except searxng.SearxError as e:
        return str(e)
check("no address: a hint", "not set" in err(""), err(""))
check("the server is unavailable", "unavailable" in err("http://down.lan"), err("http://down.lan"))
check("timeout", "did not answer" in err("http://slow.lan"), err("http://slow.lan"))
e = err("http://nojson.lan")
check("403: a hint about search.formats", "formats" in e and "json" in e, e)
check("not JSON: a clear error", "did not return JSON" in err("http://html.lan"))
check("500: the code in the error", "500" in err("http://err.lan"))
try:
    searxng.normalize_url("ftp://x"); bad = False
except ValueError:
    bad = True
check("a non-http address is refused", bad)

# settings
conn = sqlite3.connect(pathlib.Path(tempfile.mkdtemp()) / "s.db")
check("empty by default", ws.get(conn, ws.SEARXNG_URL) == "")
ws.put(conn, ws.SEARXNG_URL, "http://a:1"); check("saving", ws.get(conn, ws.SEARXNG_URL) == "http://a:1")
ws.put(conn, ws.SEARXNG_URL, "http://b:2"); check("overwriting", ws.get(conn, ws.SEARXNG_URL) == "http://b:2")
ws.put(conn, ws.SEARXNG_URL, ""); check("an empty value deletes the setting", ws.get(conn, ws.SEARXNG_URL, "none") == "none")

print("ALL OK" if ok_all else "SOME FAILED")
