"""One-time enrollment: tokens and the CSR -> certificate flow (no Qt). [Phase 1]

Each agent is created with a single-use enrollment token baked into its
installer. On first run the host proves the token, sends a CSR (see
``ca.generate_csr``), the main harness signs it (``ca.sign_csr``) and records the
resulting fingerprint in the registry; the token is then burned. Later
reconnects authenticate with the client certificate, never the token.

Not implemented yet — see AGENT_MODULE_SPEC.md section 4.2 and 11 (Phase 1).
"""
from __future__ import annotations
