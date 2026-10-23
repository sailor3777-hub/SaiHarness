"""Offline tests for core.agent.ca: CA generation, server/client certs, CSR signing,
fingerprints and CA-signature verification. No Qt, no network, no database."""
import sys

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from core.agent import ca

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


# --- CA -------------------------------------------------------------------
ca_key, ca_cert = ca.generate_ca()
ca_obj = ca.load_certificate(ca_cert)
check("CA key loads back as EC", ca.load_private_key(ca_key) is not None)
check("CA is self-signed (issuer == subject)", ca_obj.issuer == ca_obj.subject)
bc = ca_obj.extensions.get_extension_for_class(x509.BasicConstraints).value
check("CA has BasicConstraints ca=True", bc.ca is True)
check("CA cert verifies against itself", ca.verify_signed_by_ca(ca_cert, ca_cert))

# --- server certificate ---------------------------------------------------
srv_key, srv_cert = ca.generate_server_cert(ca_key, ca_cert, "192.168.1.50")
srv_obj = ca.load_certificate(srv_cert)
check("server key loads back", ca.load_private_key(srv_key) is not None)
check("server cert is signed by the CA", ca.verify_signed_by_ca(srv_cert, ca_cert))
check("server cert issuer is the CA subject", srv_obj.issuer == ca_obj.subject)
eku = srv_obj.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
check("server cert has serverAuth EKU", ExtendedKeyUsageOID.SERVER_AUTH in eku)
san = srv_obj.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
check("server SAN carries the IP host", san.get_values_for_type(x509.IPAddress) and
      str(san.get_values_for_type(x509.IPAddress)[0]) == "192.168.1.50")

# a DNS host goes into the SAN as a DNS name, not an IP
_, srv_cert_dns = ca.generate_server_cert(ca_key, ca_cert, "main.local")
san_dns = ca.load_certificate(srv_cert_dns).extensions.get_extension_for_class(
    x509.SubjectAlternativeName).value
check("server SAN carries the DNS host", "main.local" in san_dns.get_values_for_type(x509.DNSName))

# --- CSR + client certificate ---------------------------------------------
host_key, csr_pem = ca.generate_csr("agent-alpha")
csr_obj = ca.load_csr(csr_pem)
check("CSR signature is valid", csr_obj.is_signature_valid)
check("CSR subject CN is the agent name",
      csr_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "agent-alpha")

client_cert = ca.sign_csr(ca_key, ca_cert, csr_pem)
client_obj = ca.load_certificate(client_cert)
check("client cert is signed by the CA", ca.verify_signed_by_ca(client_cert, ca_cert))
check("client cert keeps the CSR subject",
      client_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "agent-alpha")
ceku = client_obj.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
check("client cert has clientAuth EKU", ExtendedKeyUsageOID.CLIENT_AUTH in ceku)
check("client cert public key matches the host key",
      client_obj.public_key().public_numbers() == ca.load_private_key(host_key).public_key().public_numbers())

# a client's private key is the host's own; it is never the CA key
check("client public key is not the CA public key",
      client_obj.public_key().public_numbers() != ca_obj.public_key().public_numbers())

# --- fingerprints ----------------------------------------------------------
fp = ca.fingerprint(client_cert)
check("fingerprint is 64 hex chars", len(fp) == 64 and all(c in "0123456789abcdef" for c in fp))
check("fingerprint is stable", ca.fingerprint(client_cert) == fp)
check("different certs have different fingerprints", ca.fingerprint(srv_cert) != fp)

# --- negative cases --------------------------------------------------------
# a certificate from a DIFFERENT CA must not verify against ours
other_key, other_cert = ca.generate_ca("Other CA")
_, other_csr = ca.generate_csr("intruder")
foreign_cert = ca.sign_csr(other_key, other_cert, other_csr)
check("a cert from another CA does not verify against ours",
      not ca.verify_signed_by_ca(foreign_cert, ca_cert))

# a structurally broken CSR is rejected
try:
    ca.sign_csr(ca_key, ca_cert, b"-----BEGIN CERTIFICATE REQUEST-----\nnonsense\n-----END CERTIFICATE REQUEST-----\n")
    rejected = False
except Exception:
    rejected = True
check("a malformed CSR is rejected", rejected)

print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
