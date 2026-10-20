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
check("публичный адрес проходит", check_url("https://example.com/a?b=1", resolve=fake("93.184.216.34")).startswith("https://example.com"))
for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "172.16.0.1", "::1", "100.64.0.1", "::ffff:127.0.0.1"):
    check(f"закрыт {ip}", bad("http://h.example/", resolve=fake(ip)), ip)
check("file:// закрыт", bad("file:///C:/Windows/win.ini"))
check("ftp:// закрыт", bad("ftp://example.com/"))
check("логин в адресе закрыт", bad("http://u:p@example.com/", resolve=fake("93.184.216.34")))
check("localhost закрыт (реальный DNS)", bad("http://localhost:1234/"))
check("блок-лист и поддомены", bad("https://a.evil.com/", blocklist=["evil.com"], resolve=fake("93.184.216.34")))
check("allow_private открывает loopback", check_url("http://127.0.0.1:8000/", allow_private=True))
check("пустой узел", bad("http:///x"))

# --- extract ---
html = """<html><head><title> Тест  страница </title><style>p{color:red}</style><script>var x=1;</script></head>
<body><nav>Меню Меню</nav><header>Шапка</header>
<main><h1>Заголовок</h1><p>Первый абзац с <a href="/doc/2#frag">ссылкой</a> и текстом.</p>
<ul><li>один</li><li>два</li></ul><pre>строка1
  строка2</pre>%s</main><footer>Подвал</footer></body></html>""" % ("<p>" + "слово " * 60 + "</p>")
p = extract(html, "https://site.test/doc/1")
check("заголовок страницы", p.title == "Тест страница", p.title)
check("скрипты и стили убраны", "var x" not in p.text and "color:red" not in p.text)
check("навигация и подвал убраны", "Меню" not in p.text and "Подвал" not in p.text and "Шапка" not in p.text)
check("структура сохранена", "# Заголовок" in p.text and "- один" in p.text and "- два" in p.text, p.text[:120])
check("pre сохраняет отступы", "  строка2" in p.text)
check("ссылка абсолютная и без якоря", p.links == ["https://site.test/doc/2"], p.links)
short = extract("<html><body><p>коротко</p><div>ещё</div></body></html>")
check("без main берётся весь body", "коротко" in short.text and "ещё" in short.text)

# --- навигация по классам, ролям и id (Sphinx, MediaWiki) ---
body = " ".join(["Основной текст статьи про списки."] * 12)
sphinx = f"""<html><head><title>Док</title></head><body>
<div class="related" role="navigation"><h3>Navigation</h3><ul><li><a href="/index.html">index</a></li><li><a href="/modules.html">modules</a></li></ul></div>
<div class="sphinxsidebar"><div><h3>Table of Contents</h3><ul><li><a href="/chapter2.html">Глава 2</a></li></ul></div></div>
<div class="body" role="main"><h1>Глава 1</h1><p>{body}</p><p><a href="/chapter3.html">дальше</a></p>
<span class="mw-editsection">[edit]</span></div>
<div id="footer"><a href="/copyright.html">Copyright</a> © 2026</div>
<div class="cookie-banner">Мы используем cookie</div>
<div role="banner"><a href="/home">Домой</a>Шапка сайта</div></body></html>"""
sp = extract(sphinx, "https://d.test/a/")
check("sphinx: навигация и сайдбар не в тексте", "Navigation" not in sp.text and "Table of Contents" not in sp.text and "index" not in sp.text, sp.text[:80])
check("sphinx: основной текст на месте", sp.text.startswith("# Глава 1") and "Основной текст" in sp.text, sp.text[:80])
check("мусор по классам, id и ролям убран", "[edit]" not in sp.text and "Copyright" not in sp.text and "cookie" not in sp.text and "Шапка" not in sp.text)
check("ссылки из навигации собраны (для краулера)", {"https://d.test/index.html", "https://d.test/modules.html", "https://d.test/chapter2.html", "https://d.test/copyright.html", "https://d.test/home"} <= set(sp.links), sp.links)
check("ссылки из содержимого идут первыми", sp.links[0] == "https://d.test/chapter3.html", sp.links[:2])
nested = extract(f"<html><body><div class='sidebar'><div><div>внутри</div></div></div><p>{body}</p><div>после</div></body></html>")
check("вложенные div внутри блока навигации закрываются верно", "внутри" not in nested.text and "после" in nested.text and "Основной" in nested.text, nested.text)
nav = extract(f"<html><body><nav class='sidebar'><a href='/x'>x</a>меню</nav><p>{body}</p></body></html>", "https://d.test/")
check("<nav> с классом не ломает счётчики", "меню" not in nav.text and "Основной" in nav.text and nav.links == ["https://d.test/x"], (nav.text[:40], nav.links))
check("скрипты по-прежнему без ссылок", extract("<script><a href='/s'>x</a></script><p>t</p>").links == [])

# --- paginate ---
t = "\n".join(f"строка {i}" for i in range(2000))
check("короткий текст отдаётся целиком, без подписи", page_slice("abc", 0, 1000) == "abc")
first = page_slice(t, 0, 1000)
check("первый кусок: начало и подсказка про offset", first.startswith("строка 0") and "Показаны символы 0–" in first and "offset=" in first, first[-120:])
check("кусок укладывается в лимит с запасом на подпись", len(first) < 1000 + 150, len(first))
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
    lines += c.split("\n\n[Показаны")[0].split("\n")
check("постраничное чтение собирает весь текст без потерь и повторов", lines == t.split("\n"), (len(parts), len(lines), len(t.split("\n"))))
check("последний кусок помечен как конец", "Это конец страницы" in parts[-1])
check("offset за пределами — понятное сообщение", "за пределами" in page_slice(t, 10**9, 1000))
check("кривой offset не падает", page_slice(t, "abc", 1000).startswith("строка 0") and page_slice(t, -5, 1000).startswith("строка 0"))
check("лимит ограничен снизу и сверху", clamp_limit(5) == 1000 and clamp_limit(10**6) == 10500 and clamp_limit("x") == 9000)

# --- пустые пункты списка ---
items = extract("<ul><li></li><li><a href='x'></a></li><li>один</li><li></li></ul><p>" + ("слово " * 60) + "</p>").text.split("\n")
check("пустые пункты списка не попадают в текст", "-" not in items, items[:4])
check("настоящие пункты остаются", "- один" in items)

print("ALL OK" if ok_all else "SOME FAILED")
