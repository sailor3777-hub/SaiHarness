import os, sys, pathlib, tempfile, shutil
from core import db, files, llm
from core.files import Selection, load_context, expand_selection, estimate_tokens, CHECKED, PARTIAL, UNCHECKED
from core.context_builder import build_messages, format_files
db.DB_PATH = pathlib.Path(sys.argv[1])

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = tempfile.mkdtemp()
root = os.path.join(tmp, "proj")
def w(rel, data, mode="w", enc="utf-8"):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    if "b" in mode: open(p, mode).write(data)
    else: open(p, mode, encoding=enc).write(data)
    return p
w("a.txt", "привет, мир\n"); w("sub/b.py", "print('hi')\n"); w("sub/deep/c.md", "# заголовок\n")
w(".git/config", "[core]"); w("node_modules/x.js", "x"); w("img.png", b"\x89PNG\x00\x00", "wb")
w("big.txt", "x" * 400_000); w("bin.dat", b"abc\x00def", "wb"); w("empty.txt", "")
w("cp1251.txt", "Русский текст в кодировке cp1251", enc="cp1251"); w("utf16.txt", "Юникод и 16 бит", enc="utf-16")
P = lambda *p: os.path.join(root, *p)

# --- Selection ---
s = Selection()
check("пусто: не выбрано", s.state(root) == UNCHECKED and s.is_empty())
s.toggle(root, True)
check("папка с галочкой: потомки наследуют", s.decision(P("sub", "b.py")) and s.state(root) == CHECKED and s.state(P("sub")) == CHECKED)
s.toggle(P("sub", "deep"), False)
check("исключение внутри: папка и предки частично", s.state(P("sub", "deep")) == UNCHECKED and s.state(P("sub")) == PARTIAL and s.state(root) == PARTIAL)
check("исключённый узел не входит, соседи входят", not s.decision(P("sub", "deep", "c.md")) and s.decision(P("sub", "b.py")))
s.toggle(P("sub", "deep", "c.md"), True)
check("явный файл внутри исключённой папки входит", s.decision(P("sub", "deep", "c.md")) and s.state(P("sub", "deep")) == PARTIAL)
s.toggle(P("sub"), True)
check("повторная отметка папки сбрасывает исключения внутри", not s.exclude and s.state(P("sub")) == CHECKED)
s.toggle(root, False)
check("снятие корня очищает всё", s.is_empty() and not s.exclude and s.state(root) == UNCHECKED)
s.toggle(P("sub", "b.py"), True)
check("отметка файла: предки частично", s.state(P("sub")) == PARTIAL and s.state(root) == PARTIAL and s.state(P("a.txt")) == UNCHECKED)
s2 = Selection({root + "x", P("a.txt")}, {"C:\\other\\z"}); s2.prune([root])
check("prune убирает пути вне корней (и похожий префикс)", s2.include == {P("a.txt")} and not s2.exclude)

