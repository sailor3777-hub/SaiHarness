"""Создание Word и Excel для инструментов docx_write / xlsx_write (чистый Python, без Qt).

Word собирается из Markdown (markdown-it-py + python-docx): заголовки, абзацы, жирный/курсив/код/ссылки,
маркированные и нумерованные списки (с вложенностью), таблицы, цитаты, блоки кода, горизонтальная черта.
Excel собирается из словаря «имя листа → строки»; строка-значение, начинающаяся с «=», пишется формулой.
Обе функции пишут атомарно (временный файл + replace) и возвращают (сообщение, '') или (None, причина).
"""
import os
import shutil
from pathlib import Path

MAX_MARKDOWN_CHARS = 500_000
MAX_CELLS = 200_000
MAX_SHEETS = 30
BAD_SHEET_CHARS = set('[]:*?/\\')
CODE_FONT = "Consolas"


def _prepare_target(path: Path, overwrite: bool, suffixes: tuple[str, ...]) -> str:
    """Пустая строка, если писать можно, иначе причина отказа."""
    if path.suffix.lower() not in suffixes:
        return f"расширение файла должно быть {' или '.join(suffixes)}"
    if path.is_dir():
        return "по этому пути папка"
    if path.exists() and not overwrite:
        return "файл уже существует; чтобы заменить его, передай overwrite=true (старый сохранится как .bak)"
    return ""


