"""Локальный поисковый индекс (SQLite FTS5, ранжирование BM25) и краулер по списку сайтов пользователя."""
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from urllib.parse import urlsplit

from core.web.fetch import FetchError, WebFetcher

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    depth INTEGER NOT NULL DEFAULT 2,
    max_pages INTEGER NOT NULL DEFAULT 200,
    pages INTEGER NOT NULL DEFAULT 0,
    crawled_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS web_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES web_sources(id) ON DELETE CASCADE,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS web_fts USING fts5(
    title, text, content='web_pages', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS web_pages_ai AFTER INSERT ON web_pages BEGIN
    INSERT INTO web_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS web_pages_ad AFTER DELETE ON web_pages BEGIN
    INSERT INTO web_fts(web_fts, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS web_pages_au AFTER UPDATE ON web_pages BEGIN
    INSERT INTO web_fts(web_fts, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
    INSERT INTO web_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
"""

SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar",
            ".exe", ".msi", ".dmg", ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2", ".ttf", ".css", ".js", ".json", ".xml")
SITEMAP_LIMIT = 500


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)


def _site_key(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_source(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Некорректный адрес сайта: {url}")
    return parts._replace(fragment="", path=parts.path or "/").geturl()


def scope_prefix(start_url: str) -> str:
    """Путь стартового адреса, внутри которого идёт обход: для /3/tutorial это /3/tutorial, для файла /a/b.html это /a,
    для корня сайта пустая строка (обходится весь сайт)."""
    path = urlsplit(start_url).path
    if path.endswith("/"):
        path = path[:-1]
    elif "." in path.rsplit("/", 1)[-1]:
        path = path.rsplit("/", 1)[0]
    return path


def canonical(url: str) -> str:
    """Адрес без якоря и без хвоста index.html: /a/index.html и /a/ — одна страница, в индексе она должна быть одна."""
    url = url.split("#")[0]
    return re.sub(r"/index\.html?$", "/", url, flags=re.IGNORECASE) if "?" not in url else url


def in_scope(url: str, key: str, prefix: str) -> bool:
    parts = urlsplit(url)
    if _site_key(url) != key:
        return False
    return not prefix or parts.path == prefix or parts.path.startswith(prefix + "/")


def query_terms(query: str) -> list[str]:
    return re.findall(r"\w+", query.lower())


class WebIndex:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        ensure_schema(conn)

    # --- источники ---

    def add_source(self, url: str, depth: int = 2, max_pages: int = 200) -> int:
        url = normalize_source(url)
        self.conn.execute("INSERT OR IGNORE INTO web_sources(url, depth, max_pages) VALUES(?,?,?)",
                          (url, max(0, min(depth, 5)), max(1, min(max_pages, 5000))))
        self.conn.commit()
        return self.conn.execute("SELECT id FROM web_sources WHERE url=?", (url,)).fetchone()["id"]

    def remove_source(self, source_id: int):
        self.conn.execute("DELETE FROM web_pages WHERE source_id=?", (source_id,))
        self.conn.execute("DELETE FROM web_sources WHERE id=?", (source_id,))
        self.conn.commit()

    def sources(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM web_sources ORDER BY url").fetchall()

    def source(self, source_id: int):
        return self.conn.execute("SELECT * FROM web_sources WHERE id=?", (source_id,)).fetchone()

    def page_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]

    def is_known_host(self, url: str) -> bool:
        key = _site_key(url)
        return any(_site_key(s["url"]) == key for s in self.sources())

    # --- поиск ---

    def search(self, query: str, limit: int = 5) -> list[dict]:
        terms = query_terms(query)
        if not terms:
            return []
        quoted = [f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in terms]
        for expr in (" AND ".join(quoted), " OR ".join(quoted)):
            rows = self.conn.execute(
                "SELECT p.url, p.title, snippet(web_fts, 1, '', '', ' … ', 28) AS snippet, p.fetched_at "
                "FROM web_fts JOIN web_pages p ON p.id = web_fts.rowid "
                "WHERE web_fts MATCH ? ORDER BY bm25(web_fts, 4.0, 1.0) LIMIT ?", (expr, limit)).fetchall()
            if rows:
                return [{**r, "snippet": " ".join(r["snippet"].split())} for r in map(dict, rows)]
        return []

    # --- обход ---

    @staticmethod
    def _sitemap_urls(fetcher: WebFetcher, start: str) -> list[str]:
        parts = urlsplit(start)
        try:
            page = fetcher.fetch(f"{parts.scheme}://{parts.netloc}/sitemap.xml", use_cache=False, robots=True)
            root = ET.fromstring(page.text)
        except (FetchError, ET.ParseError):
            return []
        return [e.text.strip() for e in root.iter() if e.text and e.tag.rsplit("}", 1)[-1] == "loc"][:SITEMAP_LIMIT]

    def crawl(self, source_id: int, fetcher: WebFetcher, progress=None, should_stop=lambda: False) -> dict:
        """Обходит сайт в ширину в пределах его домена. Страницы, которых больше нет на сайте, удаляются из индекса."""
        src = self.source(source_id)
        if src is None:
            raise ValueError("Источник не найден")
        start = src["url"]
        key = _site_key(start)
        prefix = scope_prefix(start)
        queue = deque([(start, 0)])
        queue.extend((u, 1) for u in self._sitemap_urls(fetcher, start))
        seen: set[str] = set()
        visited: set[str] = set()
        saved, errors, stopped = 0, [], False
        while queue and saved < src["max_pages"]:
            if should_stop():
                stopped = True
                break
            url, depth = queue.popleft()
            url = canonical(url)
            if url in seen or not in_scope(url, key, prefix) or urlsplit(url).path.lower().endswith(SKIP_EXT):
                continue
            seen.add(url)
            try:
                page = fetcher.fetch(url, use_cache=False, robots=True)
            except FetchError as e:
                errors.append(f"{url}: {e}")
                continue
            if len(page.text) < 40:
                continue
            final = canonical(page.final_url)
            visited.add(final)
            # страница остаётся у источника, который сохранил её первым: пересекающиеся источники не отбирают её друг у друга
            self.conn.execute(
                "INSERT INTO web_pages(source_id, url, title, text, fetched_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET title=excluded.title, text=excluded.text, fetched_at=excluded.fetched_at",
                (source_id, final, page.title, page.text, datetime.now().isoformat(timespec="seconds")))
            self.conn.commit()
            saved += 1
            if progress:
                progress(saved, url)
            if depth < src["depth"]:
                queue.extend((link, depth + 1) for link in page.links)
        if not stopped and saved:  # полный обход: что не встретилось, с сайта пропало
            for row in self.conn.execute("SELECT id, url FROM web_pages WHERE source_id=?", (source_id,)).fetchall():
                if row["url"] not in visited and not queue:
                    self.conn.execute("DELETE FROM web_pages WHERE id=?", (row["id"],))
        total = self.conn.execute("SELECT COUNT(*) FROM web_pages WHERE source_id=?", (source_id,)).fetchone()[0]
        status = "остановлено" if stopped else (f"ошибок: {len(errors)}" if errors else "готово")
        self.conn.execute("UPDATE web_sources SET pages=?, crawled_at=?, status=? WHERE id=?",
                          (total, datetime.now().isoformat(timespec="seconds"), status, source_id))
        self.conn.commit()
        return {"saved": saved, "errors": errors, "stopped": stopped, "pages": total}
