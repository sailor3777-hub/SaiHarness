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

LONG = " ".join(["detailed page text"] * 20)
SITE = {
    "/": page("Home", "Welcome. " + LONG, ["/docs/a", "/docs/b", "/private/x", "/old", "https://other.test/z"]),
    "/docs/a": page("Installation", "How to install the server: installation and setup steps. " + LONG, ["/docs/b"]),
    "/docs/b": page("Horses", "The purple mammoth and horses live together. " + LONG),
    "/private/x": page("Secret", "This is closed in robots. " + LONG),
    "/old": None,  # redirect to a private address
}
hits = []
def handler(request: httpx.Request):
    path = request.url.path
    hits.append(str(request.url))
    if request.url.host == "other.test":
        return httpx.Response(200, text=page("Foreign", LONG))
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
check("the page is loaded and parsed", p.title == "Installation" and "install the server" in p.text and not p.from_cache, p.text[:80])
n = len(hits)
p2 = f.fetch("https://site.test/docs/a")
check("a repeat comes from the cache without the network", p2.from_cache and len(hits) == n)
now[0] += 3 * 3600
check("the cache expired by TTL", not f.fetch("https://site.test/docs/a").from_cache)
check("use_cache=False goes to the network", (lambda c: (f.fetch("https://site.test/docs/a", use_cache=False), len(hits) > c)[1])(len(hits)))

def fails(url, **kw):
    try:
        mk().fetch(url, **kw); return None
    except FetchError as e:
        return str(e)
e = fails("https://site.test/old")
check("a redirect to a private address is refused", e and "non-public" in e, e)
e = fails("http://127.0.0.1:1234/v1/models")
check("loopback is refused before the request", e and "non-public" in e, e)
check("file:// is refused", fails("file:///C:/x.txt") is not None)
e = fails("https://site.test/img.png")
check("images are not read", e and "not supported" in e, e)
e = fails("https://site.test/missing")
check("404 is a clear error", e and "404" in e, e)
e = fails("https://site.test/private/x", robots=True)
check("robots.txt is respected in crawl mode", e and "robots" in e, e)
check("without robots=True direct reading is allowed", mk().fetch("https://site.test/private/x").title == "Secret")
e = fails("https://site.test/spa", render=True)
check("render without a renderer is an error", e and "unavailable" in e, e)
spa = mk(renderer=lambda url: page("Rendered", "Content from JavaScript. " + LONG)).fetch("https://site.test/spa")
check("a JS page goes to the renderer automatically", spa.rendered and spa.title == "Rendered")
PAGES_SMALL = "<html><head><title>Small</title></head><body><h1>Small page</h1><p>" + "short text " * 10 + "</p><script src='/a.js'></script></body></html>"
_calls = []
_small_handler = lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=PAGES_SMALL)
_small = WebFetcher(transport=httpx.MockTransport(_small_handler), resolve=PUBLIC, delay=0, renderer=lambda u: _calls.append(u) or PAGES_SMALL).fetch("https://small.test/", use_cache=False)
check("a small real page with a script does not go to the renderer", not _calls and not _small.rendered and "short text" in _small.text, (_calls, len(_small.text)))
check("without a renderer a JS page is read as is", not mk().fetch("https://site.test/spa", use_cache=False).rendered)
check("allow_private opens a local address", mk(allow_private=True, resolve=PUBLIC).fetch("http://127.0.0.1/docs/a").title == "Installation")
slept = []
mk(delay=2.0, clock=lambda: 50.0, sleep=slept.append).fetch("https://site.test/docs/a", use_cache=False)
f3 = mk(delay=2.0, clock=lambda: 50.0, sleep=slept.append)
f3.fetch("https://site.test/docs/a", use_cache=False); f3.fetch("https://site.test/docs/b", use_cache=False)
check("a pause between requests to one host", slept and slept[-1] > 0, slept)