# --- загрузка ---
sel = Selection({root})
b = load_context(sel, [root])
names = [n for n, _ in b.files]
check("состав: текст и разные кодировки", names == ["proj/a.txt", "proj/cp1251.txt", "proj/empty.txt", "proj/img.png", "proj/utf16.txt", "proj/sub/b.py", "proj/sub/deep/c.md"], names)
txt = dict(b.files)
check("cp1251 прочитан правильно", txt["proj/cp1251.txt"] == "Русский текст в кодировке cp1251")
check("utf-16 прочитан правильно", txt["proj/utf16.txt"] == "Юникод и 16 бит")
check("служебные папки и бинарные расширения не берутся", not any(".git" in n or "node_modules" in n for n in names) and "vision_describe" in txt["proj/img.png"] and "img.png" in txt["proj/img.png"])
check("пропущены: слишком большой и бинарный (NUL)", sorted(r for _, r in b.skipped) == sorted(["слишком большой (390 КБ)", "бинарный"]) and len(b.skipped) == 2, b.skipped)
check("не усечено", not b.truncated and b.tokens > 0 and b.chars > 0)
sel.toggle(P("sub"), False)
check("исключённая папка не читается", [n for n, _ in load_context(sel, [root]).files if n.startswith("proj/sub")] == [])
sel.toggle(P("sub", "deep", "c.md"), True)
check("явный файл из исключённой папки читается", "proj/sub/deep/c.md" in [n for n, _ in load_context(sel, [root]).files])
f_only = load_context(Selection({P("a.txt")}), [root])
check("один файл", [n for n, _ in f_only.files] == ["proj/a.txt"])
old = files.MAX_FILES; files.MAX_FILES = 3
tr = load_context(Selection({root}), [root]); files.MAX_FILES = old
check("лимит числа файлов -> truncated", tr.truncated and len(tr.files) <= 3)
old = files.MAX_TOTAL_CHARS; files.MAX_TOTAL_CHARS = 30
tr = load_context(Selection({root}), [root]); files.MAX_TOTAL_CHARS = old
check("лимит объёма -> truncated", tr.truncated and sum(len(t) for _, t in tr.files) <= 30)
check("исчезнувший файл не ломает загрузку", load_context(Selection({P("nope.txt")}), [root]).files == [])
check("estimate: латиница ≈ 5 симв./ток. (подгонка по замерам)", abs(estimate_tokens("a" * 360) - 72) <= 1, estimate_tokens("a" * 360))
check("estimate: кириллица ≈ 3.2 симв./ток. (подгонка по замерам)", abs(estimate_tokens("я" * 200) - 62) <= 1, estimate_tokens("я" * 200))
check("estimate: пусто", estimate_tokens("") == 0)

# --- сборка запроса ---
prof = {"system_prompt": "SYS", "memory": "MEM"}
m = build_messages(prof, [{"role": "user", "content": "q"}], b.files[:1])
sysmsg = m[0]["content"]
check("файлы в system после промпта и памяти", sysmsg.startswith("SYS\n\nПамять:\nMEM\n\nФайлы контекста") and "=== proj/a.txt ===\n```\nпривет, мир\n```" in sysmsg, sysmsg)
check("история после system", [x["role"] for x in m] == ["system", "user"])
check("без файлов блока нет", "Файлы контекста" not in build_messages(prof, [])[0]["content"])
check("файл с ``` оборачивается в ````", "````\na ```x``` b\n````" in format_files([("f", "a ```x``` b")]))
only_files = build_messages({"system_prompt": "", "memory": ""}, [], b.files[:1])
check("только файлы: system без пустых секций", only_files[0]["content"].startswith("Файлы контекста"))

# --- БД ---
db.add_root(root); db.add_root(root)
check("корни: добавление идемпотентно", db.list_roots() == [root])
cid = db.create_chat()
db.save_chat_context(cid, {P("a.txt"), root}, {P("sub")})
inc, exc = db.load_chat_context(cid)
check("выбор чата сохраняется и читается", inc == {P("a.txt"), root} and exc == {P("sub")})
db.save_chat_context(cid, {P("a.txt")}, set())
check("сохранение заменяет прежний выбор", db.load_chat_context(cid) == ({P("a.txt")}, set()))
cid2 = db.create_chat(); db.save_chat_context(cid2, {root}, set())
check("выбор чатов независим", db.load_chat_context(cid) == ({P("a.txt")}, set()) and db.load_chat_context(cid2) == ({root}, set()))
db.delete_chat(cid)
check("выбор удаляется вместе с чатом (каскад)", db.load_chat_context(cid) == (set(), set()) and db.load_chat_context(cid2) == ({root}, set()))
db.remove_root(root)
check("корень удаляется", db.list_roots() == [])

# --- лимит контекста у настоящего LM Studio ---
url = "http://127.0.0.1:1234/v1"
lim, loaded, mx = llm.get_context_info(url, "", "gemma-4-e4b-it-erotica")
check("загруженная модель: рабочий лимит известен", loaded and isinstance(lim, int) and lim > 0 and mx and mx >= lim, (lim, loaded, mx))
print("      gemma-4-e4b-it-erotica: лимит", lim, "максимум", mx)
import httpx
_models = httpx.get("http://127.0.0.1:1234/api/v0/models", timeout=5, trust_env=False).json()["data"]
_unloaded = next(m["id"] for m in _models if m.get("state") != "loaded" and m.get("type") != "embeddings")   # любая выгруженная чат-модель
lim2, loaded2, mx2 = llm.get_context_info(url, "", _unloaded)
check("не загруженная модель: лимит неизвестен, максимум известен", lim2 is None and not loaded2 and mx2, (lim2, loaded2, mx2))
check("неизвестная модель", llm.get_context_info(url, "", "no-such") == (None, False, None))
check("сервер недоступен -> тихий отказ", llm.get_context_info("http://127.0.0.1:1/v1", "", "x") == (None, False, None))