def _atomic_save(path: Path, save, overwrite: bool) -> str:
    """Сохраняет через save(tmp_path); существующий файл перед заменой копируется в .bak. Возвращает путь копии или ''."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".sai-tmp")
    backup = ""
    try:
        save(tmp)
        if path.exists() and overwrite:
            n, bak = 0, path.with_name(path.name + ".bak")
            while bak.exists():
                n += 1
                bak = path.with_name(f"{path.name}.bak.{n}")
            shutil.copy2(path, bak)
            backup = str(bak)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return backup


# ---------------------------------------------------------------- Word

def _add_inline(paragraph, children, base_bold=False, base_italic=False):
    bold, italic, link = base_bold, base_italic, None
    for tok in children:
        t = tok.type
        if t == "strong_open": bold = True
        elif t == "strong_close": bold = base_bold
        elif t == "em_open": italic = True
        elif t == "em_close": italic = base_italic
        elif t == "link_open": link = tok.attrGet("href")
        elif t == "link_close":
            if link:
                paragraph.add_run(f" ({link})")
            link = None
        elif t in ("softbreak", "hardbreak"):
            paragraph.add_run().add_break()
        elif t in ("text", "code_inline", "html_inline", "image"):
            text = tok.content
            if t == "image":
                text = tok.content or tok.attrGet("src") or ""
            run = paragraph.add_run(text)
            run.bold, run.italic = bold or None, italic or None
            if t == "code_inline":
                run.font.name = CODE_FONT


def _list_style(kind: str, depth: int) -> str:
    base = "List Bullet" if kind == "bullet" else "List Number"
    return base if depth <= 1 else f"{base} {min(depth, 3)}"


def markdown_to_docx(markdown: str):
    from docx import Document
    from docx.shared import Pt
    from markdown_it import MarkdownIt

    doc = Document()
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    lists: list[str] = []          # стек видов открытых списков
    quote = 0                      # глубина цитаты
    table: dict | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t == "heading_open":
            inline = tokens[i + 1]
            p = doc.add_heading(level=min(int(tok.tag[1:]), 4))
            _add_inline(p, inline.children or [])
            i += 3
            continue
        if t == "paragraph_open":
            inline = tokens[i + 1]
            if table is not None:
                table["cell"].append(inline.content)
            else:
                style = _list_style(lists[-1], len(lists)) if lists else ("Quote" if quote else None)
                p = doc.add_paragraph(style=style)
                _add_inline(p, inline.children or [])
            i += 3
            continue
        if t in ("bullet_list_open", "ordered_list_open"):
            lists.append("bullet" if t == "bullet_list_open" else "ordered")
        elif t in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif t == "blockquote_open":
            quote += 1
        elif t == "blockquote_close":
            quote -= 1
        elif t in ("fence", "code_block"):
            p = doc.add_paragraph()
            run = p.add_run(tok.content.rstrip("\n"))
            run.font.name, run.font.size = CODE_FONT, Pt(9)
        elif t == "hr":
            doc.add_paragraph("―" * 20)
        elif t == "table_open":
            table = {"rows": [], "row": None, "cell": None, "head": 0}
        elif t == "tr_open":
            table["row"] = []
        elif t in ("th_open", "td_open"):
            table["cell"] = []
            if t == "th_open":
                table["head"] = len(table["rows"]) + 1
        elif t in ("th_close", "td_close"):
            table["row"].append(" ".join(table["cell"]))
            table["cell"] = None
        elif t == "inline" and table is not None and table["cell"] is not None:
            table["cell"].append(tok.content)
        elif t == "tr_close":
            table["rows"].append(table["row"])
        elif t == "table_close":
            rows = table["rows"]
            width = max((len(r) for r in rows), default=0)
            if rows and width:
                grid = doc.add_table(rows=len(rows), cols=width)
                grid.style = "Table Grid"
                for r, row in enumerate(rows):
                    for c, text in enumerate(row):
                        cell = grid.cell(r, c)
                        run = cell.paragraphs[0].add_run(text)
                        if r < table["head"]:
                            run.bold = True
                doc.add_paragraph()
            table = None
        i += 1
    return doc


def write_docx(markdown: str, path: Path, overwrite: bool = False) -> tuple[str | None, str]:
    if not markdown.strip():
        return None, "markdown пуст"
    if len(markdown) > MAX_MARKDOWN_CHARS:
        return None, f"текст слишком длинный (больше {MAX_MARKDOWN_CHARS} символов)"
    reason = _prepare_target(path, overwrite, (".docx",))
    if reason:
        return None, reason
    try:
        doc = markdown_to_docx(markdown)
        backup = _atomic_save(path, lambda tmp: doc.save(str(tmp)), overwrite)
    except OSError as e:
        return None, f"запись не удалась ({e.__class__.__name__}: {e.strerror or e})"
    extra = f" Копия прежнего файла: {backup}." if backup else ""
    return f"Готово: создан Word-документ {path}.{extra}", ""


# ---------------------------------------------------------------- Excel

def write_xlsx(sheets: dict, path: Path, overwrite: bool = False) -> tuple[str | None, str]:
    from openpyxl import Workbook
    if not isinstance(sheets, dict) or not sheets:
        return None, "sheets должен быть непустым объектом {имя листа: [[ячейки строки], ...]}"
    if len(sheets) > MAX_SHEETS:
        return None, f"листов больше {MAX_SHEETS}"
    cells = 0
    for name, rows in sheets.items():
        if not name or len(name) > 31 or BAD_SHEET_CHARS & set(name):
            return None, f"недопустимое имя листа «{name}» (до 31 символа, без []:*?/\\)"
        if not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
            return None, f"лист «{name}»: ожидается список строк, каждая строка список ячеек"
        cells += sum(len(r) for r in rows)
    if cells > MAX_CELLS:
        return None, f"ячеек больше {MAX_CELLS}"
    reason = _prepare_target(path, overwrite, (".xlsx",))
    if reason:
        return None, reason
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append([c if isinstance(c, (str, int, float, bool)) or c is None else str(c) for c in row])
    try:
        backup = _atomic_save(path, lambda tmp: wb.save(str(tmp)), overwrite)
    except OSError as e:
        return None, f"запись не удалась ({e.__class__.__name__}: {e.strerror or e})"
    extra = f" Копия прежнего файла: {backup}." if backup else ""
    return f"Готово: создана книга Excel {path} (листов: {len(sheets)}, ячеек: {cells}).{extra}", ""