# --- index ---
conn = sqlite3.connect(tmp / "idx.db")
idx = WebIndex(conn)
sid = idx.add_source("site.test", depth=2, max_pages=50)
check("the source is normalized", idx.sources()[0]["url"] == "https://site.test/", idx.sources()[0]["url"])
check("adding again does not duplicate", idx.add_source("https://site.test/") == sid and len(idx.sources()) == 1)
prog = []
res = idx.crawl(sid, mk(), progress=lambda n, u: prog.append(u))
urls = {r["url"] for r in conn.execute("SELECT url FROM web_pages")}
check("the crawl collected the site's pages", {"https://site.test/", "https://site.test/docs/a", "https://site.test/docs/b"} <= urls, urls)
check("robots.txt excluded the closed part", "https://site.test/private/x" not in urls)
check("a foreign domain is not crawled", not any("other.test" in u for u in urls) and not any("other.test" in h for h in hits[-30:]))
check("a redirect error did not break the crawl", any("/old" in e for e in res["errors"]) and res["pages"] == 3, res)
src = idx.sources()[0]
check("the source status is recorded", src["pages"] == 3 and src["crawled_at"] and src["status"].startswith("errors"), dict(src))

r = idx.search("install server")
check("search finds the page by its words", r and r[0]["url"].endswith("/docs/a"), r)
check("there is a snippet", r and "install" in r[0]["snippet"].lower(), r)
r = idx.search("mammoth")
check("search in the page text", r and r[0]["title"] == "Horses", r)
r = idx.search("mammoths")
check("prefix search does not break the query", isinstance(r, list))
r = idx.search("purpl*; DROP TABLE web_pages; --")
check("special characters in the query are safe", isinstance(r, list) and idx.page_count() == 3)
check("an empty query gives nothing", idx.search("   ") == [])
check("the OR fallback", idx.search("mammoth nonexistentwordxyz")[0]["title"] == "Horses")
check("the title weighs more than the text", idx.search("Installation")[0]["title"] == "Installation")
check("a known host", idx.is_known_host("https://www.site.test/x") and not idx.is_known_host("https://evil.test/"))

# crawling again: a page disappeared from the site and leaves the index
SITE["/docs/b"] = None
SITE["/docs/a"] = page("Installation", "How to install the server: installation and setup steps. " + LONG)
idx.crawl(sid, mk())
urls = {r["url"] for r in conn.execute("SELECT url FROM web_pages")}
check("the vanished page is removed from the index", "https://site.test/docs/b" not in urls and idx.search("mammoth") == [], urls)
stopped = idx.crawl(sid, mk(), should_stop=lambda: True)
check("stopping the crawl", stopped["stopped"] and idx.source(sid)["status"] == "stopped")
idx.remove_source(sid)
check("deleting a source clears its pages and FTS", idx.page_count() == 0 and idx.search("install") == [] and idx.sources() == [])

# --- crawl scope: the path of the start address ---
from core.web.index import scope_prefix, in_scope
check("prefix: a folder without a slash", scope_prefix("https://s.test/3/tutorial") == "/3/tutorial")
check("prefix: a folder with a slash", scope_prefix("https://s.test/3/tutorial/") == "/3/tutorial")
check("prefix: a file gives its folder", scope_prefix("https://s.test/a/b.html") == "/a")
check("prefix: the root gives empty", scope_prefix("https://s.test/") == "")
check("in scope: a subpath and the folder itself", in_scope("https://s.test/3/tutorial/x.html", "s.test", "/3/tutorial") and in_scope("https://s.test/3/tutorial", "s.test", "/3/tutorial"))
check("out of scope: neighboring paths", not in_scope("https://s.test/3/tutorial2/x", "s.test", "/3/tutorial") and not in_scope("https://s.test/3.11/", "s.test", "/3/tutorial"))
SITE["/"] = page("Home", "Start. " + LONG, ["/docs/a", "/docs/b", "/other"])
SITE["/docs/a"] = page("Installation", "How to install the server. " + LONG, ["/docs/b", "/"])
SITE["/docs/b"] = page("Horses", "The purple mammoth. " + LONG, ["/other"])
SITE["/other"] = page("Other", "Out of scope. " + LONG)
SITE["/docs"] = page("Documentation", "Contents. " + LONG, ["/docs/a", "/docs/b", "/", "/other"])
conn2 = sqlite3.connect(tmp / "scope.db"); idx2 = WebIndex(conn2)
sid2 = idx2.add_source("https://site.test/docs", depth=3, max_pages=50)
idx2.crawl(sid2, mk(cache_dir=None))
urls2 = {r["url"] for r in conn2.execute("SELECT url FROM web_pages")}
check("the crawl does not go beyond the start address path", urls2 == {"https://site.test/docs", "https://site.test/docs/a", "https://site.test/docs/b"}, urls2)

