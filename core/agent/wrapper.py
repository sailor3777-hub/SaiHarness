"""The thin executor that runs on the host (no Qt).

No model, no inference connection, exactly one peer: the main harness. The
wrapper dials out over CA-pinned TLS and executes the tool calls it receives.

``ToolHost`` is the host-side tool layer. It reuses the existing local logic --
``core.shell`` for shell_run and ``core.files`` for reading/searching -- so
remote behaviour matches the local tools. Paths are not restricted: the agent
may touch any folder the account it runs under can reach. The configured roots
are only start folders (the base for relative paths, the default shell cwd and
fs_search scope, what fs_list shows without a path).

Two client entry points:

- ``enroll_over_channel`` runs once, on first install: the host generates its own
  key + CSR (the private key never leaves the host), connects server-auth-only
  (it has no certificate yet), sends the one-time token and CSR, and saves the
  certificate the main harness signs.

- ``run_agent`` is the steady state: connect with mTLS using the saved
  certificate, announce the agent, then serve tool calls until the socket drops.

Error results are returned as ``(output, is_error)``; the main side maps the flag
onto the router's error accounting (Phase 3). The faithful root-resolution and
error-message details of the catalog tools are aligned there too.
"""
from __future__ import annotations

import os
import re
import ssl
from pathlib import Path

from websockets.asyncio.client import connect

from core import files, shell
from core.agent import ca, protocol

MAX_READ_CHARS = 10_500  # mirrors tools_server.fs_read; the router caps a result anyway
MAX_LIST = 300


class EnrollmentFailed(RuntimeError):
    """The main harness refused enrollment (bad/expired/used token, or a bad CSR)."""


# --- host-side tool layer --------------------------------------------------

class ToolHost:
    """Runs tools locally, anywhere on the host. Returns (output, is_error)."""

    def __init__(self, roots):
        self.roots = [Path(r).resolve() for r in roots if Path(r).is_dir()]
        self.jobs = shell.Jobs()  # background commands of this agent, found by run_id

    def tool_names(self) -> list[str]:
        return ["shell_run", "shell_output", "shell_stop", "fs_read", "fs_list", "fs_search"]

    def run(self, tool: str, args: dict | None) -> tuple[str, bool]:
        args = args or {}
        try:
            if tool == "fs_read":
                return self._fs_read(str(args.get("path", "")),
                                     int(args.get("max_chars", MAX_READ_CHARS)), int(args.get("offset", 0)))
            if tool == "fs_list":
                return self._fs_list(str(args.get("path", "")))
            if tool == "fs_search":
                return self._fs_search(str(args.get("pattern", "")), str(args.get("path", "")),
                                       bool(args.get("ignore_case", True)), int(args.get("max_results", 50)))
            if tool == "shell_run":
                return self._shell_run(str(args.get("command", "")), str(args.get("cwd", "")))
            if tool == "shell_output":
                return self._shell_output(args.get("run_id"), int(args.get("offset", 0)))
            if tool == "shell_stop":
                return self._shell_stop(args.get("run_id"))
            return (f"unknown tool: {tool}", True)
        except ValueError as e:
            return (str(e), True)
        except Exception as e:  # never let a tool crash the connection
            return (f"{e.__class__.__name__}: {e}", True)

    # path helpers
    def _base(self) -> Path:
        return self.roots[0] if self.roots else Path.home()

    def _resolve(self, path: str) -> Path:
        p = Path((path or "").strip() or ".").expanduser()
        return p.resolve() if p.is_absolute() else (self._base() / p).resolve()

    # tools
    def _fs_read(self, path: str, max_chars: int, offset: int) -> tuple[str, bool]:
        p = self._resolve(path)
        if p.is_dir():
            return (f"this is a folder, not a file: {path}", True)
        text, reason = files.read_text_file(str(p))
        if text is None:
            return (f"file skipped: {reason}", True)
        limit = max(1, min(int(max_chars), MAX_READ_CHARS))
        start = max(0, int(offset))
        if text and start >= len(text):
            return (f"offset {start} is past the end of the file (it has {len(text)} characters)", True)
        chunk = text[start:start + limit]
        end = start + len(chunk)
        if end < len(text):
            chunk += f"\n[...truncated; read from offset {end} for more]"
        return (chunk, False)

    def _fs_list(self, path: str) -> tuple[str, bool]:
        if not path.strip():
            if self.roots:
                return ("\n".join(f"{r.name}/  {r}" for r in self.roots), False)
            drives = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{d}:\\")]
            return ("\n".join(drives) if drives else str(Path.home()), False)
        p = self._resolve(path)
        if not p.exists():
            return (f"folder not found: {path}", True)
        if p.is_file():
            return (f"this is a file, not a folder: {path}", True)
        lines = []
        for entry in sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.is_dir():
                lines.append(entry.name + "/")
            else:
                try:
                    lines.append(f"{entry.name}  ({entry.stat().st_size} bytes)")
                except OSError:
                    lines.append(entry.name)
            if len(lines) >= MAX_LIST:
                lines.append(f"[...more than {MAX_LIST} entries, list truncated]")
                break
        return ("\n".join(lines) if lines else "(empty folder)", False)

    def _fs_search(self, pattern: str, path: str, ignore_case: bool, max_results: int) -> tuple[str, bool]:
        if not pattern:
            return ("empty search pattern", True)
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return (f"bad regular expression: {e}", True)
        targets = [self._resolve(path)] if path.strip() else list(self.roots)
        if not targets:
            return ("give a path to search in (no start folders are configured on this host)", True)
        results: list[str] = []
        cap = max(1, min(int(max_results), 500))
        for target in targets:
            if target.is_file():
                walk = [(str(target.parent), [], [target.name])]
            else:
                walk = os.walk(target)
            for dirpath, dirnames, filenames in walk:
                dirnames[:] = [d for d in dirnames if not files.is_ignored_dir(d)]
                for name in sorted(filenames):
                    if files.looks_binary_by_name(name):
                        continue
                    full = os.path.join(dirpath, name)
                    text, _ = files.read_text_file(full)
                    if text is None:
                        continue
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if rx.search(line):
                            results.append(f"{full}:{lineno}: {line.strip()[:200]}")
                            if len(results) >= cap:
                                return ("\n".join(results), False)
        return ("\n".join(results) if results else "no matches", False)

    def _shell_run(self, command: str, cwd: str) -> tuple[str, bool]:
        if not command.strip():
            return ("empty command", True)
        wd = str(self._resolve(cwd)) if cwd.strip() else str(self._base())
        job, err = self.jobs.start(command, wd)
        if job is None:
            return (err or "the command could not be started", True)
        job.wait(shell.WAIT)  # not done in time: it keeps running, the reply says so and gives the run_id
        return shell.render(job, 0)

    def _shell_output(self, run_id, offset: int) -> tuple[str, bool]:
        job = self.jobs.get(run_id)
        if job is None:
            return (f"there is no command with run_id={run_id} (the last {shell.KEEP_JOBS} are kept)", True)
        return shell.render(job, offset)

    def _shell_stop(self, run_id) -> tuple[str, bool]:
        job = self.jobs.get(run_id)
        if job is None:
            return (f"there is no command with run_id={run_id} (the last {shell.KEEP_JOBS} are kept)", True)
        if not job.stop():
            return (f"run_id={run_id} is not running ({shell.render(job, 0, 200)[0].splitlines()[0]})", False)
        return (f"run_id={run_id} was stopped. Output so far: shell_output with run_id={run_id}.", False)


