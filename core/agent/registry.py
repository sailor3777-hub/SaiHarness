"""Agent records in the database (no Qt). [Phase 1]

One row per agent: id, name, os, client-certificate fingerprint, created_at,
expires_at, status (active / revoked / used). The fingerprint allowlist plus the
status check are the mTLS admission decision on top of ``ca.verify_signed_by_ca``;
revoking an agent flips its status and the bridge drops the connection.

Not implemented yet — see AGENT_MODULE_SPEC.md section 5 and 11 (Phase 1).
"""
from __future__ import annotations
