"""Отрисовка JS-страниц: запускает web_render.py (Qt WebEngine) отдельным процессом и возвращает HTML."""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "web_render.py"
PROCESS_GRACE = 15.0   # запас сверх таймаута страницы на запуск и завершение Chromium


def render_html(url: str, timeout: float = 30.0, allow_private: bool = False) -> str:
    cmd = [sys.executable, str(SCRIPT), url, str(timeout)] + (["--allow-private"] if allow_private else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + PROCESS_GRACE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("рендерер не ответил вовремя") from None
    out = proc.stdout.decode("utf-8", "replace")
    for line in reversed(out.splitlines()):
        if line.startswith("SAIJSON:"):
            data = json.loads(line[len("SAIJSON:"):])
            if "html" in data:
                return data["html"]
            raise RuntimeError(data.get("error", "неизвестная ошибка"))
    raise RuntimeError(f"рендерер завершился без результата (код {proc.returncode})")