# --- TLS client contexts ---------------------------------------------------

def _client_context(ca_cert_path: str, cert_path: str = "", key_path: str = "") -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(ca_cert_path)   # pin: trust only our own CA
    ctx.check_hostname = False                # CA pinning stands in for hostname checks here
    if cert_path and key_path:
        ctx.load_cert_chain(cert_path, key_path)
    return ctx


# --- enrollment (runs once) ------------------------------------------------

async def enroll_over_channel(url: str, ca_cert_path: str, token: str, agent_name: str,
                              out_key_path: str, out_cert_path: str) -> bytes:
    """Enroll with the main harness and save the issued key + certificate.

    Returns the certificate PEM. Raises EnrollmentFailed on refusal.
    """
    key_pem, csr_pem = ca.generate_csr(agent_name)
    ctx = _client_context(ca_cert_path)   # no client cert yet
    async with connect(url, ssl=ctx) as ws:
        await ws.send(protocol.enroll_request(token, csr_pem.decode("ascii"), agent_name))
        reply = protocol.decode(await ws.recv())
        if reply.get("type") != protocol.ENROLL_OK:
            raise EnrollmentFailed(reply.get("message", "enrollment refused"))
        cert_pem = reply["cert"].encode("ascii")
    Path(out_key_path).write_bytes(key_pem)
    Path(out_cert_path).write_bytes(cert_pem)
    return cert_pem


# --- steady state ----------------------------------------------------------

async def run_agent(url: str, ca_cert_path: str, cert_path: str, key_path: str,
                    agent_name: str, host: ToolHost):
    """Connect with mTLS and serve tool calls until the connection closes."""
    import asyncio

    ctx = _client_context(ca_cert_path, cert_path, key_path)
    async with connect(url, ssl=ctx) as ws:
        await ws.send(protocol.hello(agent_name))
        import platform
        await ws.send(protocol.capabilities(platform.system(), [str(r) for r in host.roots], host.tool_names()))
        async for raw in ws:
            try:
                msg = protocol.decode(raw)
            except protocol.ProtocolError:
                continue
            kind = msg.get("type")
            if kind == protocol.TOOL_CALL:
                output, is_error = await asyncio.to_thread(host.run, msg.get("tool", ""), msg.get("args") or {})
                await ws.send(protocol.tool_result(msg.get("id", ""), output, is_error))
            elif kind == protocol.PING:
                await ws.send(protocol.pong())
