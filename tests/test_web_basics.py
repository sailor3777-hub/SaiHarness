import socket
from core.web.safety import check_url, UnsafeUrl
from core.web.extract import extract
from core.web.paginate import clamp_limit, page_slice
import pathlib, tempfile

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

def fake(ip):
    return lambda host, port, type=0: [(2, 1, 6, "", (ip, port))]

def bad(url, **kw):
    try:
        check_url(url, **kw); return False
    except UnsafeUrl:
        return True

# --- safety ---
check("a public address passes", check_url("https://example.com/a?b=1", resolve=fake("93.184.216.34")).startswith("https://example.com"))
for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "172.16.0.1", "::1", "100.64.0.1", "::ffff:127.0.0.1"):
    check(f"blocked {ip}", bad("http://h.example/", resolve=fake(ip)), ip)
check("file:// is blocked", bad("file:///C:/Windows/win.ini"))
check("ftp:// is blocked", bad("ftp://example.com/"))
check("a login in the address is blocked", bad("http://u:p@example.com/", resolve=fake("93.184.216.34")))
check("localhost is blocked (real DNS)", bad("http://localhost:1234/"))
check("block list and subdomains", bad("https://a.evil.com/", blocklist=["evil.com"], resolve=fake("93.184.216.34")))
check("allow_private opens loopback", check_url("http://127.0.0.1:8000/", allow_private=True))
check("an empty host", bad("http:///x"))

# --- extract ---
html = """<html><head><title> Test  page </title><style>p{color:red}</style><script>var x=1;</script></head>
<body><nav>Menu Menu</nav><header>Masthead</header>
<main><h1>Heading</h1><p>The first paragraph with a <a href="/doc/2#frag">link</a> and text.</p>
<ul><li>one</li><li>two</li></ul><pre>line1
  line2</pre>%s</main><footer>Footer</footer></body></html>""" % ("<p>" + "word " * 60 + "</p>")
p = extract(html, "https://site.test/doc/1")
check("page title", p.title == "Test page", p.title)
check("scripts and styles removed", "var x" not in p.text and "color:red" not in p.text)
check("navigation and footer removed", "Menu" not in p.text and "Footer" not in p.text and "Masthead" not in p.text)
check("structure kept", "# Heading" in p.text and "- one" in p.text and "- two" in p.text, p.text[:120])
check("pre keeps indentation", "  line2" in p.text)
check("the link is absolute and without an anchor", p.links == ["https://site.test/doc/2"], p.links)
short = extract("<html><body><p>briefly</p><div>more</div></body></html>")
check("without main the whole body is taken", "briefly" in short.text and "more" in short.text)

# --- navigation by class, role and id (Sphinx, MediaWiki) ---
body = " ".join(["The main text of an article about lists."] * 12)
sphinx = f"""<html><head><title>Doc</title></head><body>
<div class="related" role="navigation"><h3>Navigation</h3><ul><li><a href="/index.html">index</a></li><li><a href="/modules.html">modules</a></li></ul></div>
<div class="sphinxsidebar"><div><h3>Table of Contents</h3><ul><li><a href="/chapter2.html">Chapter 2</a></li></ul></div></div>
<div class="body" role="main"><h1>Chapter 1</h1><p>{body}</p><p><a href="/chapter3.html">next</a></p>
<span class="mw-editsection">[edit]</span></div>
<div id="footer"><a href="/copyright.html">Copyright</a> © 2026</div>
<div class="cookie-banner">We use cookies</div>
<div role="banner"><a href="/home">Home</a>Site masthead</div></body></html>"""
sp = extract(sphinx, "https://d.test/a/")
check("sphinx: navigation and sidebar are not in the text", "Navigation" not in sp.text and "Table of Contents" not in sp.text and "index" not in sp.text, sp.text[:80])
check("sphinx: the main text is in place", sp.text.startswith("# Chapter 1") and "The main text" in sp.text, sp.text[:80])
check("junk by class, id and role removed", "[edit]" not in sp.text and "Copyright" not in sp.text and "cookie" not in sp.text and "masthead" not in sp.text)
check("links from navigation are collected (for the crawler)", {"https://d.test/index.html", "https://d.test/modules.html", "https://d.test/chapter2.html", "https://d.test/copyright.html", "https://d.test/home"} <= set(sp.links), sp.links)
check("links from the content come first", sp.links[0] == "https://d.test/chapter3.html", sp.links[:2])
nested = extract(f"<html><body><div class='sidebar'><div><div>inside</div></div></div><p>{body}</p><div>after</div></body></html>")
check("nested divs inside a navigation block are closed correctly", "inside" not in nested.text and "after" in nested.text and "The main" in nested.text, nested.text)
nav = extract(f"<html><body><nav class='sidebar'><a href='/x'>x</a>menu</nav><p>{body}</p></body></html>", "https://d.test/")
check("<nav> with a class does not break the counters", "menu" not in nav.text and "The main" in nav.text and nav.links == ["https://d.test/x"], (nav.text[:40], nav.links))
check("scripts still give no links", extract("<script><a href='/s'>x</a></script><p>t</p>").links == [])

# --- paginate ---
t = "\n".join(f"line {i}" for i in range(2000))
check("a short text is returned whole, without a caption", page_slice("abc", 0, 1000) == "abc")
first = page_slice(t, 0, 1000)
check("the first chunk: the start and an offset hint", first.startswith("line 0") and "Showing characters 0–" in first and "offset=" in first, first[-120:])
check("the chunk fits the limit with room for the caption", len(first) < 1000 + 150, len(first))
import re
pos, parts = 0, []
for _ in range(100):
    chunk = page_slice(t, pos, 1000)
    parts.append(chunk)
    m = re.search(r"offset=(\d+)", chunk)
    if not m:
        break
    pos = int(m.group(1))
lines = []
for c in parts:
    lines += c.split("\n\n[Showing")[0].split("\n")
check("paged reading collects the whole text without loss or repeats", lines == t.split("\n"), (len(parts), len(lines), len(t.split("\n"))))
check("the last chunk is marked as the end", "This is the end of the page" in parts[-1])
check("an offset past the end gives a clear message", "is past the text" in page_slice(t, 10**9, 1000))
check("a bad offset does not crash", page_slice(t, "abc", 1000).startswith("line 0") and page_slice(t, -5, 1000).startswith("line 0"))
check("the limit is clamped from below and above", clamp_limit(5) == 1000 and clamp_limit(10**6) == 10500 and clamp_limit("x") == 9000)

# --- empty list items ---
items = extract("<ul><li></li><li><a href='x'></a></li><li>one</li><li></li></ul><p>" + ("word " * 60) + "</p>").text.split("\n")
check("empty list items do not get into the text", "-" not in items, items[:4])
check("real items remain", "- one" in items)

print("ALL OK" if ok_all else "SOME FAILED")
