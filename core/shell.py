"""Runs PowerShell commands for the shell_run tool (no Qt).

The command is passed with -EncodedCommand (no quote escaping at all); stdout+stderr output
is collected as UTF-8. A command is a ``Job``: a reader thread collects its output, so a command that does not
finish in time is not killed but keeps running in the background and is followed with ``render`` (the shell_output
tool) or stopped with ``Job.stop`` (the shell_stop tool). A job that outlives ``MAX_LIFETIME`` is killed together
with its process tree. ``run`` is the old synchronous call (wait, then kill) on top of the same Job.
"""
import base64
import codecs
import itertools
import os
import shutil
import subprocess
import threading
import time

TIMEOUT = 60.0                # run(): the synchronous limit, then the command is killed
WAIT = float(os.environ.get("SAI_SHELL_WAIT") or 45.0)  # shell_run waits this long, then leaves the command in the background (the env is for tests)
MAX_LIFETIME = 3600.0         # a background command is killed after this long
KEEP_JOBS = 20                # finished jobs kept for shell_output (running ones are always kept)
MAX_OUTPUT_CHARS = 1_000_000  # stored output; there is no point keeping more, the tail beyond is dropped
CHUNK_CHARS = 10_500          # one reply; the router cuts a tool result to 12,000, and the offset hint must fit
DROPPED_NOTE = "\n[the rest of the output was dropped: over 1,000,000 characters]"

# All streams are merged into stdout inside PowerShell itself: otherwise with -EncodedCommand errors and progress
# arrive in stderr as CLIXML (XML instead of text). The exit code is taken from the last external program.
# UTF8 without BOM: [Text.Encoding]::UTF8 in $OutputEncoding adds a BOM to text piped into an external program
# (`'Test2' | powershell -File x.ps1` received "´╗┐Test2").
_PRELUDE = ("$ProgressPreference = 'SilentlyContinue'; "
            "$u8 = New-Object System.Text.UTF8Encoding($false); [Console]::OutputEncoding = $u8; $OutputEncoding = $u8; ")
_EXIT = "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"


def powershell_exe() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def _spawn(command: str, cwd: str) -> tuple[subprocess.Popen | None, str]:
    exe = powershell_exe()
    if exe is None:
        return None, "PowerShell not found in PATH"
    script = _PRELUDE + "& {\n" + command + "\n} *>&1 | Out-String -Stream; " + _EXIT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        return None, f"could not start PowerShell ({e.__class__.__name__})"
    return proc, ""


def _kill_tree(proc: subprocess.Popen):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


