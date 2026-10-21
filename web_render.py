"""Renders a JavaScript page with Qt WebEngine in a separate process (started by core/web/render.py).

Usage: python web_render.py <url> [timeout_sec] [--allow-private]
Result: one line "SAIJSON:" + JSON {"html": ...} or {"error": ...}; any other Qt output is ignored.
A separate process because tools_server.py runs without Qt and without an event loop, while WebEngine needs a GUI thread.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-logging")
sys.path.insert(0, str(Path(__file__).resolve().parent))

SETTLE_MS = 1200   # pause after loading: scripts get time to finish drawing the page


def emit(payload: dict):
    sys.stdout.buffer.write(("SAIJSON:" + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow_private = "--allow-private" in sys.argv
    if not args:
        emit({"error": "no address given"})
        return 2
    url = args[0]
    timeout_ms = int(float(args[1]) * 1000) if len(args) > 1 else 30_000

    from core.web.safety import UnsafeUrl, check_url
    try:
        check_url(url, allow_private)
    except UnsafeUrl as e:
        emit({"error": str(e)})
        return 1

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
    from PySide6.QtWidgets import QApplication

    class Guard(QWebEngineUrlRequestInterceptor):
        """Blocks the page's requests (including images, scripts, iframes) to non-public addresses."""

        def __init__(self):
            super().__init__()
            self.verdicts: dict[str, bool] = {}

        def interceptRequest(self, info):
            target = info.requestUrl()
            if target.scheme() in ("data", "blob", "about"):
                return
            key = target.host()
            if key not in self.verdicts:
                try:
                    check_url(target.toString(), allow_private)
                    self.verdicts[key] = True
                except UnsafeUrl:
                    self.verdicts[key] = False
            if not self.verdicts[key]:
                info.block(True)

    app = QApplication(sys.argv[:1])
    profile = QWebEngineProfile()          # unnamed: an in-memory profile, no cookies or cache written to disk
    guard = Guard()
    profile.setUrlRequestInterceptor(guard)
    page = QWebEnginePage(profile)
    result: dict = {}

    def finish(payload: dict):
        if not result:
            result.update(payload)
            app.quit()

    def on_load(ok: bool):
        if not ok:
            finish({"error": "the page did not load"})
            return
        QTimer.singleShot(SETTLE_MS, lambda: page.toHtml(lambda html: finish({"html": html})))

    page.loadFinished.connect(on_load)
    QTimer.singleShot(timeout_ms, lambda: finish({"error": f"timeout {timeout_ms // 1000} s"}))
    page.load(QUrl(url))
    app.exec()
    emit(result or {"error": "no result"})
    page.deleteLater()
    del page, profile
    return 0 if "html" in result else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)   # WebEngine sometimes hangs on a normal exit; the result has already been printed
