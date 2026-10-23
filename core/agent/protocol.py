"""Channel frame format over the WebSocket link (no Qt, no network I/O).

One agent connection carries JSON text frames, each with a ``type`` and -- for
request/response pairs -- a correlation ``id``. Pure (de)serialization lives
here; the socket work is in bridge.py (main side) and wrapper.py (host side).

Steady state (mTLS connection, after enrollment):
    hello         host -> main : the agent announces its name + protocol version
    capabilities  host -> main : os, allowed roots, tool names
    tool_call     main -> host : {id, tool, args}
    tool_result   host -> main : {id, output, is_error}
    ping / pong   either way   : keepalive

Enrollment (server-auth-only connection, no client cert yet):
    enroll_request host -> main : {token, csr, name}
    enroll_ok      main -> host : {cert}            the signed client certificate
    error          main -> host : {message}         enrollment refused

``is_error`` is carried as an explicit boolean, not inferred from the text, so a
tool whose output happens to start with "ERROR" is not mistaken for a failure
(the same reason core.markers exists for the local tools). The main side maps it
back onto the router's error accounting in Phase 3.
"""
from __future__ import annotations

import json
import uuid

PROTO_VERSION = 1
MAX_FRAME_BYTES = 2_000_000  # a tool result is capped far below this; reject anything larger

HELLO = "hello"
CAPABILITIES = "capabilities"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
PING = "ping"
PONG = "pong"
ENROLL_REQUEST = "enroll_request"
ENROLL_OK = "enroll_ok"
ERROR = "error"


class ProtocolError(ValueError):
    """A frame was not valid JSON, not an object, or missing its type."""


def new_id() -> str:
    """A correlation id for a tool_call / tool_result pair."""
    return uuid.uuid4().hex


def encode(frame_type: str, **fields) -> str:
    fields["type"] = frame_type
    return json.dumps(fields, ensure_ascii=False)


def decode(raw) -> dict:
    """Parse a received frame into a dict with a ``type``. Raises ProtocolError."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_FRAME_BYTES:
            raise ProtocolError("frame too large")
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as e:
            raise ProtocolError("frame is not valid UTF-8") from e
    elif isinstance(raw, str):
        if len(raw) > MAX_FRAME_BYTES:
            raise ProtocolError("frame too large")
    else:
        raise ProtocolError("frame is not text")
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ProtocolError("frame is not valid JSON") from e
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ProtocolError("frame has no type")
    return obj


# --- builders -------------------------------------------------------------

def hello(agent_name: str) -> str:
    return encode(HELLO, name=agent_name, proto=PROTO_VERSION)


def capabilities(os_name: str, roots: list[str], tools: list[str]) -> str:
    return encode(CAPABILITIES, os=os_name, roots=list(roots), tools=list(tools))


def tool_call(call_id: str, tool: str, args: dict) -> str:
    return encode(TOOL_CALL, id=call_id, tool=tool, args=args or {})


def tool_result(call_id: str, output: str, is_error: bool) -> str:
    return encode(TOOL_RESULT, id=call_id, output=output, is_error=bool(is_error))


def ping() -> str:
    return encode(PING)


def pong() -> str:
    return encode(PONG)


def enroll_request(token: str, csr_pem: str, name: str = "") -> str:
    return encode(ENROLL_REQUEST, token=token, csr=csr_pem, name=name)


def enroll_ok(cert_pem: str) -> str:
    return encode(ENROLL_OK, cert=cert_pem)


def error(message: str) -> str:
    return encode(ERROR, message=message)
