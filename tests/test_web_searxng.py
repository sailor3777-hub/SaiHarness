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
        raise httpx.ConnectError("нет соединения")
    if req.url.host == "slow.lan":
        raise httpx.ReadTimeout("долго")
    if req.url.host == "nojson.lan":
        return httpx.Response(403, text="Forbidden")
    if req.url.host == "html.lan":
        return httpx.Response(200, text="<html>не json</html>", headers={"content-type": "text/html"})
    if req.url.host == "err.lan":
        return httpx.Response(500)
    results = [{"title": f"Результат {i}", "url": f"https://r{i}.test/", "content": "слово  " * 100 if i == 1 else f"описание {i}"} for i in range(1, 8)]
    results.insert(2, {"title": "Без адреса", "content": "x"})
    return httpx.Response(200, json={"query": req.url.params["q"], "results": results})
T = httpx.MockTransport(handler)

r = searxng.search("192.168.0.50:8080/", "python тесты", 3, transport=T)
req = seen[-1]
check("запрос на /search с format=json", req.url.path == "/search" and req.url.params["format"] == "json" and req.url.params["q"] == "python тесты", str(req.url))
check("схема по умолчанию http, слэш убран", str(req.url).startswith("http://192.168.0.50:8080/search"))
check("лимит результатов", len(r) == 3 and r[0]["url"] == "https://r1.test/")
check("записи без адреса пропущены", all(x["url"] for x in searxng.search("x.lan", "q", 20, transport=T)))
check("сниппет обрезан и без лишних пробелов", len(r[0]["snippet"]) <= searxng.SNIPPET_MAX and "  " not in r[0]["snippet"])
check("язык передаётся", searxng.search("x.lan", "q", 1, language="ru", transport=T) and seen[-1].url.params["language"] == "ru")

searxng.search("x.lan", "q", 1, transport=T); check("general: параметр categories не отправляется", "categories" not in seen[-1].url.params)
searxng.search("x.lan", "q", 1, category="it", transport=T); check("категория it передаётся", seen[-1].url.params["categories"] == "it")
try:
    searxng.search("x.lan", "q", 1, category="porn", transport=T); bad_cat = ""
except searxng.SearxError as e:
    bad_cat = str(e)
check("неизвестная категория — ошибка со списком", "it" in bad_cat and "general" in bad_cat, bad_cat)

def err(url):
    try:
        searxng.search(url, "q", transport=T); return ""
    except searxng.SearxError as e:
        return str(e)
check("нет адреса — подсказка", "не задан" in err(""), err(""))
check("сервер недоступен", "недоступен" in err("http://down.lan"), err("http://down.lan"))
check("таймаут", "не ответил" in err("http://slow.lan"), err("http://slow.lan"))
e = err("http://nojson.lan")
check("403 — подсказка про search.formats", "formats" in e and "json" in e, e)
check("не JSON — понятная ошибка", "не JSON" in err("http://html.lan"))
check("500 — код в ошибке", "500" in err("http://err.lan"))
try:
    searxng.normalize_url("ftp://x"); bad = False
except ValueError:
    bad = True
check("не-http адрес отклонён", bad)

# настройки
conn = sqlite3.connect(pathlib.Path(tempfile.mkdtemp()) / "s.db")
check("по умолчанию пусто", ws.get(conn, ws.SEARXNG_URL) == "")
ws.put(conn, ws.SEARXNG_URL, "http://a:1"); check("сохранение", ws.get(conn, ws.SEARXNG_URL) == "http://a:1")
ws.put(conn, ws.SEARXNG_URL, "http://b:2"); check("перезапись", ws.get(conn, ws.SEARXNG_URL) == "http://b:2")
ws.put(conn, ws.SEARXNG_URL, ""); check("пустое значение удаляет настройку", ws.get(conn, ws.SEARXNG_URL, "нет") == "нет")

print("ALL OK" if ok_all else "SOME FAILED")
