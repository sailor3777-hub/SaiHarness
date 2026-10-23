"""Files in the chat context: selection with checkboxes, folder traversal, reading and size estimation.

The selection is stored as two sets of paths: include (checked) and exclude (excluded inside a checked
folder). A checked folder means "all text files inside"; exclusions uncheck individual nodes.
Paths come from os.scandir/os.walk starting at the roots and are compared as strings.
"""
import errno
import math
import os
from collections import Counter
from dataclasses import dataclass, field

from core.markers import NOT_FOUND

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
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}  # same as core.vision.IMAGE_EXTENSIONS
MAX_FILE_BYTES = 300_000      # a larger file is not taken whole
MAX_FILES = 2000              # protection against accidentally selecting the whole disk
MAX_TOTAL_CHARS = 3_000_000

CHECKED, PARTIAL, UNCHECKED = 2, 1, 0  # same as Qt.CheckState


# Tokens per character of each class. Fitted on measurements of real models (Qwen 3.5/3.6 and Gemma 4 count
# almost the same) on 46 samples: prose, code, tables, logs, JSON. Median error ≈6%, 90th percentile ≈15%.
_TOKENS_PER_CHAR = {
    "cyrillic": 0.310, "latin": 0.199, "digit": 1.244, "space": 0.017,
    "newline": 0.637, "ascii_other": 0.756, "non_ascii_other": 1.177,
}


def _char_class(ch: str) -> str:
    if "\u0400" <= ch <= "\u04ff":
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
    """Token estimate without the model's tokenizer (about ±15%; the server reports the real number in usage)."""
    if not text:
        return 0
    total = 0.0
    for ch, n in Counter(text).items():  # by unique characters: fast even for millions of characters
        total += _TOKENS_PER_CHAR[_char_class(ch)] * n
    return math.ceil(total)


def is_ignored_dir(name: str) -> bool:
    return name in IGNORE_DIRS


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXT


def binary_placeholder(path: str) -> str:
    """Instead of a binary file's content the model gets its path: it looks at a picture with vision_describe."""
    try:
        size = f", {os.path.getsize(path) // 1024} KB"
    except OSError:
        size = ""
    hint = ("Content not included, this is an image: look at it with the vision_describe tool if it is available."
            if is_image_name(path) else "Content not included: the file is binary, not text.")
    kind = "image" if is_image_name(path) else "binary file"
    return "\n".join([f"[{kind}{size}]", f"Path: {path}", hint])


def looks_binary_by_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in BINARY_EXT


class Selection:
    """The set of checked and excluded paths, with the check inherited from folders."""

    def __init__(self, include=(), exclude=()):
        self.include: set[str] = set(include)
        self.exclude: set[str] = set(exclude)

    def copy(self) -> "Selection":
        return Selection(self.include, self.exclude)

    def is_empty(self) -> bool:
        return not self.include

    def decision(self, path: str) -> bool:
        """Whether a path is in the selection: the nearest checked or excluded node up the tree decides."""
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
        """Drop entries that are under none of the roots (the root was removed from the list)."""
        def inside(p):
            return any(p == r or p.startswith(r + os.sep) for r in existing_roots)
        self.include = {p for p in self.include if inside(p)}
        self.exclude = {p for p in self.exclude if inside(p)}


@dataclass
class ContextBundle:
    files: list[tuple[str, str]] = field(default_factory=list)  # (display name, text)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    truncated: bool = False  # the selection exceeded the MAX_FILES / MAX_TOTAL_CHARS limits

    @property
    def chars(self) -> int:
        return sum(len(t) for _, t in self.files)

    @property
    def tokens(self) -> int:
        return sum(estimate_tokens(t) + estimate_tokens(n) + 8 for n, t in self.files)


