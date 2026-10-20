import json, pathlib, sqlite3, tempfile
import httpx
from core.web.fetch import WebFetcher, FetchError
from core.web.index import WebIndex

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

PUBLIC = lambda host, port, type=0: [(2, 1, 6, "", ("93.184.216.34", port))]
def resolver(table):
    import ipaddress
    def literal(host):
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return None
    return lambda host, port, type=0: [(2, 1, 6, "", (literal(host) or table.get(host, "93.184.216.34"), port))]

def page(title, body, links=()):
    a = "".join(f'<a href="{l}">l</a>' for l in links)
    return f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{body}</p>{a}</main></body></html>"

LONG = " ".join(["подробный текст страницы"] * 20)
SITE = {
    "/": page("Главная", "Добро пожаловать. " + LONG, ["/docs/a", "/docs/b", "/private/x", "/old", "https://other.test/z"]),
    "/docs/a": page("Установка", "Как установить сервер: шаги установки и настройки. " + LONG, ["/docs/b"]),
    "/docs/b": page("Лошади", "Фиолетовый мамонт и лошади живут вместе. " + LONG),
    "/private/x": page("Секрет", "Это закрыто в robots. " + LONG),
    "/old": None,  # редирект на частный адрес
}
hits = []
def handler(request: httpx.Request):
    path = request.url.path
    hits.append(str(request.url))
    if request.url.host == "other.test":
        return httpx.Response(200, text=page("Чужой", LONG))
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    if path == "/sitemap.xml":
        return httpx.Response(404)
    if path == "/old":
        return httpx.Response(302, headers={"location": "http://internal.test/admin"})
    if path == "/spa":
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body><div id=app></div><script>x()</script></body></html>")
    if path == "/img.png":
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    if path in SITE and SITE[path]:
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=SITE[path])
    return httpx.Response(404)

tmp = pathlib.Path(tempfile.mkdtemp())
now = [1000.0]
def mk(**kw):
    args = dict(cache_dir=tmp / "cache", transport=httpx.MockTransport(handler), resolve=resolver({"internal.test": "10.0.0.5"}),
                delay=0, clock=lambda: now[0], sleep=lambda s: None)
    args.update(kw)
    return WebFetcher(**args)

# --- fetch ---
f = mk()
p = f.fetch("https://site.test/docs/a")
check("страница загружена и разобрана", p.title == "Установка" and "установить сервер" in p.text and not p.from_cache, p.text[:80])
n = len(hits)
p2 = f.fetch("https://site.test/docs/a")
check("повтор берётся из кэша без сети", p2.from_cache and len(hits) == n)
now[0] += 3 * 3600
check("кэш устарел по TTL", not f.fetch("https://site.test/docs/a").from_cache)
check("use_cache=False идёт в сеть", (lambda c: (f.fetch("https://site.test/docs/a", use_cache=False), len(hits) > c)[1])(len(hits)))

def fails(url, **kw):
    try:
        mk().fetch(url, **kw); return None
    except FetchError as e:
        return str(e)
e = fails("https://site.test/old")
check("редирект на частный адрес отклонён", e and "непубличный" in e, e)
e = fails("http://127.0.0.1:1234/v1/models")
check("loopback отклонён до запроса", e and "непубличный" in e, e)
check("file:// отклонён", fails("file:///C:/x.txt") is not None)
e = fails("https://site.test/img.png")
check("картинки не читаются", e and "не поддерживается" in e, e)
e = fails("https://site.test/missing")
check("404 — понятная ошибка", e and "404" in e, e)
e = fails("https://site.test/private/x", robots=True)
check("robots.txt соблюдается в режиме обхода", e and "robots" in e, e)
check("без robots=True прямое чтение разрешено", mk().fetch("https://site.test/private/x").title == "Секрет")
e = fails("https://site.test/spa", render=True)
check("render без рендерера — ошибка", e and "недоступна" in e, e)
spa = mk(renderer=lambda url: page("Отрисовано", "Контент из JavaScript. " + LONG)).fetch("https://site.test/spa")
check("JS-страница уходит в рендерер автоматически", spa.rendered and spa.title == "Отрисовано")
PAGES_SMALL = "<html><head><title>Малая</title></head><body><h1>Малая страница</h1><p>" + "короткий текст " * 10 + "</p><script src='/a.js'></script></body></html>"
_calls = []
_small_handler = lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=PAGES_SMALL)
_small = WebFetcher(transport=httpx.MockTransport(_small_handler), resolve=PUBLIC, delay=0, renderer=lambda u: _calls.append(u) or PAGES_SMALL).fetch("https://small.test/", use_cache=False)
check("небольшая настоящая страница со скриптом не уходит в рендерер", not _calls and not _small.rendered and "короткий текст" in _small.text, (_calls, len(_small.text)))
check("без рендерера JS-страница читается как есть", not mk().fetch("https://site.test/spa", use_cache=False).rendered)
check("allow_private открывает локальный адрес", mk(allow_private=True, resolve=PUBLIC).fetch("http://127.0.0.1/docs/a").title == "Установка")
slept = []
mk(delay=2.0, clock=lambda: 50.0, sleep=slept.append).fetch("https://site.test/docs/a", use_cache=False)
f3 = mk(delay=2.0, clock=lambda: 50.0, sleep=slept.append)
f3.fetch("https://site.test/docs/a", use_cache=False); f3.fetch("https://site.test/docs/b", use_cache=False)
check("пауза между запросами к одному узлу", slept and slept[-1] > 0, slept)

