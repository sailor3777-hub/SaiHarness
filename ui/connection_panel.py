from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QListWidget, QListWidgetItem,
    QLineEdit, QComboBox, QPushButton, QLabel, QMessageBox,
)

from core import db
from core.connection import KINDS, normalize_url
from worker import ModelListRequest


class ConnectionPanel(QWidget):
    """Список подключений к серверам моделей и форма их настройки."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self._editing: str | None = None

        self.list = QListWidget()
        self.edit_btn = QPushButton("Изменить")
        self.delete_btn = QPushButton("Удалить")
        self.new_btn = QPushButton("Новое")
        buttons = QHBoxLayout()
        buttons.addWidget(self.new_btn)
        buttons.addWidget(self.edit_btn)
        buttons.addWidget(self.delete_btn)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Имя подключения")
        self.kind = QComboBox()
        self.kind.addItems(KINDS)
        self.url = QLineEdit()
        self.url.setPlaceholderText("URL или IP, например 192.168.1.5:1234")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("API-ключ (необязательно)")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.save_btn = QPushButton("Сохранить")
        self.check_btn = QPushButton("Проверить")
        self.check_req = ModelListRequest(self)

        form_buttons = QHBoxLayout()
        form_buttons.addWidget(self.check_btn)
        form_buttons.addWidget(self.save_btn)

        form = QFormLayout()
        form.addRow("Имя", self.name)
        form.addRow("Тип", self.kind)
        form.addRow("URL / IP", self.url)
        form.addRow("Ключ", self.api_key)
        form.addRow(self.status)
        form.addRow(form_buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        layout.addLayout(form)

        self.new_btn.clicked.connect(self._new)
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn.clicked.connect(self._delete)
        self.save_btn.clicked.connect(self._save)
        self.check_btn.clicked.connect(self._check)
        self.check_req.succeeded.connect(self._on_check_ok)
        self.check_req.failed.connect(lambda msg: self.status.setText(f"✗ {msg}"))
        self.check_req.busy_changed.connect(self._on_check_busy)
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())
        self.list.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.reload()

    def reload(self, select: str | None = None):
        self._rows = {r["name"]: r for r in db.list_connections()}
        current = select or self._current_name()
        self.list.blockSignals(True)
        self.list.clear()
        for name, r in self._rows.items():
            item = QListWidgetItem(f"{name} — {r['url']}")
            item.setData(Qt.UserRole, name)
            self.list.addItem(item)
            if name == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self._update_buttons()

    def _current_name(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _update_buttons(self):
        has = self._current_name() is not None
        self.edit_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)

    def _new(self):
        self._editing = None
        self.name.clear()
        self.url.clear()
        self.api_key.clear()
        self.status.setText("")
        self.list.clearSelection()
        self.list.setCurrentItem(None)

    def _edit(self):
        name = self._current_name()
        if name is None:
            return
        r = self._rows[name]
        self._editing = name
        self.name.setText(r["name"])
        self.kind.setCurrentText(r["kind"])
        self.url.setText(r["url"])
        self.api_key.setText(r["api_key"])
        self.status.setText(f"Редактирование: {name}")

    def _delete(self):
        name = self._current_name()
        if name is None:
            return
        answer = QMessageBox.question(self, "Удалить подключение", f"Удалить подключение «{name}»?")
        if answer != QMessageBox.Yes:
            return
        db.delete_connection(name)
        if self._editing == name:
            self._new()
        self.reload()
        self.changed.emit()

    def _check(self):
        """Проверяет адрес и ключ из формы (без сохранения) запросом списка моделей."""
        try:
            url = normalize_url(self.url.text(), KINDS[self.kind.currentText()])
        except ValueError as e:
            self.status.setText(f"✗ {e}")
            return
        self.status.setText(f"Проверяю {url} …")
        self.check_req.start(url, self.api_key.text().strip())

    def _on_check_ok(self, models: list):
        self.status.setText(f"✓ Подключено, моделей на сервере: {len(models)}")

    def _on_check_busy(self, busy: bool):
        self.check_btn.setEnabled(not busy)
        self.check_btn.setText("Проверяю…" if busy else "Проверить")

    def wait_threads(self):
        self.check_req.wait()

    def _save(self):
        name = self.name.text().strip()
        if not name:
            self.status.setText("Нужно имя подключения")
            return
        kind = self.kind.currentText()
        try:
            url = normalize_url(self.url.text(), KINDS[kind])
            db.save_connection(name, kind, url, self.api_key.text().strip(), old_name=self._editing)
        except ValueError as e:
            self.status.setText(str(e))
            return
        self._editing = name
        self.url.setText(url)
        self.status.setText(f"Сохранено: {name}")
        self.reload(select=name)
        self.changed.emit()
