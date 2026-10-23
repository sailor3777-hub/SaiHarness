"""Offline tests for core.agent.registry and core.agent.enroll: agent records,
one-time enrollment tokens and the CSR -> certificate flow. In-memory database,
no Qt, no network."""
import sqlite3
import sys

from core.agent import ca, enroll
from core.agent.registry import Registry, RegistryInputError

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def raises(exc, fn):
    try:
        fn()
        return False
    except exc:
        return True


def new_registry() -> Registry:
    return Registry(sqlite3.connect(":memory:"))


# one CA for the whole file
CA_KEY, CA_CERT = ca.generate_ca()


# --- registry basics ------------------------------------------------------
reg = new_registry()
aid, token = enroll.create_agent(reg, "alpha")
row = reg.get(aid)
check("new agent is pending", row["status"] == "pending")
check("raw token is not stored", row["token_hash"] != token and row["token_hash"] != "")
check("token is stored as its hash", row["token_hash"] == enroll.hash_token(token))
check("new agent has no fingerprint yet", row["fingerprint"] == "")
check("get_by_name finds it", reg.get_by_name("alpha")["id"] == aid)
check("duplicate name is refused", raises(RegistryInputError, lambda: enroll.create_agent(reg, "alpha")))
check("invalid name is refused", raises(RegistryInputError, lambda: enroll.create_agent(reg, "has space")))
check("empty name is refused", raises(RegistryInputError, lambda: enroll.create_agent(reg, "")))

# --- full enrollment flow -------------------------------------------------
host_key, csr = ca.generate_csr("alpha")
eid, client_cert = enroll.enroll(reg, CA_KEY, CA_CERT, token, csr)
check("enroll returns the same agent id", eid == aid)
check("issued cert is signed by the CA", ca.verify_signed_by_ca(client_cert, CA_CERT))
row = reg.get(aid)
check("agent is now active", row["status"] == "active")
check("fingerprint recorded matches the cert", row["fingerprint"] == ca.fingerprint(client_cert))
check("token is burned after enrollment", row["token_hash"] == "")
check("enrolled_at is set", bool(row["enrolled_at"]))
check("cert_expires_at is set", bool(row["cert_expires_at"]))

# --- admission ------------------------------------------------------------
check("admit accepts the active fingerprint", reg.admit(ca.fingerprint(client_cert))["id"] == aid)
check("admit rejects an unknown fingerprint", reg.admit("00" * 32) is None)
check("admit rejects an empty fingerprint", reg.admit("") is None)

# --- token is single-use --------------------------------------------------
_, csr2 = ca.generate_csr("alpha")
check("the same token cannot enroll twice",
      raises(enroll.EnrollError, lambda: enroll.enroll(reg, CA_KEY, CA_CERT, token, csr2)))

# --- bad / unknown / expired tokens ---------------------------------------
reg2 = new_registry()
bid, btoken = enroll.create_agent(reg2, "beta")
_, bcsr = ca.generate_csr("beta")
check("an unknown token is refused",
      raises(enroll.EnrollError, lambda: enroll.enroll(reg2, CA_KEY, CA_CERT, "not-a-real-token", bcsr)))
# expire the token by hand, then it must be refused
reg2.conn.execute("UPDATE agents SET token_expires_at=datetime('now','-1 day') WHERE id=?", (bid,))
reg2.conn.commit()
check("an expired token is refused",
      raises(enroll.EnrollError, lambda: enroll.enroll(reg2, CA_KEY, CA_CERT, btoken, bcsr)))
check("agent stays pending after a failed enrollment", reg2.get(bid)["status"] == "pending")

# --- reissue --------------------------------------------------------------
new_tok = enroll.reissue(reg2, bid)
check("the old token still does not work after reissue",
      raises(enroll.EnrollError, lambda: enroll.enroll(reg2, CA_KEY, CA_CERT, btoken, bcsr)))
rid, rcert = enroll.enroll(reg2, CA_KEY, CA_CERT, new_tok, bcsr)
check("the reissued token enrolls", rid == bid and reg2.get(bid)["status"] == "active")
check("reissue is refused for an active agent",
      raises(enroll.EnrollError, lambda: enroll.reissue(reg2, bid)))

# --- malformed CSR --------------------------------------------------------
reg3 = new_registry()
cid, ctoken = enroll.create_agent(reg3, "gamma")
check("a malformed CSR is refused as an enroll error",
      raises(enroll.EnrollError, lambda: enroll.enroll(reg3, CA_KEY, CA_CERT, ctoken, b"garbage")))
check("agent stays pending after a bad CSR", reg3.get(cid)["status"] == "pending")

# --- revoke / delete ------------------------------------------------------
check("revoke blocks an active agent", reg.revoke(aid) and reg.get(aid)["status"] == "revoked")
check("a revoked agent is not admitted", reg.admit(ca.fingerprint(client_cert)) is None)
check("list returns the agents", {r["name"] for r in reg.list()} == {"alpha"})
check("delete removes the row", reg.delete(aid) and reg.get(aid) is None)

print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
