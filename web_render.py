"""Отрисовка страницы с JavaScript через Qt WebEngine в отдельном процессе (запускается core/web/render.py).

Вызов: python web_render.py <url> [таймаут_сек] [--allow-private]
Результат: одна строка "SAIJSON:" + JSON {"html": ...} или {"error": ...}; остальной вывод Qt игнорируется.
Процесс отдельный, потому что tools_server.py работает без Qt и без цикла событий, а WebEngine нужен GUI-поток.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-logging")
sys.path.insert(0, str(Path(__file__).resolve().parent))

SETTLE_MS = 1200   # пауза после загрузки: скрипты успевают дорисовать страницу


def emit(payload: dict):
    sys.stdout.buffer.write(("SAIJSON:" + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow_private = "--allow-private" in sys.argv
    if not args:
        emit({"error": "не указан адрес"})
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
        """Блокирует запросы страницы (в том числе картинки, скрипты, iframe) к непубличным адресам."""

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
    profile = QWebEngineProfile()          # без имени: профиль в памяти, куки и кэш на диск не пишутся
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
            finish({"error": "страница не загрузилась"})
            return
        QTimer.singleShot(SETTLE_MS, lambda: page.toHtml(lambda html: finish({"html": html})))

    page.loadFinished.connect(on_load)
    QTimer.singleShot(timeout_ms, lambda: finish({"error": f"таймаут {timeout_ms // 1000} с"}))
    page.load(QUrl(url))
    app.exec()
    emit(result or {"error": "нет результата"})
    page.deleteLater()
    del page, profile
    return 0 if "html" in result else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)   # WebEngine иногда зависает при штатном выходе; результат уже выведен