def read_text_file(path: str) -> tuple[str | None, str]:
    """Reads a text file. Returns (text, '') or (None, skip reason)."""
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            return None, f"too large ({size // 1024} KB)"
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, NOT_FOUND
    except PermissionError:
        return None, "no access to the file"
    except OSError as e:
        # the e.strerror text depends on the Windows language (it can be German), so the error code is used
        return None, f"cannot be read ({errno.errorcode.get(e.errno, 'I/O error')})"
    utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not utf16 and _looks_binary(raw):
        return None, "binary"
    for enc in ("utf-8-sig", "utf-16") if utf16 else ("utf-8-sig",):
        try:
            return raw.decode(enc).replace("\x00", ""), ""
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1251", errors="replace").replace("\x00", ""), ""  # the usual encoding of old Russian files


def _looks_binary(raw: bytes) -> bool:
    """A binary file: NUL bytes make up more than 1% of the start. Rare NULs (a header or zero padding, as in
    DumpStack.log) do not make a file binary: they are cut out of the text that was read."""
    sample = raw[:8192].rstrip(b"\x00")
    return sample.count(b"\x00") * 100 > len(sample)


def decode_for_edit(raw: bytes) -> tuple[str, str] | tuple[None, str]:
    """Decodes a file for editing so that it can be written back without distortion.

    Returns (text, encoding) or (None, reason for refusal). Supported: UTF-8 (with or without BOM)
    and cp1251, which strictly decodes everything that is not UTF-8: encoding back
    returns the original bytes. UTF-16 and binary files are not edited.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None, "UTF-16 encoding is not supported for editing"
    if b"\x00" in raw[:8192]:
        return None, "binary file"
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        return raw.decode(encoding), encoding
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1251"), "cp1251"
    except UnicodeDecodeError:
        return None, "could not determine the encoding without loss"


def _display_name(path: str, roots: list[str]) -> str:
    best = max((r for r in roots if path == r or path.startswith(r + os.sep)), key=len, default=None)
    if best is None:
        return path.replace("\\", "/")
    rel = os.path.relpath(path, best)
    base = os.path.basename(best.rstrip("\\/")) or best
    return base if rel == "." else f"{base}/{rel}".replace("\\", "/")


def expand_selection(sel: Selection, roots: list[str]) -> tuple[list[str], bool, list[str]]:
    """Files in the selection (without service folders), a truncation flag and checked paths missing on disk."""
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
                        continue  # text and images are taken from a folder; other binary files only if checked explicitly
                    found[full] = None
                    if len(found) >= MAX_FILES:
                        return list(found), True, missing
    return list(found), truncated, missing


def load_context(sel: Selection, roots: list[str]) -> ContextBundle:
    """Reads the selected files within the limits. Call from a background thread for large selections."""
    from core import docs  # local import: the agent host reuses this module without pypdf/python-docx
    bundle = ContextBundle()
    if sel.is_empty():
        return bundle
    paths, bundle.truncated, missing = expand_selection(sel, roots)
    bundle.skipped += [(p, "not found on disk") for p in missing]
    total = 0
    for path in paths:
        if docs.is_document_name(path):
            text, reason = docs.read_document_cached(path)
            if text is not None and len(text) > MAX_FILE_BYTES:
                text, reason = None, f"document text too large ({len(text) // 1024} KB)"
        elif looks_binary_by_name(path):
            text, reason = binary_placeholder(path), ""
        else:
            text, reason = read_text_file(path)
        if text is None:
            bundle.skipped.append((path, reason))
            continue
        if total + len(text) > MAX_TOTAL_CHARS:
            bundle.truncated = True
            bundle.skipped.append((path, "total size limit exceeded"))
            continue
        total += len(text)
        bundle.files.append((_display_name(path, roots), text))
    return bundle


def summarize(include: frozenset, exclude: frozenset, roots: tuple) -> dict:
    """Selection summary for the interface (runs in the background): number of files, size, token estimate."""
    bundle = load_context(Selection(include, exclude), list(roots))
    return {
        "files": len(bundle.files), "chars": bundle.chars, "tokens": bundle.tokens,
        "skipped": len(bundle.skipped), "truncated": bundle.truncated,
    }
