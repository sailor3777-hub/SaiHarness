"""Render a per-agent installer script from the template (no Qt).

The main harness fills the template placeholders with real values: the address
hosts dial, the pinned CA certificate, the one-time enrollment token and the
agent name. The crypto material comes from ``core.agent`` code, never from a
model; this module only substitutes strings.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "templates" / "installer.ps1.tmpl"


def render(address: str, ca_cert_pem, token: str, name: str, template: str | None = None) -> str:
    text = template if template is not None else TEMPLATE.read_text(encoding="utf-8")
    ca_text = ca_cert_pem.decode("ascii") if isinstance(ca_cert_pem, (bytes, bytearray)) else ca_cert_pem
    return (
        text.replace("{{AGENT_NAME}}", name)
            .replace("{{MAIN_ADDRESS}}", address)
            .replace("{{ENROLLMENT_TOKEN}}", token)
            .replace("{{CA_CERT_PEM}}", ca_text.strip())
    )