# --- документы (PDF / Word / Excel) идут в контекст текстом ---
import docx as _docx, openpyxl as _xl
from pypdf import PdfWriter as _PdfWriter
from core import docs as _docs
droot = os.path.join(tmp, "docs"); os.makedirs(droot)
D = lambda n: os.path.join(droot, n)
_d = _docx.Document(); _d.add_paragraph("Договор аренды №77"); _d.save(D("contract.docx"))
_wb = _xl.Workbook(); _wb.active.append(["Статья", 100]); _wb.save(D("book.xlsx"))
_w = _PdfWriter(); _w.add_blank_page(100, 100)
with open(D("scan.pdf"), "wb") as _f: _w.write(_f)
dctx = load_context(Selection({D("contract.docx"), D("book.xlsx"), D("scan.pdf")}), [droot])
dt = dict(dctx.files)
check("docx в контексте текстом", "Договор аренды №77" in dt["docs/contract.docx"] and "binary" not in dt["docs/contract.docx"].lower())
check("xlsx в контексте текстом", "Статья	100" in dt["docs/book.xlsx"], dt.get("docs/book.xlsx"))
check("скан пропущен с причиной, а не заглушкой", "docs/scan.pdf" not in dt and any("нет текстового слоя" in r for _, r in dctx.skipped), dctx.skipped)
check("токены документов считаются", dctx.tokens > 0)
folder = load_context(Selection({droot}), [droot])
check("папка с галочкой документы сама не тянет (только явная отметка)", folder.files == [] and folder.skipped == [])
_calls = []
_orig = _docs.read_document
_docs.read_document = lambda path: (_calls.append(path), _orig(path))[1]
_docs._cache.clear()
load_context(Selection({D("contract.docx")}), [droot]); load_context(Selection({D("contract.docx")}), [droot])
check("повторное чтение документа берётся из кеша", len(_calls) == 1, _calls)
_d2 = _docx.Document(); _d2.add_paragraph("Новая версия"); _d2.save(D("contract.docx")); os.utime(D("contract.docx"), (1, 2_000_000_000))
check("изменённый файл читается заново", "Новая версия" in dict(load_context(Selection({D("contract.docx")}), [droot]).files)["docs/contract.docx"] and len(_calls) == 2, _calls)
_docs.read_document = _orig
_old = files.MAX_FILE_BYTES; files.MAX_FILE_BYTES = 5
_big = load_context(Selection({D("book.xlsx")}), [droot]); files.MAX_FILE_BYTES = _old
check("слишком длинный текст документа пропускается", _big.files == [] and "слишком большой текст документа" in _big.skipped[0][1], _big.skipped)

# текстовый файл с редкими NUL (как DumpStack.log Windows: NUL в заголовке и добивка нулями до 12 КБ)
_dump = b"DLOGFILE00010000DUMPC%" + b"\x00" * 10 + b"\nBugCheckCode 0x19C\n" * 100 + b"\x00" * 3000
_t, _r = files.read_text_file(w("DumpStack.log", _dump, "wb"))
check("текст с редкими NUL читается, NUL вырезаны", _r == "" and _t and "BugCheckCode 0x19C" in _t and "\x00" not in _t and _t.startswith("DLOGFILE00010000DUMPC%"), (_t or "")[:40] + _r)
_t, _r = files.read_text_file(w("bin2.dat", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00" * 50, "wb"))
check("настоящий бинарник по-прежнему пропускается", _t is None and _r == "бинарный", _r)

shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK" if ok_all else "SOME FAILED")

