from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QListWidget, QListWidgetItem, QLabel,
    QMenu, QInputDialog, QMessageBox, QComboBox,
)

from core import db
from ui import themes


class ChatList(QWidget):
    chat_selected = Signal(object)  # int | None
    new_requested = Signal()
    chat_deleted = Signal(int)
    chat_renamed = Signal(int, str)
    theme_requested = Signal(str)  # ключ темы, выбранной пользователем

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        title = QLabel("Чаты")
        title.setObjectName("title")
        self.theme_box = QComboBox()
        self.theme_box.setToolTip("Оформление окна")
        for theme in themes.THEMES.values():
            self.theme_box.addItem(theme.title, theme.key)
        self.theme_box.setCurrentIndex(self.theme_box.findData(themes.current().key))
        self.new_btn = QPushButton("+ Новый чат")
        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        layout.addWidget(title)
        layout.addWidget(self.theme_box)
        layout.addWidget(self.new_btn)
        layout.addWidget(self.list)

        self.new_btn.clicked.connect(self.new_requested)
        self.theme_box.activated.connect(lambda i: self.theme_requested.emit(self.theme_box.itemData(i)))
        themes.bus.changed.connect(self._sync_theme_box)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.itemDoubleClicked.connect(self._rename)
        self.list.customContextMenuRequested.connect(self._menu)

    def _sync_theme_box(self, theme):
        """Тему могли сменить не из селектора: показываем актуальную."""
        self.theme_box.blockSignals(True)
        self.theme_box.setCurrentIndex(self.theme_box.findData(theme.key))
        self.theme_box.blockSignals(False)

    def current_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def reload(self, select_id: int | None = None):
        """Перечитать список из БД. Выбирает select_id, иначе сохраняет текущий выбор."""
        keep = select_id if select_id is not None else self.current_id()
        self.list.blockSignals(True)
        self.list.clear()
        target = None
        for row in db.list_chats():
            item = QListWidgetItem(row["title"])
            item.setData(Qt.UserRole, row["id"])
            self.list.addItem(item)
            if row["id"] == keep:
                target = item
        if target is not None:
            self.list.setCurrentItem(target)
        self.list.blockSignals(False)

    def _on_current_changed(self, current, _previous):
        self.chat_selected.emit(current.data(Qt.UserRole) if current else None)

    def _menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        rename = menu.addAction("Переименовать")
        delete = menu.addAction("Удалить")
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen is rename:
            self._rename(item)
        elif chosen is delete:
            self._delete(item)

    def _rename(self, item):
        chat_id = item.data(Qt.UserRole)
        title, ok = QInputDialog.getText(self, "Переименовать чат", "Название:", text=item.text())
        title = title.strip()
        if ok and title:
            db.rename_chat(chat_id, title)
            self.reload()
            self.chat_renamed.emit(chat_id, title)

    def _delete(self, item):
        chat_id = item.data(Qt.UserRole)
        answer = QMessageBox.question(self, "Удалить чат", f"Удалить чат «{item.text()}» вместе с историей?")
        if answer != QMessageBox.Yes:
            return
        was_current = chat_id == self.current_id()
        db.delete_chat(chat_id)
        self.chat_deleted.emit(chat_id)
        if was_current:
            self.reload(select_id=-1)  # ничего не выбрано
            first = self.list.item(0)
            if first is not None:
                self.list.setCurrentItem(first)  # эмитит chat_selected
            else:
                self.chat_selected.emit(None)
        else:
            self.reload()
