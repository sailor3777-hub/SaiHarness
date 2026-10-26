"""Offline loopback test for the agent channel (core.agent.bridge + wrapper):
enrollment over the wire, an mTLS steady-state connection, tool_call -> tool_result
dispatch, and admission control (a foreign-CA certificate is rejected at the TLS
layer, a revoked agent is refused). Everything runs on 127.0.0.1; no real network."""
import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from core.agent import ca, enroll, wrapper
from core.agent.bridge import Bridge
from core.agent.registry import Registry

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


async def wait_until(fn, timeout=5.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if fn():
            return True
        await asyncio.sleep(0.05)
    return bool(fn())


async def main(db_path: str):
    tmp = Path(tempfile.mkdtemp(prefix="sai_chan_"))
    try:
        # CA + server identity, written to files (ssl loads cert chains from files)
        ca_key, ca_cert = ca.generate_ca()
        srv_key, srv_cert = ca.generate_server_cert(ca_key, ca_cert, "127.0.0.1")
        ca_cert_path = tmp / "ca_cert.pem"; ca_cert_path.write_bytes(ca_cert)
        srv_cert_path = tmp / "server_cert.pem"; srv_cert_path.write_bytes(srv_cert)
        srv_key_path = tmp / "server_key.pem"; srv_key_path.write_bytes(srv_key)

        work = tmp / "work"; work.mkdir()
        (work / "hello.txt").write_text("secret-marker-42", encoding="utf-8")

        # a pending agent in the shared file registry
        reg = Registry(sqlite3.connect(db_path))
        agent_id, token = enroll.create_agent(reg, "alpha")
        reg.conn.close()

        bridge = Bridge(ca_key, ca_cert, str(srv_cert_path), str(srv_key_path), str(ca_cert_path), db_path)
        await bridge.start("127.0.0.1", 0)
        url = f"wss://127.0.0.1:{bridge.port()}"

        # --- enrollment over the channel ---
        ckey = tmp / "client_key.pem"; ccert = tmp / "client_cert.pem"
        cert_pem = await asyncio.wait_for(
            wrapper.enroll_over_channel(url, str(ca_cert_path), token, "alpha", str(ckey), str(ccert)), 10)
        check("enrollment returns a cert signed by the CA", ca.verify_signed_by_ca(cert_pem, ca_cert))
        check("client key + cert files are written", ckey.exists() and ccert.exists())
        reg = Registry(sqlite3.connect(db_path))
        check("agent is active after enrollment", reg.get(agent_id)["status"] == "active")
        reg.conn.close()

        # the one-time token no longer enrolls
        rej = False
        try:
            _, csr2 = ca.generate_csr("alpha")
            await asyncio.wait_for(
                wrapper.enroll_over_channel(url, str(ca_cert_path), token, "alpha",
                                            str(tmp / "k2.pem"), str(tmp / "c2.pem")), 10)
        except wrapper.EnrollmentFailed:
            rej = True
        check("the one-time token is refused the second time", rej)

        # --- steady state (mTLS) ---
        host = wrapper.ToolHost([str(work)])
        task = asyncio.create_task(
            wrapper.run_agent(url, str(ca_cert_path), str(ccert), str(ckey), "alpha", host))
        check("agent connects with its certificate",
              await wait_until(lambda: "alpha" in bridge.connected_names(), 5))

        res = await bridge.call("alpha", "fs_read", {"path": str(work / "hello.txt")})
        check("tool_call returns the file content", "secret-marker-42" in res["output"] and not res["is_error"])
        res2 = await bridge.call("alpha", "fs_read", {"path": str(work / "nope.txt")})
        check("a missing file comes back as an error result", res2["is_error"])
        res3 = await bridge.call("alpha", "fs_list", {"path": str(work)})
        check("fs_list sees the file", "hello.txt" in res3["output"] and not res3["is_error"])
        res4 = await bridge.call("alpha", "shell_run", {"command": "echo channel-ok"})
        check("shell_run runs on the host", "channel-ok" in res4["output"] and not res4["is_error"])

        # paths are not restricted: a file outside the start folders is readable
        res5 = await bridge.call("alpha", "fs_read", {"path": str(tmp / "ca_cert.pem")})
        check("a path outside the start folders is readable",
              "BEGIN CERTIFICATE" in res5["output"] and not res5["is_error"])

        # deleting the agent closes its live connection
        check("disconnect drops a connected agent",
              await bridge.disconnect("alpha")
              and await wait_until(lambda: "alpha" not in bridge.connected_names(), 5))
        check("disconnecting an unknown agent is a no-op", not await bridge.disconnect("nobody"))

        # disconnect
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        check("agent is gone after it disconnects",
              await wait_until(lambda: "alpha" not in bridge.connected_names(), 5))

        # --- a foreign-CA certificate cannot connect ---
        other_key, other_cert = ca.generate_ca("Intruder CA")
        intr_key, intr_csr = ca.generate_csr("alpha")
        intr_cert = ca.sign_csr(other_key, other_cert, intr_csr)
        fkey = tmp / "foreign_key.pem"; fkey.write_bytes(intr_key)
        fcert = tmp / "foreign_cert.pem"; fcert.write_bytes(intr_cert)
        rejected = False
        try:
            await asyncio.wait_for(
                wrapper.run_agent(url, str(ca_cert_path), str(fcert), str(fkey), "alpha", host), 5)
        except Exception:
            rejected = True
        check("a foreign-CA certificate is rejected",
              rejected and "alpha" not in bridge.connected_names())

        # --- a revoked agent is refused even with a valid certificate ---
        reg = Registry(sqlite3.connect(db_path)); reg.revoke(agent_id); reg.conn.close()
        task2 = asyncio.create_task(
            wrapper.run_agent(url, str(ca_cert_path), str(ccert), str(ckey), "alpha", host))
        connected = await wait_until(lambda: "alpha" in bridge.connected_names(), 2)
        check("a revoked agent is not admitted", not connected and "alpha" not in bridge.connected_names())
        task2.cancel()
        try:
            await task2
        except (asyncio.CancelledError, Exception):
            pass

        await bridge.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


db = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
asyncio.run(main(db))
print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
