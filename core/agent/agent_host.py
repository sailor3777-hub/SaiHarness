"""Host-side entry point: enroll once, then stay connected (no Qt).

This runs on the remote host (inside the install folder the installer prepared).
It reads ``agent.json`` (name, address, roots) and the pinned ``ca_cert.pem``,
enrolls on first run using the one-time token, and then keeps a steady-state
mTLS connection, reconnecting after drops. The enrollment token is used exactly
once and the token file is removed after a successful enrollment.

Layout in the install folder:
    ca_cert.pem            the pinned CA (written by the installer)
    agent.json             {"name", "address", "roots": [...]}
    enroll_token.txt       the one-time token (present only until enrolled)
    client_key.pem         this agent's private key (created here on enroll)
    client_cert.pem        the certificate the main harness issued

Run:  python agent_host.py --dir <install folder>
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make the shipped package importable when run as a bare script from the install
# folder (installer lays out core/ next to this file's package root).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.agent import wrapper  # noqa: E402

RECONNECT_BACKOFF = 3.0  # seconds between reconnection attempts


def _load_config(install_dir: Path) -> dict:
    # utf-8-sig: PowerShell's Set-Content -Encoding utf8 writes a BOM that plain utf-8 rejects.
    return json.loads((install_dir / "agent.json").read_text(encoding="utf-8-sig"))


def _token(install_dir: Path, token: str | None) -> str | None:
    if token:
        return token
    tf = install_dir / "enroll_token.txt"
    return tf.read_text(encoding="utf-8-sig").strip() if tf.exists() else None


async def supervise(install_dir, token: str | None = None,
                    backoff: float = RECONNECT_BACKOFF, stop: "asyncio.Event | None" = None):
    """Enroll if needed, then keep reconnecting until ``stop`` is set."""
    install_dir = Path(install_dir)
    cfg = _load_config(install_dir)
    name, address, roots = cfg["name"], cfg["address"], cfg.get("roots", [])
    ca_path = str(install_dir / "ca_cert.pem")
    key_path = install_dir / "client_key.pem"
    cert_path = install_dir / "client_cert.pem"
    url = address if address.startswith("wss://") else f"wss://{address}"
    host = wrapper.ToolHost(roots)

    if not (cert_path.exists() and key_path.exists()):
        tok = _token(install_dir, token)
        if not tok:
            raise wrapper.EnrollmentFailed("no enrollment token and no certificate yet")
        await wrapper.enroll_over_channel(url, ca_path, tok, name, str(key_path), str(cert_path))
        (install_dir / "enroll_token.txt").unlink(missing_ok=True)  # burn the local copy

    while not (stop is not None and stop.is_set()):
        try:
            await wrapper.run_agent(url, ca_path, str(cert_path), str(key_path), name, host)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # connection dropped or refused; back off and retry
        if stop is not None and stop.is_set():
            break
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            raise


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SaiHarness remote agent host")
    parser.add_argument("--dir", required=True, help="install folder with agent.json and ca_cert.pem")
    parser.add_argument("--token", default=None, help="one-time enrollment token (else read from enroll_token.txt)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(supervise(args.dir, args.token))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
