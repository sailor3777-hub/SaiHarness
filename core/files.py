"""Файлы в контексте чата: выбор галочками, обход папок, чтение и оценка размера.

Выбор хранится как два набора путей: include (отмеченные) и exclude (исключённые внутри отмеченной
папки). Папка с галочкой означает «все текстовые файлы внутри», исключения снимают отдельные узлы.
Пути берутся из os.scandir/os.walk от корней и сравниваются как строки.
"""
import errno
import math
import os
from collections import Counter
from dataclasses import dataclass, field

from core import docs

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode",
    "dist", "build", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".psd", ".svgz",
    ".zip", ".7z", ".rar", ".gz", ".tar", ".bz2", ".xz", ".jar", ".whl",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".a", ".lib", ".pyc", ".pyd", ".class",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".db", ".sqlite", ".sqlite3",
    ".mp3", ".wav", ".flac", ".ogg", ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".ttf", ".otf", ".woff", ".woff2", ".gguf", ".safetensors", ".pt", ".onnx", ".npy", ".npz",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}  # совпадает с core.vision.IMAGE_EXTENSIONS
MAX_FILE_BYTES = 300_000      # файл крупнее не берём целиком
MAX_FILES = 2000              # защита от случайного выбора всего диска
MAX_TOTAL_CHARS = 3_000_000

CHECKED, PARTIAL, UNCHECKED = 2, 1, 0  # совпадает с Qt.CheckState


# Токенов на один символ каждого класса. Подобрано по замерам реальных моделей (Qwen 3.5/3.6, Gemma 4 считают
# почти одинаково) на 46 образцах: проза, код, таблицы, логи, JSON. Медиана ошибки ≈6%, 90-й перцентиль ≈15%.
_TOKENS_PER_CHAR = {
    "cyrillic": 0.310, "latin": 0.199, "digit": 1.244, "space": 0.017,
    "newline": 0.637, "ascii_other": 0.756, "non_ascii_other": 1.177,
}


def _char_class(ch: str) -> str:
    if "Ѐ" <= ch <= "ӿ":
        return "cyrillic"
    if ch.isascii():
        if ch.isalpha():
            return "latin"
        if ch.isdigit():
            return "digit"
        if ch in " \t":
            return "space"
        return "newline" if ch in "\r\n" else "ascii_other"
    return "digit" if ch.isdigit() else "non_ascii_other"


def estimate_tokens(text: str) -> int:
    """Оценка числа токенов без токенизатора модели (порядок ±15%; реальное число сообщает сервер в usage)."""
    if not text:
        return 0
    total = 0.0
    for ch, n in Counter(text).items():  # по уникальным символам: быстро даже для миллионов символов
        total += _TOKENS_PER_CHAR[_char_class(ch)] * n
    return math.ceil(total)


def is_ignored_dir(name: str) -> bool:
    return name in IGNORE_DIRS


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXT


def binary_placeholder(path: str) -> str:
    """Вместо содержимого бинарного файла модель получает путь: картинку она смотрит через vision_describe."""
    try:
        size = f", {os.path.getsize(path) // 1024} КБ"
    except OSError:
        size = ""
    hint = ("Содержимое не вставлено, это изображение: посмотри его инструментом vision_describe, если он есть."
            if is_image_name(path) else "Содержимое не вставлено: файл бинарный, не текстовый.")
    kind = "изображение" if is_image_name(path) else "бинарный файл"
    return "\n".join([f"[{kind}{size}]", f"Путь: {path}", hint])


def looks_binary_by_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in BINARY_EXT


class Selection:
    """Набор отмеченных и исключённых путей с наследованием отметки от папок."""

    def __init__(self, include=(), exclude=()):
        self.include: set[str] = set(include)
        self.exclude: set[str] = set(exclude)

    def copy(self) -> "Selection":
        return Selection(self.include, self.exclude)

    def is_empty(self) -> bool:
        return not self.include

    def decision(self, path: str) -> bool:
        """Входит ли путь в выбор: решает ближайший отмеченный или исключённый узел вверх по дереву."""
        p = path
        while True:
            if p in self.exclude:
                return False
            if p in self.include:
                return True
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent

    def state(self, path: str) -> int:
        prefix = path + os.sep
        inside = lambda s: any(x.startswith(prefix) for x in s)
        if self.decision(path):
            return PARTIAL if inside(self.exclude) else CHECKED
        return PARTIAL if inside(self.include) else UNCHECKED

    def toggle(self, path: str, checked: bool):
        prefix = path + os.sep
        self.include = {p for p in self.include if not p.startswith(prefix)}
        self.exclude = {p for p in self.exclude if not p.startswith(prefix)}
        self.include.discard(path)
        self.exclude.discard(path)
        inherited = self.decision(os.path.dirname(path)) if os.path.dirname(path) != path else False
        if checked and not inherited:
            self.include.add(path)
        elif not checked and inherited:
            self.exclude.add(path)

    def prune(self, existing_roots: list[str]):
        """Убрать записи, которые не лежат ни в одном из корней (корень удалён из списка)."""
        def inside(p):
            return any(p == r or p.startswith(r + os.sep) for r in existing_roots)
        self.include = {p for p in self.include if inside(p)}
        self.exclude = {p for p in self.exclude if inside(p)}