class Job:
    """One running or finished command. ``state``: running | done | timeout | stopped."""

    def __init__(self, job_id: int, command: str, cwd: str, proc: subprocess.Popen, lifetime: float = MAX_LIFETIME):
        self.id = job_id
        self.command = command
        self.cwd = cwd
        self.lifetime = lifetime
        self.state = "running"
        self.code: int | None = None
        self._proc = proc
        self._parts: list[str] = []
        self._len = 0
        self._dropped = False
        self._started = time.monotonic()
        self._ended: float | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        threading.Thread(target=self._read, daemon=True, name=f"shell-read-{job_id}").start()
        threading.Thread(target=self._watch, daemon=True, name=f"shell-watch-{job_id}").start()

    # --- background threads -------------------------------------------------

    def _append(self, text: str):
        if not text:
            return
        with self._lock:
            if self._dropped:
                return
            room = MAX_OUTPUT_CHARS - self._len
            if len(text) > room:
                text, self._dropped = text[:max(0, room)], True
            self._parts.append(text)
            self._len += len(text)

    def _read(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""  # a trailing \r waits for the next chunk, so "\r\n" split across chunks still becomes "\n"
        try:
            while True:
                data = self._proc.stdout.read1(65536)
                if not data:
                    break
                text = pending + decoder.decode(data)
                pending = "\r" if text.endswith("\r") else ""
                self._append((text[:-1] if pending else text).replace("\r\n", "\n"))
            self._append((pending + decoder.decode(b"", final=True)).replace("\r\n", "\n"))
        except (OSError, ValueError):
            pass  # the pipe was closed under us
        try:
            code = self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        with self._lock:
            self.code = code
            if self.state == "running":
                self.state = "done"
            self._ended = time.monotonic()
        self._done.set()

    def _watch(self):
        if not self._done.wait(self.lifetime):
            self.stop("timeout")

    # --- control ------------------------------------------------------------

    def wait(self, timeout: float) -> bool:
        """Waits until the command finishes; True if it did."""
        return self._done.wait(timeout)

    def stop(self, state: str = "stopped") -> bool:
        """Kills the process tree. False if the job was no longer running."""
        with self._lock:
            if self.state != "running":
                return False
            self.state = state
        _kill_tree(self._proc)
        if not self._done.wait(5):
            try:
                self._proc.kill()
            except OSError:
                pass
            self._done.wait(2)
        return True

    def snapshot(self) -> dict:
        with self._lock:
            text = "".join(self._parts) + (DROPPED_NOTE if self._dropped else "")
            end = self._ended if self._ended is not None else time.monotonic()
            return {"state": self.state, "code": self.code, "text": text, "elapsed": end - self._started}


class Jobs:
    """The commands of one session, numbered from 1 (the run_id the model sees)."""

    def __init__(self, keep: int = KEEP_JOBS, lifetime: float = MAX_LIFETIME):
        self.keep = keep
        self.lifetime = lifetime
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, command: str, cwd: str) -> tuple[Job | None, str]:
        """Starts a command. Returns (job, "") or (None, why it did not start)."""
        proc, err = _spawn(command, cwd)
        if proc is None:
            return None, err
        with self._lock:
            job = Job(next(self._ids), command, cwd, proc, self.lifetime)
            self._jobs[job.id] = job
            for old in [k for k, j in self._jobs.items() if k <= job.id - self.keep and j.state != "running"]:
                del self._jobs[old]
        return job, ""

    def get(self, run_id) -> Job | None:
        try:
            return self._jobs.get(int(run_id))
        except (TypeError, ValueError):
            return None


def render(job: Job, offset: int = 0, limit: int = CHUNK_CHARS, tool: str = "shell_output") -> tuple[str, bool]:
    """The reply for shell_run / shell_output: a status line, a chunk of the output from ``offset`` and a hint
    saying how to continue. Returns (text, is_error); only an offset past the end is an error."""
    snap = job.snapshot()
    text, state = snap["text"], snap["state"]
    if state == "running":
        status = f"still running, {snap['elapsed']:.0f} s"
    elif state == "done":
        status = f"exit code {snap['code']}"
    elif state == "timeout":
        status = f"TIMEOUT: the command was killed after {job.lifetime:g} s"
    else:
        status = "stopped by shell_stop"
    header = f"[{status}; folder {job.cwd}; run_id={job.id}]\n"
    start = max(0, int(offset))
    if start > len(text):
        return f"offset {start} is past the end of the output (it has {len(text)} characters)", True
    chunk = text[start:start + max(200, limit - len(header) - 250)]
    end = start + len(chunk)
    if end < len(text):
        tail = (f"\n[truncated: showing characters {start}–{end} of {len(text)}; to continue: "
                f"{tool} with run_id={job.id}, offset={end}]")
    elif state == "running":
        tail = (f"\n[still running: to follow it use {tool} with run_id={job.id}, offset={end}; "
                f"to stop it use shell_stop with run_id={job.id}]")
    else:
        tail = f"\n[shown from character {start} to the end, {len(text)} in total]" if start else ""
    if chunk:
        body = chunk
    elif state == "running":
        body = "(no new output yet)"
    else:
        body = "(no output)" if not text else "(no more output)"
    return header + body + tail, False


def run(command: str, cwd: str, timeout: float = TIMEOUT) -> tuple[int | None, str, bool]:
    """Synchronous run: returns (exit code, output, timed_out). The code is None if PowerShell did not start
    or the command was killed on timeout."""
    job, err = Jobs(keep=1).start(command, cwd)
    if job is None:
        return None, err, False
    timed_out = not job.wait(timeout)
    if timed_out:
        job.stop("timeout")
    snap = job.snapshot()
    return (None if timed_out else snap["code"]), snap["text"], timed_out
