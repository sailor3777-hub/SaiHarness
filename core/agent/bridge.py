"""In-process agent bridge: the TLS listener agents dial into (no Qt here).

The bridge owns the agent-facing WebSocket-over-TLS listener and the table of
connected agents. Each connection is one of two kinds, told apart by whether the
client presented a certificate during the TLS handshake:

- no client certificate -> enrollment. The host has only its one-time token and
  the CA certificate yet. It sends ``enroll_request`` (token + CSR); the bridge
  runs ``core.agent.enroll.enroll`` (verify token, sign CSR, record fingerprint,
  burn token) and replies ``enroll_ok`` with the issued certificate. The host
  then reconnects with that certificate.

- a client certificate -> steady state (mTLS). The TLS layer has already checked
  the certificate is signed by our CA; the bridge adds the registry half of
  admission (``Registry.admit``: the fingerprint is known and the agent is
  active). An admitted agent is registered as connected and served: tool_result
  frames resolve pending calls, pings are answered.

``call(name, tool, args)`` sends a tool_call to a connected agent and awaits its
tool_result. Wiring these connected agents into the tool catalog and the
permission flow is Phase 3; this module is the transport and admission only.

The listener uses CERT_OPTIONAL so both kinds of connection reach the same
handler on one port; a certificate, when presented, is still verified against
the CA, so a certificate from any other CA fails the handshake outright.
"""
from __future__ import annotations

import asyncio
import ssl

from websockets.asyncio.server import serve

from core.agent import ca, enroll, protocol
from core.agent.registry import Registry

TOOL_TIMEOUT = 90.0  # matches the router's shell budget; a call that overruns is abandoned

# WebSocket close codes the bridge uses (4000-4999 is the application-private range)
CLOSE_NOT_ADMITTED = 4403
CLOSE_BAD_ENROLL = 4400


class ConnectedAgent:
    def __init__(self, name: str, fingerprint: str, ws):
        self.name = name
        self.fingerprint = fingerprint
        self.ws = ws
        self.os = ""
        self.tools: list[str] = []
        self.roots: list[str] = []
        self.pending: dict[str, asyncio.Future] = {}


def _peer_der(ws) -> bytes | None:
    """The client certificate (DER) from the TLS handshake, or None if none was sent."""
    transport = getattr(ws, "transport", None)
    if transport is None:
        return None
    ssl_object = transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    return ssl_object.getpeercert(binary_form=True)


class Bridge:
    def __init__(self, ca_key_pem: bytes, ca_cert_pem: bytes,
                 server_cert_path: str, server_key_path: str, ca_cert_path: str, db_path: str):
        self.ca_key_pem = ca_key_pem
        self.ca_cert_pem = ca_cert_pem
        self.server_cert_path = server_cert_path
        self.server_key_path = server_key_path
        self.ca_cert_path = ca_cert_path
        self.db_path = db_path
        self._connected: dict[str, ConnectedAgent] = {}
        self._server = None

    # --- lifecycle ----------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(self.server_cert_path, self.server_key_path)
        ctx.load_verify_locations(self.ca_cert_path)
        ctx.verify_mode = ssl.CERT_OPTIONAL  # enrollment clients have no cert yet
        return ctx

    async def start(self, host: str = "127.0.0.1", port: int = 0):
        self._server = await serve(self._handle, host, port, ssl=self._ssl_context())
        return self._server

    async def stop(self):
        for agent in list(self._connected.values()):
            try:
                await agent.ws.close()
            except Exception:
                pass
        self._connected.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def disconnect(self, name: str) -> bool:
        """Close a connected agent's socket (after it was deleted). True if it was connected."""
        agent = self._connected.get(name)
        if agent is None:
            return False
        try:
            await agent.ws.close(CLOSE_NOT_ADMITTED, "removed")
        except Exception:
            pass
        return True

    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    def connected_names(self) -> list[str]:
        return sorted(self._connected)

    async def snapshot(self) -> list[dict]:
        """Serializable view of the connected agents, for reading from another thread."""
        return [{"name": a.name, "os": a.os, "tools": list(a.tools), "roots": list(a.roots)}
                for a in self._connected.values()]

    def _registry(self) -> Registry:
        import sqlite3
        return Registry(sqlite3.connect(self.db_path))

    # --- connection handling ------------------------------------------------

    async def _handle(self, ws):
        der = _peer_der(ws)
        if der is None:
            await self._handle_enrollment(ws)
        else:
            await self._handle_steady(ws, ca.fingerprint_der(der))

    async def _handle_enrollment(self, ws):
        try:
            frame = protocol.decode(await ws.recv())
        except (protocol.ProtocolError, Exception):
            await ws.close(CLOSE_BAD_ENROLL, "bad enrollment frame")
            return
        if frame.get("type") != protocol.ENROLL_REQUEST:
            await ws.send(protocol.error("expected enroll_request"))
            await ws.close(CLOSE_BAD_ENROLL, "expected enroll_request")
            return
        reg = self._registry()
        try:
            _, cert = enroll.enroll(reg, self.ca_key_pem, self.ca_cert_pem,
                                    frame.get("token", ""), (frame.get("csr") or "").encode("ascii"))
        except enroll.EnrollError as e:
            await ws.send(protocol.error(str(e)))
            await ws.close(CLOSE_BAD_ENROLL, "enrollment refused")
            return
        finally:
            reg.conn.close()
        await ws.send(protocol.enroll_ok(cert.decode("ascii")))

    async def _handle_steady(self, ws, fingerprint: str):
        reg = self._registry()
        try:
            agent_row = reg.admit(fingerprint)
            os_setter = (lambda os_name: reg.set_os(fingerprint, os_name)) if agent_row else None
        finally:
            if agent_row is None:
                reg.conn.close()
        if agent_row is None:
            await ws.close(CLOSE_NOT_ADMITTED, "not admitted")
            return
        agent = ConnectedAgent(agent_row["name"], fingerprint, ws)
        self._connected[agent.name] = agent
        try:
            async for raw in ws:
                try:
                    msg = protocol.decode(raw)
                except protocol.ProtocolError:
                    continue
                kind = msg.get("type")
                if kind == protocol.TOOL_RESULT:
                    fut = agent.pending.pop(msg.get("id", ""), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif kind == protocol.CAPABILITIES:
                    agent.os = msg.get("os", "")
                    agent.tools = list(msg.get("tools") or [])
                    agent.roots = list(msg.get("roots") or [])
                    os_setter(agent.os)
                elif kind == protocol.PING:
                    await ws.send(protocol.pong())
                # HELLO and anything else is informational
        except Exception:
            pass
        finally:
            if self._connected.get(agent.name) is agent:
                del self._connected[agent.name]
            for fut in agent.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(f"agent {agent.name} disconnected"))
            reg.conn.close()

    # --- dispatch -----------------------------------------------------------

    async def call(self, name: str, tool: str, args: dict | None = None,
                   timeout: float = TOOL_TIMEOUT) -> dict:
        """Run a tool on a connected agent. Returns {output, is_error}.

        Raises ConnectionError if the agent is not connected, TimeoutError if it
        does not answer in time, or ConnectionError if it drops mid-call.
        """
        agent = self._connected.get(name)
        if agent is None:
            raise ConnectionError(f"agent {name} is not connected")
        call_id = protocol.new_id()
        fut = asyncio.get_running_loop().create_future()
        agent.pending[call_id] = fut
        await agent.ws.send(protocol.tool_call(call_id, tool, args or {}))
        try:
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            agent.pending.pop(call_id, None)
            raise
        return {"output": msg.get("output", ""), "is_error": bool(msg.get("is_error"))}