# --- canonical addresses and overlapping sources ---
from core.web.index import canonical
check("canonical: index.html → folder", canonical("https://s.test/a/index.html") == "https://s.test/a/" and canonical("https://s.test/a/INDEX.HTM") == "https://s.test/a/")
check("canonical: the anchor is dropped, the query is kept", canonical("https://s.test/a#x") == "https://s.test/a" and canonical("https://s.test/index.html?x=1") == "https://s.test/index.html?x=1")
SITE["/docs"] = page("Documentation", "Contents. " + LONG, ["/docs/index.html", "/docs/a"])
SITE["/docs/index.html"] = SITE["/docs"]
SITE["/docs/a"] = page("Installation", "How to install the server. " + LONG, ["/docs/index.html", "/docs/"])
SITE["/docs/"] = SITE["/docs"]
conn3 = sqlite3.connect(tmp / "overlap.db"); idx3 = WebIndex(conn3)
narrow = idx3.add_source("https://site.test/docs/", depth=2, max_pages=50)
wide = idx3.add_source("https://site.test/", depth=2, max_pages=50)
idx3.crawl(narrow, mk(cache_dir=None))
urls3 = [r["url"] for r in conn3.execute("SELECT url FROM web_pages WHERE source_id=?", (narrow,))]
check("index.html and the folder are one page", sorted(urls3) == ["https://site.test/docs/", "https://site.test/docs/a"], urls3)
before = conn3.execute("SELECT COUNT(*) FROM web_pages WHERE source_id=?", (narrow,)).fetchone()[0]
idx3.crawl(wide, mk(cache_dir=None))
after = conn3.execute("SELECT COUNT(*) FROM web_pages WHERE source_id=?", (narrow,)).fetchone()[0]
check("a wide crawl does not take pages from a narrow one", before == after == 2, (before, after))
total3 = conn3.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
check("no duplicates by address", total3 == conn3.execute("SELECT COUNT(DISTINCT url) FROM web_pages").fetchone()[0])
idx3.remove_source(wide)
check("deleting the wide one does not touch the narrow one's pages", conn3.execute("SELECT COUNT(*) FROM web_pages WHERE source_id=?", (narrow,)).fetchone()[0] == 2)

# --- search snippets without line breaks ---
conn4 = sqlite3.connect(tmp / "snip.db"); idx4 = WebIndex(conn4)
s4 = idx4.add_source("https://snip.test/")
conn4.execute("INSERT INTO web_pages(source_id, url, title, text, fetched_at) VALUES(?,?,?,?,?)",
              (s4, "https://snip.test/a", "T", "Start\n\n- an item about mammoths\n\n- a second item\twith a tab " + LONG, "2026-01-01"))
conn4.commit()
sn = idx4.search("mammoths")[0]["snippet"]
check("a snippet without line breaks and double spaces", "\n" not in sn and "\t" not in sn and "  " not in sn and "mammoths" in sn, repr(sn))

print("ALL OK" if ok_all else "SOME FAILED")