# --- index ---
conn = sqlite3.connect(tmp / "idx.db")
idx = WebIndex(conn)
sid = idx.add_source("site.test", depth=2, max_pages=50)
check("источник нормализован", idx.sources()[0]["url"] == "https://site.test/", idx.sources()[0]["url"])
check("повторное добавление не дублирует", idx.add_source("https://site.test/") == sid and len(idx.sources()) == 1)
prog = []
res = idx.crawl(sid, mk(), progress=lambda n, u: prog.append(u))
urls = {r["url"] for r in conn.execute("SELECT url FROM web_pages")}
check("обход собрал страницы сайта", {"https://site.test/", "https://site.test/docs/a", "https://site.test/docs/b"} <= urls, urls)
check("robots.txt исключил закрытое", "https://site.test/private/x" not in urls)
check("чужой домен не обходится", not any("other.test" in u for u in urls) and not any("other.test" in h for h in hits[-30:]))
check("ошибка редиректа не сломала обход", any("/old" in e for e in res["errors"]) and res["pages"] == 3, res)
src = idx.sources()[0]
check("статус источника записан", src["pages"] == 3 and src["crawled_at"] and src["status"].startswith("ошибок"), dict(src))

r = idx.search("установить сервер")
check("поиск находит страницу по словам", r and r[0]["url"].endswith("/docs/a"), r)
check("сниппет есть", r and "установить" in r[0]["snippet"].lower(), r)
r = idx.search("мамонт")
check("поиск по русскому тексту", r and r[0]["title"] == "Лошади", r)
r = idx.search("мамонтов")
check("префиксный поиск не ломает запрос", isinstance(r, list))
r = idx.search("фиолетов*; DROP TABLE web_pages; --")
check("спецсимволы в запросе безопасны", isinstance(r, list) and idx.page_count() == 3)
check("пустой запрос — пусто", idx.search("   ") == [])
check("OR-запасной вариант", idx.search("мамонт несуществующеесловоxyz")[0]["title"] == "Лошади")
check("заголовок весит больше текста", idx.search("Установка")[0]["title"] == "Установка")
check("известный хост", idx.is_known_host("https://www.site.test/x") and not idx.is_known_host("https://evil.test/"))

# повторный обход: страница пропала с сайта — уходит из индекса
SITE["/docs/b"] = None
SITE["/docs/a"] = page("Установка", "Как установить сервер: шаги установки и настройки. " + LONG)
idx.crawl(sid, mk())
urls = {r["url"] for r in conn.execute("SELECT url FROM web_pages")}
check("пропавшая страница удалена из индекса", "https://site.test/docs/b" not in urls and idx.search("мамонт") == [], urls)
stopped = idx.crawl(sid, mk(), should_stop=lambda: True)
check("остановка обхода", stopped["stopped"] and idx.source(sid)["status"] == "остановлено")
idx.remove_source(sid)
check("удаление источника чистит страницы и FTS", idx.page_count() == 0 and idx.search("установить") == [] and idx.sources() == [])

