"""Runs a PowerShell command for the shell_run tool (no Qt).

The command is passed with -EncodedCommand (no quote escaping at all); stdout+stderr output
is collected in full as UTF-8. On timeout the whole process tree is killed.
"""
import base64
import shutil
import subprocess

TIMEOUT = 60.0
MAX_OUTPUT_CHARS = 1_000_000  # stored output; there is no point keeping more, the tail beyond is dropped

# All streams are merged into stdout inside PowerShell itself: otherwise with -EncodedCommand errors and progress
# arrive in stderr as CLIXML (XML instead of text). The exit code is taken from the last external program.
# UTF8 without BOM: [Text.Encoding]::UTF8 in $OutputEncoding adds a BOM to text piped into an external program
# (`'Test2' | powershell -File x.ps1` received "´╗┐Test2").
_PRELUDE = ("$ProgressPreference = 'SilentlyContinue'; "
            "$u8 = New-Object System.Text.UTF8Encoding($false); [Console]::OutputEncoding = $u8; $OutputEncoding = $u8; ")
_EXIT = "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"


def powershell_exe() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def run(command: str, cwd: str, timeout: float = TIMEOUT) -> tuple[int | None, str, bool]:
    """Returns (exit code, output, timed_out). The code is None if PowerShell did not start."""
    exe = powershell_exe()
    if exe is None:
        return None, "PowerShell not found in PATH", False
    script = _PRELUDE + "& {\n" + command + "\n} *>&1 | Out-String -Stream; " + _EXIT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        return None, f"could not start PowerShell ({e.__class__.__name__})", False
    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            raw, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            raw = b""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n[the rest of the output was dropped: over 1,000,000 characters]"
    return (None if timed_out else proc.returncode), text, timed_out
