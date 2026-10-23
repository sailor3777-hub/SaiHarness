"""One-time enrollment: tokens and the CSR -> certificate flow (no Qt).

Each agent is created with a single-use enrollment token that the main harness
bakes into the agent's installer. The raw token is shown/stored once in the
installer and never kept in the database -- the registry keeps only its SHA-256
hash, like a password.

On first run the host proves the token and sends a CSR (``ca.generate_csr`` runs
on the host, so its private key never crosses the wire). The main harness signs
the CSR (``ca.sign_csr``), records the resulting fingerprint in the registry and
burns the token. Later reconnects authenticate with the client certificate, so
the token is used exactly once in an agent's life.

This module owns token policy (generation, lifetime, verification); the registry
is storage and the CA is crypto.
"""
from __future__ import annotations

import hashlib
import secrets

from core.agent import ca
from core.agent.registry import Registry

TOKEN_TTL_DAYS = 7         # how long an unused enrollment token stays valid
TOKEN_BYTES = 32           # entropy of the token (256 bits); a plain hash is enough to store it


class EnrollError(ValueError):
    """Enrollment was refused (unknown, expired or already-used token, or a bad CSR)."""


def new_token() -> str:
    """A fresh high-entropy enrollment token (URL-safe text)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The value stored in the registry. Tokens are random and high-entropy, so a
    single SHA-256 is sufficient; no slow KDF is needed as it would be for a human password."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create_agent(reg: Registry, name: str, ttl_days: int = TOKEN_TTL_DAYS) -> tuple[int, str]:
    """Create a pending agent and return ``(agent_id, token)``.

    The caller shows the token to the host exactly once (via the installer); it
    cannot be recovered later, only reissued. Raises RegistryInputError on a bad
    or duplicate name.
    """
    token = new_token()
    agent_id = reg.create(name, hash_token(token), ttl_days)
    return agent_id, token


def reissue(reg: Registry, agent_id: int, ttl_days: int = TOKEN_TTL_DAYS) -> str:
    """Issue a new token for a still-pending agent (e.g. the first install failed).

    Returns the new token. Raises EnrollError if the agent is not pending.
    """
    token = new_token()
    if not reg.reissue_token(agent_id, hash_token(token), ttl_days):
        raise EnrollError("no pending agent to reissue a token for")
    return token


def enroll(
    reg: Registry,
    ca_key_pem: bytes,
    ca_cert_pem: bytes,
    token: str,
    csr_pem: bytes,
    client_days: int = ca.CLIENT_DAYS,
) -> tuple[int, bytes]:
    """Complete enrollment: verify the token, sign the CSR, record the agent.

    Returns ``(agent_id, client_cert_pem)``. Raises EnrollError if the token is
    unknown/expired/used or the CSR is invalid. The token is burned on success,
    so a second call with the same token fails.
    """
    agent = reg.find_pending_by_token(hash_token(token))
    if agent is None:
        raise EnrollError("enrollment token is unknown, expired or already used")
    try:
        client_cert = ca.sign_csr(ca_key_pem, ca_cert_pem, csr_pem, days=client_days)
    except ValueError as e:
        raise EnrollError(f"bad CSR: {e}") from e
    fingerprint = ca.fingerprint(client_cert)
    if not reg.mark_enrolled(agent["id"], fingerprint, client_days):
        # The agent stopped being pending between the lookup and here (e.g. a
        # concurrent enrollment won the race). Treat it as a used token.
        raise EnrollError("enrollment token is unknown, expired or already used")
    return agent["id"], client_cert
