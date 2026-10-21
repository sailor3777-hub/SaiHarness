import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "saiharness.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_profiles (
    name TEXT PRIMARY KEY,
    connection TEXT NOT NULL DEFAULT '',
    base_model TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    temperature REAL NOT NULL DEFAULT 0.7,
    max_tokens INTEGER NOT NULL DEFAULT 2048,
    max_steps INTEGER NOT NULL DEFAULT 16,
    memory TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS connections (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS context_roots (
    path TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS chat_context (
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('in', 'ex')),
    PRIMARY KEY (chat_id, path)
);
CREATE TABLE IF NOT EXISTS chat_tools (
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    tool_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, tool_id)
);
CREATE TABLE IF NOT EXISTS tool_policy (
    tool_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('auto', 'ask', 'deny'))
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""

DEFAULT_CHAT_TITLE = "New chat"
LEGACY_CHAT_TITLES: tuple[str, ...] = ("Новый чат",)  # earlier default titles (Russian version): such chats in old databases get an automatic title too
DEFAULT_MAX_STEPS = 16  # how many rounds of tool calls the model may make in one reply


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    # migrations: columns added after the first versions of the tables
    _ensure_column(conn, "model_profiles", "connection", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "model_profiles", "max_steps", f"INTEGER NOT NULL DEFAULT {DEFAULT_MAX_STEPS}")
    _ensure_column(conn, "model_profiles", "memory_rules", "TEXT NOT NULL DEFAULT ''")  # rules for long-term memory (empty: core.memory.DEFAULT_RULES is used)
    _ensure_column(conn, "chats", "profile", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "messages", "reasoning", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "messages", "tool_calls", "TEXT NOT NULL DEFAULT ''")    # JSON of the calls in the model's reply
    _ensure_column(conn, "messages", "tool_call_id", "TEXT NOT NULL DEFAULT ''")  # for role='tool'
    _ensure_column(conn, "messages", "tool_name", "TEXT NOT NULL DEFAULT ''")     # for role='tool'
    _ensure_column(conn, "messages", "images", "TEXT NOT NULL DEFAULT ''")        # JSON list of paths of attached images
    return conn


def _ensure_column(conn, table: str, column: str, ddl: str):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()


def get_profile(name: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM model_profiles WHERE name=?", (name,)).fetchone()


def get_connection(name: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM connections WHERE name=?", (name,)).fetchone()


def set_chat_profile(chat_id: int, profile: str):
    with connect() as conn:
        conn.execute("UPDATE chats SET profile=? WHERE id=?", (profile, chat_id))


def save_connection(name, kind, url, api_key, old_name=None):
    """Creates a connection or updates old_name (renaming it).
    Raises ValueError if the name is already taken by another connection."""
    with connect() as conn:
        taken = conn.execute("SELECT 1 FROM connections WHERE name=?", (name,)).fetchone()
        if taken and name != old_name:
            raise ValueError(f"The name \"{name}\" is already taken")
        if old_name:
            cur = conn.execute(
                "UPDATE connections SET name=?, kind=?, url=?, api_key=? WHERE name=?",
                (name, kind, url, api_key, old_name),
            )
            if cur.rowcount:
                if name != old_name:
                    conn.execute("UPDATE model_profiles SET connection=? WHERE connection=?", (name, old_name))
                return
        conn.execute(
            "INSERT INTO connections(name, kind, url, api_key) VALUES(?,?,?,?)",
            (name, kind, url, api_key),
        )


def list_connections() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM connections ORDER BY name").fetchall()


def delete_connection(name):
    with connect() as conn:
        conn.execute("DELETE FROM connections WHERE name=?", (name,))


def list_roots() -> list[str]:
    with connect() as conn:
        return [r["path"] for r in conn.execute("SELECT path FROM context_roots ORDER BY path")]


def add_root(path: str):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO context_roots(path) VALUES(?)", (path,))


def remove_root(path: str):
    with connect() as conn:
        conn.execute("DELETE FROM context_roots WHERE path=?", (path,))


def load_chat_context(chat_id: int) -> tuple[set[str], set[str]]:
    """(include, exclude) of the paths selected for the chat."""
    with connect() as conn:
        rows = conn.execute("SELECT path, mode FROM chat_context WHERE chat_id=?", (chat_id,)).fetchall()
    return {r["path"] for r in rows if r["mode"] == "in"}, {r["path"] for r in rows if r["mode"] == "ex"}


def save_chat_context(chat_id: int, include, exclude):
    with connect() as conn:
        conn.execute("DELETE FROM chat_context WHERE chat_id=?", (chat_id,))
        conn.executemany(
            "INSERT INTO chat_context(chat_id, path, mode) VALUES(?,?,?)",
            [(chat_id, p, "in") for p in include] + [(chat_id, p, "ex") for p in exclude],
        )


def is_default_title(title: str) -> bool:
    """The chat has no name yet: it has the default title (current or from an older version)."""
    return title == DEFAULT_CHAT_TITLE or title in LEGACY_CHAT_TITLES


def create_chat(title: str = DEFAULT_CHAT_TITLE) -> int:
    with connect() as conn:
        return conn.execute("INSERT INTO chats(title) VALUES(?)", (title,)).lastrowid


def list_chats() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM chats ORDER BY updated_at DESC, id DESC").fetchall()


def rename_chat(chat_id: int, title: str):
    with connect() as conn:
        conn.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))


