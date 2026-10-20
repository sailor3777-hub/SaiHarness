import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    QMessageBox,
)

from core import db
from core.web import searxng
from core.web import settings as web_settings
from core.web.index import WebIndex
from worker import AsyncCall, CrawlThread

DEFAULT_DEPTH = 2
DEFAULT_MAX_PAGES = 200


def _probe_searxng(url: str) -> int:
    """Пробный запрос: сколько результатов вернул SearXNG (ошибка — SearxError)."""
    return len(searxng.search(url, "test", 1, timeout=6.0))


def _fmt_time(value: str) -> str:
    return value.replace("T", " ") if value else "—"


class WebPanel(QWidget):
    """Сайты для локального поискового индекса (инструмент web_search). Страницы обходятся и сохраняются в базе."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self._thread: CrawlThread | None = None
        self._crawling_id: int | None = None
        self._queue: list[int] = []

        self.searx_url = QLineEdit()
        self.searx_url.setPlaceholderText("Адрес вашего SearXNG, например http://192.168.0.50:8080")
        self.searx_check = QPushButton("Проверить")
        self.searx_status = QLabel("")
        self.searx_status.setWordWrap(True)
        self.searx_probe = AsyncCall(_probe_searxng, self)
        self.searx_probe.succeeded.connect(self._on_searx_ok)
        self.searx_probe.failed.connect(self._on_searx_failed)
        self.searx_probe.busy_changed.connect(lambda busy: self.searx_check.setEnabled(not busy))

        self.url = QLineEdit()
        self.url.setPlaceholderText("Адрес сайта, например docs.python.org")
        self.depth = QSpinBox()
        self.depth.setRange(0, 5)
        self.depth.setValue(DEFAULT_DEPTH)
        self.depth.setToolTip("Сколько переходов по ссылкам от стартовой страницы (0 — только она сама).")
        self.max_pages = QSpinBox()
        self.max_pages.setRange(1, 5000)
        self.max_pages.setValue(DEFAULT_MAX_PAGES)
        self.max_pages.setToolTip("Предел страниц одного сайта в индексе.")
        self.add_btn = QPushButton("Добавить и обойти")
        self.refresh_btn = QPushButton("Обновить")
        self.stop_btn = QPushButton("Остановить")
        self.remove_btn = QPushButton("Удалить")

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Сайт", "Страниц", "Обход", "Состояние"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setColumnWidth(0, 200)
        self.info = QLabel("")
        self.info.setWordWrap(True)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Глубина"))
        opts.addWidget(self.depth)
        opts.addWidget(QLabel("Страниц"))
        opts.addWidget(self.max_pages)
        opts.addStretch(1)
        row = QHBoxLayout()
        for b in (self.refresh_btn, self.stop_btn, self.remove_btn):
            row.addWidget(b)

        searx_row = QHBoxLayout()
        searx_row.addWidget(self.searx_url, 1)
        searx_row.addWidget(self.searx_check)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("SearXNG: поиск по интернету (инструмент web_search_internet)"))
        layout.addLayout(searx_row)
        layout.addWidget(self.searx_status)
        layout.addWidget(QLabel("Сайты для поиска (индекс хранится локально; web_search ищет только в нём)"))
        layout.addWidget(self.url)
        layout.addLayout(opts)
        layout.addWidget(self.add_btn)
        layout.addWidget(self.tree, 1)
        layout.addLayout(row)
        layout.addWidget(self.info)

        self.searx_url.editingFinished.connect(self._save_searx)
        self.searx_check.clicked.connect(self._check_searx)
        self._load_searx()
        self.url.returnPressed.connect(self.add_site)
        self.add_btn.clicked.connect(self.add_site)
        self.refresh_btn.clicked.connect(self.refresh_selected)
        self.stop_btn.clicked.connect(self.stop)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.reload()

    # --- данные ---

    def _index(self) -> WebIndex:
        return WebIndex(sqlite3.connect(db.DB_PATH, timeout=30))

    def reload(self):
        keep = {self.tree.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(i).isSelected()}
        self.tree.clear()
        db.connect().close()  # гарантирует, что файл базы существует
        index = self._index()
        try:
            for s in index.sources():
                state = "идёт обход…" if s["id"] == self._crawling_id else (
                    "в очереди" if s["id"] in self._queue else (s["status"] or "не обходился"))
                item = QTreeWidgetItem([s["url"], str(s["pages"]), _fmt_time(s["crawled_at"]), state])
                item.setData(0, Qt.UserRole, s["id"])
                item.setToolTip(0, f"Глубина {s['depth']}, до {s['max_pages']} страниц")
                self.tree.addTopLevelItem(item)
                item.setSelected(s["id"] in keep)
        finally:
            index.conn.close()
        self._update_buttons()

    def _selected_ids(self) -> list[int]:
        return [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()]

    def _update_buttons(self):
        has = bool(self._selected_ids())
        crawling = self._crawling_id is not None
        self.refresh_btn.setEnabled(has)
        self.remove_btn.setEnabled(has and not crawling)
        self.stop_btn.setEnabled(crawling)

    # --- SearXNG ---

    def _load_searx(self):
        db.connect().close()
        conn = sqlite3.connect(db.DB_PATH, timeout=30)
        try:
            self.searx_url.setText(web_settings.get(conn, web_settings.SEARXNG_URL))
        finally:
            conn.close()

    def _save_searx(self) -> bool:
        try:
            value = searxng.normalize_url(self.searx_url.text())
        except ValueError as e:
            self.searx_status.setText(str(e))
            return False
        self.searx_url.setText(value)
        conn = sqlite3.connect(db.DB_PATH, timeout=30)
        try:
            web_settings.put(conn, web_settings.SEARXNG_URL, value)
        finally:
            conn.close()
        return True

    def _check_searx(self):
        if not self._save_searx():
            return
        if not self.searx_url.text():
            self.searx_status.setText("Адрес не задан.")
            return
        self.searx_status.setText("Проверяю…")
        self.searx_probe.start(self.searx_url.text())

    def _on_searx_ok(self, count: int):
        self.searx_status.setText(f"Работает: тестовый запрос вернул результатов — {count}." if count else
                                  "Отвечает, но результатов на тестовый запрос нет (проверь включённые поисковые системы).")

    def _on_searx_failed(self, message: str):
        self.searx_status.setText(message)

    # --- действия ---

    def add_site(self):
        text = self.url.text().strip()
        if not text:
            return
        index = self._index()
        try:
            source_id = index.add_source(text, self.depth.value(), self.max_pages.value())
        except ValueError as e:
            self.info.setText(str(e))
            return
        finally:
            index.conn.close()
        self.url.clear()
        self.changed.emit()
        self._enqueue([source_id])

    def refresh_selected(self):
        self._enqueue(self._selected_ids())

    def remove_selected(self):
        ids = self._selected_ids()
        if not ids or self._crawling_id is not None:
            return
        if QMessageBox.question(self, "Удалить сайты из индекса?", f"Сайтов: {len(ids)}. Собранные страницы будут удалены.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        index = self._index()
        try:
            for i in ids:
                index.remove_source(i)
        finally:
            index.conn.close()
        self.info.setText("")
        self.reload()
        self.changed.emit()

    def stop(self):
        self._queue.clear()
        if self._thread is not None:
            self._thread.stop()
            self.info.setText("Останавливаю…")

    # --- обход ---

    def _enqueue(self, ids: list[int]):
        for i in ids:
            if i != self._crawling_id and i not in self._queue:
                self._queue.append(i)
        self.reload()
        if self._thread is None:
            self._next()

    def _next(self):
        if not self._queue:
            self._thread, self._crawling_id = None, None
            self.reload()
            return
        self._crawling_id = self._queue.pop(0)
        t = CrawlThread(self._crawling_id, str(db.DB_PATH), self)
        t.progress.connect(lambda n, url: self.info.setText(f"Сохранено страниц: {n}\n{url}"))
        t.finished_ok.connect(self._on_done)
        t.failed.connect(self._on_failed)
        t.finished.connect(self._on_thread_finished)
        self._thread = t
        self.reload()
        t.start()

    def _on_done(self, result: dict):
        errors = result["errors"]
        text = f"Готово: в индексе страниц сайта — {result['pages']}."
        if result["stopped"]:
            text = "Обход остановлен."
        if errors:
            text += f" Не загрузилось: {len(errors)} (первая: {errors[0][:160]})"
        self.info.setText(text)
        self.changed.emit()

    def _on_failed(self, message: str):
        self.info.setText(message)

    def _on_thread_finished(self):
        t, self._thread = self._thread, None
        if t is not None:
            t.deleteLater()
        self._crawling_id = None
        self._next()

    def wait_threads(self):
        self.searx_probe.wait()
        self._queue.clear()
        if self._thread is not None:
            self._thread.stop()
            self._thread.wait(8000)
