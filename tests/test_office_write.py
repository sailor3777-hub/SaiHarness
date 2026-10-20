"""docx_write / xlsx_write на реальном MCP-сервере: содержимое, перезапись, отказы; чтение результата через core.docs."""
import sys, pathlib, tempfile
from core import db, docs
from core.mcp_manager import McpManager, load_config

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "office.db"
root = tmp / "proj"; root.mkdir(); db.add_root(str(root))
m = McpManager(); m.connect_all(load_config(tmp / "mcp.json", db.DB_PATH))
def call(tool, **a): return m.call("toolbox", tool, a, 60)[0]
tools = {t.name: t for t in m.all_tools()}
check("оба инструмента пишущие (спросят)", not tools["docx_write"].read_only and not tools["xlsx_write"].read_only)

MD = """# Отчёт

Первый абзац с **жирным**, *курсивом* и `кодом`, ссылка [сайт](https://example.com).

## Список

- пункт один
- пункт два
  - вложенный
1. первый
2. второй

| Имя | Сумма |
|-----|-------|
| Аня | 10 |
| Борис | 20 |

> цитата

```
print("код")
```
"""
d = root / "report.docx"
t = call("docx_write", path=str(d), markdown=MD)
check("docx создан", t.startswith("Готово") and d.exists() and d.stat().st_size > 2000, t)
text, reason = docs.read_document(str(d))
check("читается обратно через docs", text is not None and reason == "", reason)
check("заголовки как заголовки", "# Отчёт" in text and "## Список" in text, text)
check("абзац и ссылка", "жирным" in text and "курсивом" in text and "https://example.com" in text, text)
check("списки", "пункт один" in text and "вложенный" in text and "второй" in text, text)
check("таблица строками", "Имя | Сумма" in text and "Аня | 10" in text and "Борис | 20" in text, text)
check("цитата и код", "цитата" in text and 'print("код")' in text, text)
import docx as _docx
dd = _docx.Document(str(d))
check("жирный реально жирный", any(r.bold and r.text == "жирным" for p in dd.paragraphs for r in p.runs))
check("таблица как таблица 3x2", len(dd.tables) == 1 and len(dd.tables[0].rows) == 3 and len(dd.tables[0].columns) == 2)
check("временных файлов не осталось", [x.name for x in root.iterdir() if x.name.endswith(".sai-tmp")] == [])

# ---- защита от затирания ----
before = d.read_bytes()
t = call("docx_write", path=str(d), markdown="# Новый")
check("существующий файл не затирается", t.startswith("ОШИБКА") and "overwrite" in t and d.read_bytes() == before, t)
t = call("docx_write", path=str(d), markdown="# Новый", overwrite=True)
bak = root / "report.docx.bak"
check("overwrite=true: заменён, старый в .bak", t.startswith("Готово") and "Копия прежнего" in t and bak.exists() and bak.read_bytes() == before, t)
check("новое содержимое", "Новый" in docs.read_document(str(d))[0])
t = call("docx_write", path=str(d), markdown="# Ещё", overwrite=True)
check("вторая копия не затирает первую (.bak.1)", (root / "report.docx.bak.1").exists() and bak.read_bytes() == before, t)

# ---- отказы ----
check("не .docx", call("docx_write", path=str(root / "a.txt"), markdown="x").startswith("ОШИБКА"))
check("пустой markdown", "пуст" in call("docx_write", path=str(root / "e.docx"), markdown="  "))
(root / "dir.docx").mkdir(); check("папка вместо файла", "папка" in call("docx_write", path=str(root / "dir.docx"), markdown="x"))
rel = call("docx_write", path="rel.docx", markdown="# Относительный")
check("относительный путь: от первой разрешённой папки", rel.startswith("Готово") and (root / "rel.docx").exists(), rel)
sub = call("docx_write", path=str(root / "new" / "deep" / "x.docx"), markdown="# В новой папке")
check("недостающие папки создаются", sub.startswith("Готово") and (root / "new" / "deep" / "x.docx").exists(), sub)

# ---- Excel ----
x = root / "tab.xlsx"
t = call("xlsx_write", path=str(x), sheets={"Данные": [["Имя", "Сумма"], ["Аня", 10], ["Борис", 20.5], ["Итого", "=SUM(B2:B3)"]],
                                            "Пусто": [], "Флаги": [[True, None, "текст"]]})
check("xlsx создан", t.startswith("Готово") and "листов: 3" in t and x.exists(), t)
text, reason = docs.read_document(str(x))
check("читается обратно через docs", text is not None and "Аня" in text and "Борис" in text and "20.5" in text, reason or text)
import openpyxl
wb = openpyxl.load_workbook(str(x))
check("листы и имена", wb.sheetnames == ["Данные", "Пусто", "Флаги"], wb.sheetnames)
check("формула записана формулой", wb["Данные"]["B4"].value == "=SUM(B2:B3)" and wb["Данные"]["B2"].value == 10)
before = x.read_bytes()
t = call("xlsx_write", path=str(x), sheets={"A": [[1]]})
check("xlsx: существующий не затирается", t.startswith("ОШИБКА") and x.read_bytes() == before, t)
t = call("xlsx_write", path=str(x), sheets={"A": [[1]]}, overwrite=True)
check("xlsx: overwrite делает .bak", t.startswith("Готово") and (root / "tab.xlsx.bak").read_bytes() == before, t)
check("плохое имя листа", "имя листа" in call("xlsx_write", path=str(root / "b.xlsx"), sheets={"a/b": [[1]]}))
check("длинное имя листа", "имя листа" in call("xlsx_write", path=str(root / "b.xlsx"), sheets={"x" * 32: [[1]]}))
check("не .xlsx", call("xlsx_write", path=str(root / "b.csv"), sheets={"a": [[1]]}).startswith("ОШИБКА"))
check("пустые sheets", call("xlsx_write", path=str(root / "b.xlsx"), sheets={}).startswith("ОШИБКА"))
check("файл не создан при отказе", not (root / "b.xlsx").exists())

m.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
