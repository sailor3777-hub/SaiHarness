"""Rendering JS pages: runs web_render.py (Qt WebEngine) as a separate process and returns the HTML."""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "web_render.py"
PROCESS_GRACE = 15.0   # margin beyond the page timeout for Chromium to start and shut down


def render_html(url: str, timeout: float = 30.0, allow_private: bool = False) -> str:
    cmd = [sys.executable, str(SCRIPT), url, str(timeout)] + (["--allow-private"] if allow_private else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + PROCESS_GRACE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("the renderer did not answer in time") from None
    out = proc.stdout.decode("utf-8", "replace")
    for line in reversed(out.splitlines()):
        if line.startswith("SAIJSON:"):
            data = json.loads(line[len("SAIJSON:"):])
            if "html" in data:
                return data["html"]
            raise RuntimeError(data.get("error", "unknown error"))
    raise RuntimeError(f"the renderer exited without a result (code {proc.returncode})")