@dataclass
class ContextBundle:
    files: list[tuple[str, str]] = field(default_factory=list)  # (имя для показа, текст)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (путь, причина)
    truncated: bool = False  # выбор оказался больше лимитов MAX_FILES / MAX_TOTAL_CHARS

    @property
    def chars(self) -> int:
        return sum(len(t) for _, t in self.files)

    @property
    def tokens(self) -> int:
        return sum(estimate_tokens(t) + estimate_tokens(n) + 8 for n, t in self.files)


def read_text_file(path: str) -> tuple[str | None, str]:
    """Читает текстовый файл. Возвращает (текст, '') или (None, причина пропуска)."""
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            return None, f"слишком большой ({size // 1024} КБ)"
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, "файл не найден"
    except PermissionError:
        return None, "нет доступа к файлу"
    except OSError as e:
        # текст e.strerror зависит от языка Windows (бывает немецким), поэтому берём код ошибки
        return None, f"не читается ({errno.errorcode.get(e.errno, 'ошибка ввода-вывода')})"
    utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not utf16 and _looks_binary(raw):
        return None, "бинарный"
    for enc in ("utf-8-sig", "utf-16") if utf16 else ("utf-8-sig",):
        try:
            return raw.decode(enc).replace("\x00", ""), ""
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1251", errors="replace").replace("\x00", ""), ""  # типичная кодировка старых русских файлов


def _looks_binary(raw: bytes) -> bool:
    """Бинарный файл: NUL-байтов больше 1% начала. Редкие NUL (заголовок или добивка нулями, как в
    DumpStack.log) файл бинарным не делают: они вырезаются из прочитанного текста."""
    sample = raw[:8192].rstrip(b"\x00")
    return sample.count(b"\x00") * 100 > len(sample)


def decode_for_edit(raw: bytes) -> tuple[str, str] | tuple[None, str]:
    """Декодирует файл для правки так, чтобы его можно было записать обратно без искажений.

    Возвращает (текст, кодировка) или (None, причина отказа). Поддерживаются UTF-8 (с BOM и без)
    и cp1251, в которой строго декодируется всё, что не является UTF-8: обратное кодирование
    возвращает исходные байты. UTF-16 и бинарные файлы не правятся.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None, "кодировка UTF-16 для правки не поддерживается"
    if b"\x00" in raw[:8192]:
        return None, "бинарный файл"
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        return raw.decode(encoding), encoding
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1251"), "cp1251"
    except UnicodeDecodeError:
        return None, "не удалось определить кодировку без потерь"


def _display_name(path: str, roots: list[str]) -> str:
    best = max((r for r in roots if path == r or path.startswith(r + os.sep)), key=len, default=None)
    if best is None:
        return path.replace("\\", "/")
    rel = os.path.relpath(path, best)
    base = os.path.basename(best.rstrip("\\/")) or best
    return base if rel == "." else f"{base}/{rel}".replace("\\", "/")


def expand_selection(sel: Selection, roots: list[str]) -> tuple[list[str], bool, list[str]]:
    """Файлы, попавшие в выбор (без служебных папок), признак усечения и отмеченные пути, которых нет на диске."""
    found: dict[str, None] = {}
    missing: list[str] = []
    truncated = False
    for p in sorted(sel.include):
        if os.path.isfile(p):
            if sel.decision(p):
                found[p] = None
        elif not os.path.isdir(p):
            missing.append(p)
        else:
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not is_ignored_dir(d) and sel.decision(os.path.join(dirpath, d))
                )
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    if not sel.decision(full):
                        continue
                    if looks_binary_by_name(name) and not is_image_name(name):
                        continue  # из папки берутся текст и картинки; прочие бинарные файлы только если отмечены явно
                    found[full] = None
                    if len(found) >= MAX_FILES:
                        return list(found), True, missing
    return list(found), truncated, missing


def load_context(sel: Selection, roots: list[str]) -> ContextBundle:
    """Читает выбранные файлы с учётом лимитов. Вызывать из фонового потока для больших выборов."""
    bundle = ContextBundle()
    if sel.is_empty():
        return bundle
    paths, bundle.truncated, missing = expand_selection(sel, roots)
    bundle.skipped += [(p, "не найден на диске") for p in missing]
    total = 0
    for path in paths:
        if docs.is_document_name(path):
            text, reason = docs.read_document_cached(path)
            if text is not None and len(text) > MAX_FILE_BYTES:
                text, reason = None, f"слишком большой текст документа ({len(text) // 1024} КБ)"
        elif looks_binary_by_name(path):
            text, reason = binary_placeholder(path), ""
        else:
            text, reason = read_text_file(path)
        if text is None:
            bundle.skipped.append((path, reason))
            continue
        if total + len(text) > MAX_TOTAL_CHARS:
            bundle.truncated = True
            bundle.skipped.append((path, "превышен общий лимит объёма"))
            continue
        total += len(text)
        bundle.files.append((_display_name(path, roots), text))
    return bundle


def summarize(include: frozenset, exclude: frozenset, roots: tuple) -> dict:
    """Сводка выбора для интерфейса (запускается в фоне): число файлов, объём, оценка токенов."""
    bundle = load_context(Selection(include, exclude), list(roots))
    return {
        "files": len(bundle.files), "chars": bundle.chars, "tokens": bundle.tokens,
        "skipped": len(bundle.skipped), "truncated": bundle.truncated,
    }
