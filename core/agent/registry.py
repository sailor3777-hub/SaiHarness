"""Agent records in the database (no Qt).

One row per agent in the ``agents`` table. The row carries the single-use
enrollment token (only its hash, never the raw token) until the agent enrolls,
then the client-certificate fingerprint. The fingerprint allowlist plus the
status check are the registry half of the mTLS admission decision, on top of the
cryptographic check in ``core.agent.ca.verify_signed_by_ca``:

    pending  -> created, token still valid, not yet enrolled
    active   -> enrolled, has a fingerprint, may connect
    revoked  -> blocked; the bridge drops the socket and refuses new connections

This module is storage only. Token generation, lifetimes and the CSR -> cert
flow live in ``core.agent.enroll``; the table just holds what it is given.

Like ``core.memory``, a ``Registry`` wraps a connection passed in, so tests run
on an in-memory database and nothing touches ``data/saiharness.db``.
"""
from __future__ import annotations

import re
import sqlite3

# A name is used verbatim in the catalog as host_<name>.<tool>, so keep it to a
# safe, short identifier.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    os TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL DEFAULT '',
    token_expires_at TEXT,
    cert_expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'revoked')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    enrolled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agents_fingerprint ON agents(fingerprint);
"""


class RegistryInputError(ValueError):
    """An error in words the user understands (a bad or duplicate name)."""


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise RegistryInputError(
            "agent name must be 1-32 chars of letters, digits, '-' or '_' and start with a letter or digit"
        )
    return name


class Registry:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        ensure_schema(conn)

    def close(self):
        self.conn.close()

    # --- creation -----------------------------------------------------------

    def create(self, name: str, token_hash: str, token_ttl_days: int) -> int:
        """Create a pending agent carrying a hashed enrollment token. Returns its id.

        Raises RegistryInputError if the name is invalid or already taken.
        """
        name = clean_name(name)
        if self.get_by_name(name) is not None:
            raise RegistryInputError(f'the name "{name}" is already taken')
        cur = self.conn.execute(
            "INSERT INTO agents(name, token_hash, token_expires_at, status)"
            f" VALUES(?, ?, datetime('now', '+{int(token_ttl_days)} days'), 'pending')",
            (name, token_hash),
        )
        self.conn.commit()
        return cur.lastrowid

    def reissue_token(self, agent_id: int, token_hash: str, token_ttl_days: int) -> bool:
        """Replace the token of a still-pending agent. True if a pending agent was updated."""
        cur = self.conn.execute(
            "UPDATE agents SET token_hash=?,"
            f" token_expires_at=datetime('now', '+{int(token_ttl_days)} days')"
            " WHERE id=? AND status='pending'",
            (token_hash, agent_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # --- enrollment result --------------------------------------------------

    def mark_enrolled(self, agent_id: int, fingerprint: str, cert_ttl_days: int) -> bool:
        """Record the issued certificate and activate the agent, burning its token.

        True on success; False if there was no such pending agent.
        """
        cur = self.conn.execute(
            "UPDATE agents SET fingerprint=?, status='active', token_hash='',"
            " enrolled_at=datetime('now'),"
            f" cert_expires_at=datetime('now', '+{int(cert_ttl_days)} days')"
            " WHERE id=? AND status='pending'",
            (fingerprint, agent_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_os(self, fingerprint: str, os_name: str):
        """Record the OS the host announced on connect (Phase 2)."""
        self.conn.execute("UPDATE agents SET os=? WHERE fingerprint=? AND fingerprint!=''",
                          (os_name, fingerprint))
        self.conn.commit()

    # --- lookups ------------------------------------------------------------

    def get(self, agent_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()

    def get_by_name(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()

    def find_pending_by_token(self, token_hash: str) -> sqlite3.Row | None:
        """A pending agent whose unexpired token matches. Used only during enrollment."""
        if not token_hash:
            return None
        return self.conn.execute(
            "SELECT * FROM agents WHERE token_hash=? AND token_hash!='' AND status='pending'"
            " AND token_expires_at > datetime('now')",
            (token_hash,),
        ).fetchone()

    def admit(self, fingerprint: str) -> sqlite3.Row | None:
        """The registry half of mTLS admission: an active agent with this fingerprint."""
        if not fingerprint:
            return None
        return self.conn.execute(
            "SELECT * FROM agents WHERE fingerprint=? AND fingerprint!='' AND status='active'",
            (fingerprint,),
        ).fetchone()

    def list(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agents ORDER BY name").fetchall()

    # --- revocation / removal ----------------------------------------------

    def revoke(self, agent_id: int) -> bool:
        """Block the agent. Keeps the row for the record; drops any live token."""
        cur = self.conn.execute(
            "UPDATE agents SET status='revoked', token_hash='' WHERE id=? AND status!='revoked'",
            (agent_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, agent_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        self.conn.commit()
        return cur.rowcount > 0
