"""The main harness's own CA and server identity on disk (no Qt).

Everything lives in ``data/agent/`` (``data/`` is already in ``.gitignore``):

    ca_key.pem / ca_cert.pem        the certificate authority (the key is the
                                    single most sensitive secret; access is
                                    restricted best-effort)
    server_key.pem / server_cert.pem  the certificate the bridge presents

``ensure_identity`` creates whatever is missing and returns the PEM of the CA
plus the file paths the bridge needs. Agents pin the CA, and the client does not
check the server hostname (CA pinning stands in for it), so the server
certificate's name is a fixed placeholder and never needs regenerating when the
user changes the address hosts dial.
"""
from __future__ import annotations

import os
from pathlib import Path

from core.agent import ca

SERVER_CN = "saiharness-main"


def ensure_identity(agent_dir) -> dict:
    agent_dir = Path(agent_dir)
    agent_dir.mkdir(parents=True, exist_ok=True)
    ca_key_path = agent_dir / "ca_key.pem"
    ca_cert_path = agent_dir / "ca_cert.pem"
    srv_key_path = agent_dir / "server_key.pem"
    srv_cert_path = agent_dir / "server_cert.pem"

    if not (ca_key_path.exists() and ca_cert_path.exists()):
        ca_key, ca_cert = ca.generate_ca()
        ca_key_path.write_bytes(ca_key)
        ca_cert_path.write_bytes(ca_cert)
        _restrict(ca_key_path)
        # a new CA invalidates the old server cert
        srv_key_path.unlink(missing_ok=True)
        srv_cert_path.unlink(missing_ok=True)

    ca_key = ca_key_path.read_bytes()
    ca_cert = ca_cert_path.read_bytes()

    if not (srv_key_path.exists() and srv_cert_path.exists()):
        srv_key, srv_cert = ca.generate_server_cert(ca_key, ca_cert, SERVER_CN)
        srv_key_path.write_bytes(srv_key)
        srv_cert_path.write_bytes(srv_cert)
        _restrict(srv_key_path)

    return {
        "ca_key_pem": ca_key,
        "ca_cert_pem": ca_cert,
        "ca_cert_path": str(ca_cert_path),
        "server_cert_path": str(srv_cert_path),
        "server_key_path": str(srv_key_path),
    }


def _restrict(path: Path):
    """Best-effort: tighten permissions on a private key. No-op where unsupported."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
