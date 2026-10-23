"""Offline tests for core.agent.identity and core.agent.installer: the main
harness's CA/server files on disk and rendering a per-agent installer. No Qt."""
import sys
import tempfile
from pathlib import Path

from core.agent import ca, identity, installer

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


tmp = Path(tempfile.mkdtemp(prefix="sai_identity_"))
info = identity.ensure_identity(tmp / "agent")

for key in ("ca_key_pem", "ca_cert_pem", "ca_cert_path", "server_cert_path", "server_key_path"):
    check(f"identity has {key}", info.get(key))
check("ca_cert file exists", Path(info["ca_cert_path"]).exists())
check("server cert + key files exist",
      Path(info["server_cert_path"]).exists() and Path(info["server_key_path"]).exists())
check("server cert is signed by the generated CA",
      ca.verify_signed_by_ca(Path(info["server_cert_path"]).read_bytes(), info["ca_cert_pem"]))

info2 = identity.ensure_identity(tmp / "agent")
check("ensure_identity is idempotent (same CA on second call)",
      info2["ca_cert_pem"] == info["ca_cert_pem"])

# a different folder gets its own CA
other = identity.ensure_identity(tmp / "agent2")
check("a different folder gets a different CA", other["ca_cert_pem"] != info["ca_cert_pem"])

# --- installer rendering ---
text = installer.render("192.168.1.10:8765", info["ca_cert_pem"], "TOKEN-xyz", "alpha")
check("installer carries the address", "192.168.1.10:8765" in text)
check("installer carries the token", "TOKEN-xyz" in text)
check("installer carries the agent name", "alpha" in text)
check("installer embeds the CA certificate", "BEGIN CERTIFICATE" in text)
check("no placeholders are left unfilled", "{{" not in text and "}}" not in text)

print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
