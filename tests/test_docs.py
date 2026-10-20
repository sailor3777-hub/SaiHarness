"""Чтение PDF / Word / Excel: ядро core.docs и инструмент fs_read через настоящий MCP-сервер toolbox (в т.ч. offset)."""
import pathlib, tempfile
import docx, openpyxl
from pypdf import PdfWriter
from core import db, docs
from core.mcp_manager import McpManager, default_config

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
proj = tmp / "proj"; proj.mkdir()
db.add_root(str(proj))


def make_pdf(path, pages):
    """Минимальный PDF с текстом (Helvetica, только латиница): по странице на элемент pages."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n"); offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out)); out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


# ---------- PDF ----------
make_pdf(proj / "doc.pdf", ["Hello PDF page one", "Second page MAGIC-77"])
t, r = docs.read_document(str(proj / "doc.pdf"))
check("PDF: текст и номера страниц", t and r == "" and "--- стр. 1 из 2 ---" in t and "Hello PDF page one" in t and "MAGIC-77" in t, (t, r))
w = PdfWriter(); w.add_blank_page(200, 200)
with open(proj / "scan.pdf", "wb") as f: w.write(f)
t, r = docs.read_document(str(proj / "scan.pdf"))
check("PDF без текста: понятная причина", t is None and "нет текстового слоя" in r, (t, r))
w = PdfWriter(); w.add_blank_page(200, 200); w.encrypt("secret")
with open(proj / "locked.pdf", "wb") as f: w.write(f)
t, r = docs.read_document(str(proj / "locked.pdf"))
check("PDF с паролем: причина", t is None and "паролем" in r, (t, r))
(proj / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
t, r = docs.read_document(str(proj / "broken.pdf"))
check("битый PDF: причина, без падения", t is None and r, (t, r))

# ---------- Word ----------
d = docx.Document()
d.add_heading("Отчёт о проекте", 1)
d.add_paragraph("Первый абзац с числом 42.")
d.add_heading("Подраздел", 2)
d.add_paragraph("пункт списка", style="List Bullet")
tb = d.add_table(rows=2, cols=2)
tb.cell(0, 0).text = "Имя"; tb.cell(0, 1).text = "Сумма"; tb.cell(1, 0).text = "Аренда"; tb.cell(1, 1).text = "1500"
d.add_paragraph("Заключение ЯНТАРЬ-9.")
d.save(proj / "report.docx")
t, r = docs.read_document(str(proj / "report.docx"))
check("Word: заголовки", t and "# Отчёт о проекте" in t and "## Подраздел" in t, t)
check("Word: абзацы и список", "Первый абзац с числом 42." in t and "- пункт списка" in t, t)
check("Word: таблица строками", "Имя | Сумма" in t and "Аренда | 1500" in t, t)
check("Word: порядок как в документе", t.index("пункт списка") < t.index("Аренда | 1500") < t.index("ЯНТАРЬ-9"), t)
(proj / "fake.docx").write_bytes(b"not a zip")
t, r = docs.read_document(str(proj / "fake.docx"))
check("битый docx: причина", t is None and "Word" in r, (t, r))

# ---------- Excel ----------
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Бюджет"
ws.append(["Статья", "Сумма"]); ws.append(["Аренда", 1500.0]); ws.append([None, None]); ws.append(["Итого", "=B2*2"])
ws2 = wb.create_sheet("Пустой")
wb.create_sheet("Данные").append([1, 2.5, "x\ty"])
wb.save(proj / "book.xlsx")
t, r = docs.read_document(str(proj / "book.xlsx"))
check("Excel: листы и строки", t and "=== Лист «Бюджет» ===" in t and "Статья\tСумма" in t and "Аренда\t1500" in t, t)
check("Excel: пустые строки пропущены, вещественные без «.0»", "\n\n" not in t.split("=== Лист «Данные»")[0].strip() and "1500.0" not in t, t)
check("Excel: формула без сохранённого значения не роняет", "Итого" in t, t)
check("Excel: табуляция в ячейке не ломает колонки", "1\t2.5\tx y" in t, t)
check("Excel: второй лист и пустой лист", "=== Лист «Пустой» ===" in t and "=== Лист «Данные» ===" in t, t)
t, r = docs.read_document(str(proj / "nope.xlsx"))
check("нет файла", t is None and r == "файл не найден", (t, r))
t, r = docs.read_document(str(proj / "x.doc"))
check("старый формат не поддерживается", t is None and "не поддерживается" in r, (t, r))

# ---------- fs_read через MCP ----------
m = McpManager(log_dir=tmp / "logs"); m.connect_all(default_config(db.DB_PATH))
txt, _ = m.call("toolbox", "fs_read", {"path": "doc.pdf"}, 60)
check("fs_read PDF", "MAGIC-77" in txt and "ОШИБКА" not in txt, txt)
txt, _ = m.call("toolbox", "fs_read", {"path": "report.docx"}, 60)
check("fs_read docx", "ЯНТАРЬ-9" in txt, txt)
txt, _ = m.call("toolbox", "fs_read", {"path": "book.xlsx"}, 60)
check("fs_read xlsx", "Аренда\t1500" in txt, txt)
txt, _ = m.call("toolbox", "fs_read", {"path": "scan.pdf"}, 60)
check("fs_read скана: ошибка с причиной", "ОШИБКА" in txt and "текстового слоя" in txt, txt)
txt, _ = m.call("toolbox", "fs_read", {"path": "old.xls"}, 60)
check("fs_read несуществующего .xls: файл не найден", "ОШИБКА" in txt, txt)

# куски через offset
big = "".join(f"строка {i:05d}\n" for i in range(3000))
(proj / "big.txt").write_bytes(big.encode("utf-8"))
got, off, parts = "", 0, 0
while True:
    txt, _ = m.call("toolbox", "fs_read", {"path": "big.txt", "offset": off}, 60)
    parts += 1
    if "[обрезано:" not in txt:
        body = txt.split("\n[показано с символа")[0]
        got += body
        break
    body, tail = txt.rsplit("\n[обрезано:", 1)
    got += body
    off = int(tail.split("offset=")[1].rstrip("]"))
    if parts > 20: break
check("offset: куски склеиваются в исходный текст", got == big and parts == len(big) // 10_500 + 1, (parts, len(got), len(big)))
check("кусок помещается в лимит роутера", all(len(x) < 12_000 for x in [txt]), len(txt))
txt, _ = m.call("toolbox", "fs_read", {"path": "big.txt", "offset": 10_000_000}, 60)
check("offset за концом: ошибка", "ОШИБКА" in txt and "за концом" in txt, txt)
txt, _ = m.call("toolbox", "fs_read", {"path": "big.txt", "max_chars": 50_000}, 60)
check("max_chars больше лимита ужимается", len(txt) < 11_000 and "offset=10500" in txt, len(txt))
m.shutdown()

print("\nALL OK" if ok_all else "\nЕСТЬ ОШИБКИ")
raise SystemExit(0 if ok_all else 1)
