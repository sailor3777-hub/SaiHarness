"""Web access settings in the main database (read by both the Web tab and tools_server.py in its own process)."""
import sqlite3

SEARXNG_URL = "searxng_url"


def _ensure(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS web_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    _ensure(conn)
    row = conn.execute("SELECT value FROM web_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def put(conn: sqlite3.Connection, key: str, value: str):
    _ensure(conn)
    if value:
        conn.execute("INSERT INTO web_settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    else:
        conn.execute("DELETE FROM web_settings WHERE key=?", (key,))
    conn.commit()