# --- область обхода: путь стартового адреса ---
from core.web.index import scope_prefix, in_scope
check("префикс: каталог без слэша", scope_prefix("https://s.test/3/tutorial") == "/3/tutorial")
check("префикс: каталог со слэшем", scope_prefix("https://s.test/3/tutorial/") == "/3/tutorial")
check("префикс: файл — его папка", scope_prefix("https://s.test/a/b.html") == "/a")
check("префикс: корень — пусто", scope_prefix("https://s.test/") == "")
check("в области: подпуть и сама папка", in_scope("https://s.test/3/tutorial/x.html", "s.test", "/3/tutorial") and in_scope("https://s.test/3/tutorial", "s.test", "/3/tutorial"))
check("вне области: соседние пути", not in_scope("https://s.test/3/tutorial2/x", "s.test", "/3/tutorial") and not in_scope("https://s.test/3.11/", "s.test", "/3/tutorial"))
SITE["/"] = page("Главная", "Начало. " + LONG, ["/docs/a", "/docs/b", "/other"])
SITE["/docs/a"] = page("Установка", "Как установить сервер. " + LONG, ["/docs/b", "/"])
SITE["/docs/b"] = page("Лошади", "Фиолетовый мамонт. " + LONG, ["/other"])
SITE["/other"] = page("Другое", "Вне области. " + LONG)
SITE["/docs"] = page("Документация", "Оглавление. " + LONG, ["/docs/a", "/docs/b", "/", "/other"])
conn2 = sqlite3.connect(tmp / "scope.db"); idx2 = WebIndex(conn2)
sid2 = idx2.add_source("https://site.test/docs", depth=3, max_pages=50)
idx2.crawl(sid2, mk(cache_dir=None))
urls2 = {r["url"] for r in conn2.execute("SELECT url FROM web_pages")}
check("обход не выходит за путь стартового адреса", urls2 == {"https://site.test/docs", "https://site.test/docs/a", "https://site.test/docs/b"}, urls2)

# --- каноничные адреса и пересекающиеся источники ---
from core.web.index import canonical
check("canonical: index.html → папка", canonical("https://s.test/a/index.html") == "https://s.test/a/" and canonical("https://s.test/a/INDEX.HTM") == "https://s.test/a/")
check("canonical: якорь отброшен, запрос не трогается", canonical("https://s.test/a#x") == "https://s.test/a" and canonical("https://s.test/index.html?x=1") == "https://s.test/index.html?x=1")
SITE["/docs"] = page("Документация", "Оглавление. " + LONG, ["/docs/index.html", "/docs/a"])
SITE["/docs/index.html"] = SITE["/docs"]
SITE["/docs/a"] = page("Установка", "Как установить сервер. " + LONG, ["/docs/index.html", "/docs/"])
SITE["/docs/"] = SITE["/docs"]
conn3 = sqlite3.connect(tmp / "overlap.db"); idx3 = WebIndex(conn3)
narrow = idx3.add_source("https://site.test/docs/", depth=2, max_pages=50)
wide = idx3.add_source("https://site.test/", depth=2, max_pages=50)
idx3.crawl(narrow, mk(cache_dir=None))
urls3 = [r["url"] for r in conn3.execute("SELECT url FROM web_pages WHERE source_id=?", (narrow,))]
check("index.html и папка — одна страница", sorted(urls3) == ["https://site.test/docs/", "https://site.test/docs/a"], urls3)
before = conn3.execute("SELECT COUNT(*) FROM web_pages WHERE source_id=?", (narrow,)).fetchone()[0]
idx3.crawl(wide, mk(cache_dir=None))
after = conn3.execute("SELECT COUNT(*) FROM web_pages WHERE source_id=?", (narrow,)).fetchone()[0]
check("широкий обход не отбирает страницы у узкого", before == after == 2, (before, after))
total3 = conn3.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
check("дублей по адресу нет", total3 == conn3.execute("SELECT COUNT(DISTINCT url) FROM web_pages").fetchone()[0])
idx3.remove_source(wide)
check("удаление широкого не трогает страницы узкого", conn3.execute("SELECT COUNT(*) FROM web_pages WHERE source_id=?", (narrow,)).fetchone()[0] == 2)

# --- фрагменты поиска без переносов строк ---
conn4 = sqlite3.connect(tmp / "snip.db"); idx4 = WebIndex(conn4)
s4 = idx4.add_source("https://snip.test/")
conn4.execute("INSERT INTO web_pages(source_id, url, title, text, fetched_at) VALUES(?,?,?,?,?)",
              (s4, "https://snip.test/a", "Т", "Начало\n\n- пункт про мамонтов\n\n- второй пункт\tс табом " + LONG, "2026-01-01"))
conn4.commit()
sn = idx4.search("мамонтов")[0]["snippet"]
check("фрагмент без переносов строк и двойных пробелов", "\n" not in sn and "\t" not in sn and "  " not in sn and "мамонтов" in sn, repr(sn))

print("ALL OK" if ok_all else "SOME FAILED")
