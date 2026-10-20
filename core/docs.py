"""Текст из PDF, Word и Excel для инструмента fs_read (чистый Python, без Qt).

read_document(path) возвращает (текст, '') или (None, причина). Форматы: .pdf (pypdf), .docx (python-docx),
.xlsx/.xlsm (openpyxl). Старые .doc/.xls не поддерживаются. Результат не режется по размеру модели:
это делает вызывающий код (fs_read отдаёт текст кусками через offset).
"""
import os
import threading
from collections import OrderedDict

DOC_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xlsm"}
MAX_DOC_BYTES = 30_000_000     # файл крупнее не открываем
MAX_DOC_CHARS = 2_000_000      # дальше текст не извлекаем
MAX_SHEET_ROWS = 20_000


def is_document_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in DOC_EXTENSIONS


class _Out:
    """Накопитель текста с предельным объёмом."""

    def __init__(self):
        self.parts: list[str] = []
        self.size = 0
        self.full = False

    def add(self, s: str) -> bool:
        if self.full:
            return False
        self.parts.append(s)
        self.size += len(s) + 1
        if self.size >= MAX_DOC_CHARS:
            self.full = True
        return not self.full

    def text(self) -> str:
        t = "\n".join(self.parts)
        if self.full:
            t = t[:MAX_DOC_CHARS] + f"\n[документ длиннее {MAX_DOC_CHARS} символов, остальное не извлечено]"
        return t


def _read_pdf(path: str) -> tuple[str | None, str]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")  # PDF с пустым паролем открывается без вопросов
            except Exception:
                ok = 0
            if not ok:
                return None, "PDF защищён паролем"
        out = _Out()
        pages = len(reader.pages)
        has_text = False
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            has_text = has_text or bool(text)
            if not out.add(f"--- стр. {i} из {pages} ---\n{text}" if text else f"--- стр. {i} из {pages} --- (текста нет)"):
                break
    except PdfReadError as e:
        return None, f"PDF повреждён ({e})"
    if not has_text:
        return None, "в PDF нет текстового слоя (похоже на скан), прочитать его как текст нельзя"
    return out.text(), ""


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _read_docx(path: str) -> tuple[str | None, str]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        document = docx.Document(path)
    except Exception as e:
        return None, f"не открывается как Word ({type(e).__name__})"
    out = _Out()
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "") if para.style is not None else ""
            if style.startswith(("Heading", "Заголовок")):
                digits = "".join(c for c in style if c.isdigit())
                text = "#" * (int(digits) if digits else 1) + " " + text
            elif style == "Title":
                text = "# " + text
            elif "List" in style or "Список" in style:
                text = "- " + text
            if not out.add(text):
                break
        elif tag == "tbl":
            table = Table(child, document)
            ok = True
            for row in table.rows:
                cells, last_tc = [], None
                for c in row.cells:
                    if c._tc is not last_tc:  # объединённая ячейка повторяется в row.cells
                        cells.append(_cell(c.text.strip()))
                        last_tc = c._tc
                ok = out.add(" | ".join(cells))
                if not ok:
                    break
            if not ok:
                break
            out.add("")
    return out.text(), ""


def _read_xlsx(path: str) -> tuple[str | None, str]:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        return None, f"не открывается как Excel ({type(e).__name__})"
    out = _Out()
    try:
        for ws in wb.worksheets:
            rows = 0
            if not out.add(f"=== Лист «{ws.title}» ==="):
                break
            for row in ws.iter_rows(values_only=True):
                cells = [_cell(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not cells:
                    continue
                rows += 1
                if rows > MAX_SHEET_ROWS:
                    out.add(f"[строк больше {MAX_SHEET_ROWS}, остальные не извлечены]")
                    break
                if not out.add("\t".join(cells)):
                    break
            if out.full:
                break
    finally:
        wb.close()
    return out.text(), ""


_READERS = {".pdf": _read_pdf, ".docx": _read_docx, ".xlsx": _read_xlsx, ".xlsm": _read_xlsx}


def read_document(path: str) -> tuple[str | None, str]:
    """Текст документа или (None, причина). Формулы Excel отдаются последним сохранённым значением."""
    ext = os.path.splitext(path)[1].lower()
    reader = _READERS.get(ext)
    if reader is None:
        return None, "формат не поддерживается"
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return None, "файл не найден"
    except OSError:
        return None, "нет доступа к файлу"
    if size > MAX_DOC_BYTES:
        return None, f"слишком большой ({size // 1024 // 1024} МБ)"
    try:
        return reader(path)
    except PermissionError:
        return None, "нет доступа к файлу"
    except Exception as e:  # битые файлы дают самые разные исключения библиотек
        return None, f"не удалось прочитать ({type(e).__name__})"


_cache: "OrderedDict[tuple, tuple[str | None, str]]" = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 8


def read_document_cached(path: str) -> tuple[str | None, str]:
    """read_document с кешем по (путь, время изменения, размер): панель «Файлы» читает выбор при каждой сводке и отправке."""
    try:
        st = os.stat(path)
    except OSError:
        return read_document(path)
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    result = read_document(path)
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return result
