"""Render a per-agent installer script from the template (no Qt).

Approach A ("embed in the installer"): the generated ``installer.ps1`` carries
everything needed to stand up the agent on the host -- the pinned CA, the
one-time token, the address, and the agent package itself (our slim host code)
as base64. The host script unpacks it, makes a venv, installs the two third-party
deps (``cryptography``, ``websockets``), and runs ``agent_host``. Nothing but the
two deps comes from anywhere other than this script.

The crypto material comes from ``core.agent`` code, never from a model; this
module only collects source files and substitutes strings.
"""
from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "templates" / "installer.ps1.tmpl"
ROOT = Path(__file__).resolve().parents[2]

# The slim host package: just what the wrapper needs. Deliberately excludes
# core/docs.py (and its pypdf/python-docx deps): files.py imports it lazily and
# the host never calls load_context, so fs_read/list/search work without it.
PACKAGE_FILES = (
    "core/__init__.py",
    "core/markers.py",
    "core/shell.py",
    "core/files.py",
    "core/agent/__init__.py",
    "core/agent/ca.py",
    "core/agent/protocol.py",
    "core/agent/wrapper.py",
    "core/agent/agent_host.py",
)


def build_package(root: Path | None = None) -> bytes:
    """A zip of the slim host package, laid out as core/... so imports work."""
    root = Path(root) if root is not None else ROOT
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in PACKAGE_FILES:
            zf.writestr(rel, (root / rel).read_bytes())
    return buf.getvalue()


def render(address: str, ca_cert_pem, token: str, name: str,
           template: str | None = None, package: bytes | None = None) -> str:
    text = template if template is not None else TEMPLATE.read_text(encoding="utf-8")
    ca_text = ca_cert_pem.decode("ascii") if isinstance(ca_cert_pem, (bytes, bytearray)) else ca_cert_pem
    pkg = package if package is not None else build_package()
    pkg_b64 = base64.b64encode(pkg).decode("ascii")
    return (
        text.replace("{{AGENT_NAME}}", name)
            .replace("{{MAIN_ADDRESS}}", address)
            .replace("{{ENROLLMENT_TOKEN}}", token)
            .replace("{{CA_CERT_PEM}}", ca_text.strip())
            .replace("{{PACKAGE_B64}}", pkg_b64)
    )