def delete_chat(chat_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))


def get_chat(chat_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()


def add_message(chat_id: int, role: str, content: str, reasoning: str = "", images: str = "") -> int:
    return add_messages(chat_id, [{"role": role, "content": content, "reasoning": reasoning, "images": images}])[-1]


def add_messages(chat_id: int, messages: list[dict]) -> list[int]:
    """Adds messages in one transaction (a tool call and its results are saved only together).

    Keys: role, content, and the optional reasoning, tool_calls (JSON string), tool_call_id, tool_name, images (JSON string).
    """
    ids = []
    with connect() as conn:
        for m in messages:
            ids.append(conn.execute(
                "INSERT INTO messages(chat_id, role, content, reasoning, tool_calls, tool_call_id, tool_name, images)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (chat_id, m["role"], m["content"], m.get("reasoning", ""), m.get("tool_calls", ""),
                 m.get("tool_call_id", ""), m.get("tool_name", ""), m.get("images", "")),
            ).lastrowid)
        conn.execute("UPDATE chats SET updated_at=datetime('now') WHERE id=?", (chat_id,))
    return ids


def load_chat_tools(chat_id: int) -> set[str]:
    """ids of the tools ("server.name") enabled in the chat."""
    with connect() as conn:
        return {r["tool_id"] for r in conn.execute("SELECT tool_id FROM chat_tools WHERE chat_id=?", (chat_id,))}


def save_chat_tools(chat_id: int, tool_ids):
    with connect() as conn:
        conn.execute("DELETE FROM chat_tools WHERE chat_id=?", (chat_id,))
        conn.executemany("INSERT INTO chat_tools(chat_id, tool_id) VALUES(?,?)", [(chat_id, t) for t in tool_ids])


def load_tool_policies() -> dict[str, str]:
    with connect() as conn:
        return {r["tool_id"]: r["mode"] for r in conn.execute("SELECT tool_id, mode FROM tool_policy")}


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    """An empty value deletes the setting."""
    with connect() as conn:
        if value:
            conn.execute("INSERT INTO app_settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))
        else:
            conn.execute("DELETE FROM app_settings WHERE key=?", (key,))


def set_tool_policy(tool_id: str, mode: str | None):
    """mode=None resets to the default."""
    with connect() as conn:
        if mode is None:
            conn.execute("DELETE FROM tool_policy WHERE tool_id=?", (tool_id,))
        else:
            conn.execute(
                "INSERT INTO tool_policy(tool_id, mode) VALUES(?,?) ON CONFLICT(tool_id) DO UPDATE SET mode=excluded.mode",
                (tool_id, mode),
            )


def list_messages(chat_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()


def save_profile(name, connection, base_model, system_prompt, temperature, max_tokens, memory,
                 max_steps=DEFAULT_MAX_STEPS, memory_rules=""):
    with connect() as conn:
        conn.execute(
            "INSERT INTO model_profiles(name, connection, base_model, system_prompt, temperature, max_tokens, memory,"
            " max_steps, memory_rules) VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET connection=excluded.connection,"
            " base_model=excluded.base_model,"
            " system_prompt=excluded.system_prompt, temperature=excluded.temperature,"
            " max_tokens=excluded.max_tokens, memory=excluded.memory, max_steps=excluded.max_steps,"
            " memory_rules=excluded.memory_rules",
            (name, connection, base_model, system_prompt, temperature, max_tokens, memory, max_steps, memory_rules),
        )


def delete_profile(name):
    with connect() as conn:
        conn.execute("DELETE FROM model_profiles WHERE name=?", (name,))


def list_profiles() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM model_profiles ORDER BY name").fetchall()
