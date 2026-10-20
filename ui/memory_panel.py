from contextlib import closing

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core import db
from core.memory import Memory, MemoryInputError

ID_ROLE = Qt.UserRole
ALL_TOPICS = "Все темы"
SEARCH_LIMIT = 200


class MemoryPanel(QWidget):
    """Долгосрочная память агента: просмотр, поиск, правка и удаление записей без участия модели."""

    def __init__(self):
        super().__init__()
        self._loading = False

        self.query = QLineEdit()
        self.query.setPlaceholderText("Поиск по памяти")
        self.query.setClearButtonEnabled(True)
        self.topic_filter = QComboBox()
        self.refresh_btn = QPushButton("Обновить")

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Тема", "Заголовок", "Обновлено"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 110)
        self.tree.setColumnWidth(1, 170)
        self.info = QLabel("")
        self.info.setWordWrap(True)

        self.topic = QLineEdit()
        self.topic.setPlaceholderText("Тема")
        self.title = QLineEdit()
        self.title.setPlaceholderText("Заголовок")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Что запомнить: самодостаточный текст, понятный без исходного разговора")
        self.new_btn = QPushButton("Новая запись")
        self.save_btn = QPushButton("Сохранить")
        self.delete_btn = QPushButton("Удалить")
        self.status = QLabel("")
        self.status.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(self.topic_filter)
        top.addWidget(self.refresh_btn)
        buttons = QHBoxLayout()
        for button in (self.new_btn, self.save_btn, self.delete_btn):
            buttons.addWidget(button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.tree, 3)
        layout.addWidget(self.info)
        layout.addWidget(self.topic)
        layout.addWidget(self.title)
        layout.addWidget(self.text, 2)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

        self.query.textChanged.connect(lambda _t: self._fill_list())
        self.topic_filter.currentIndexChanged.connect(lambda _i: self._fill_list())
        self.refresh_btn.clicked.connect(self.reload)
        self.tree.currentItemChanged.connect(lambda item, _prev: self._show(item))
        self.new_btn.clicked.connect(self._new)
        self.save_btn.clicked.connect(self._save)
        self.delete_btn.clicked.connect(self._delete)
        self.reload()

    def showEvent(self, event):
        super().showEvent(event)
        self.reload()  # записи добавляет модель из другого процесса: при открытии вкладки читаем заново

    # --- список ---

    def reload(self):
        """Перечитывает темы и записи из базы, сохраняя фильтр и выбранную запись."""
        self._load_topics(keep_filter=True)
        self._fill_list(select=self.current_id())

    def _load_topics(self, keep_filter: bool):
        with closing(db.connect()) as conn:
            topics = Memory(conn).topics()
        keep = self.topic_filter.currentData() if keep_filter else None
        self._loading = True
        self.topic_filter.clear()
        self.topic_filter.addItem(ALL_TOPICS, None)
        for name, count in topics:
            self.topic_filter.addItem(f"{name} ({count})", name)
        self.topic_filter.setCurrentIndex(max(self.topic_filter.findData(keep), 0) if keep else 0)
        self._loading = False

    def _fill_list(self, select: int | None = None):
        if self._loading:
            return
        topic = self.topic_filter.currentData()
        query = self.query.text().strip()
        with closing(db.connect()) as conn:
            mem = Memory(conn)
            if query:
                rows = mem.search(query, topic, SEARCH_LIMIT)
            else:
                rows = [{"id": r["id"], "topic": r["topic"], "title": r["title"], "updated_at": r["updated_at"]}
                        for r in mem.list(topic)]
            total = mem.count()
        self.tree.blockSignals(True)
        self.tree.clear()
        chosen = None
        for r in rows:
            item = QTreeWidgetItem([r["topic"], r["title"], r["updated_at"][:10]])
            item.setData(0, ID_ROLE, r["id"])
            self.tree.addTopLevelItem(item)
            if r["id"] == select:
                chosen = item
        self.tree.blockSignals(False)
        shown = f"показано {len(rows)} из {total}" if (query or topic) else f"записей: {total}"
        self.info.setText(shown if total else "Память пуста. Попроси агента «запомни это» (нужны инструменты memory_* "
                                              "во вкладке «Тулы») или добавь запись здесь.")
        if chosen is not None:
            self.tree.blockSignals(True)  # без перечитывания: в редакторе может быть несохранённый текст
            self.tree.setCurrentItem(chosen)
            self.tree.blockSignals(False)
        elif select is not None:
            self._new()  # выбранной записи больше нет (удалена или отфильтрована)
        self._update_buttons()

    def current_id(self) -> int | None:
        item = self.tree.currentItem()
        return item.data(0, ID_ROLE) if item is not None else None

    # --- редактор ---

    def _show(self, item):
        if item is None:
            return
        with closing(db.connect()) as conn:
            row = Memory(conn).get(item.data(0, ID_ROLE))
        if row is None:
            self.reload()
            return
        self.topic.setText(row["topic"])
        self.title.setText(row["title"])
        self.text.setPlainText(row["text"])
        self.status.setText(f"№{row['id']} · создана {row['created_at'][:16]} · обновлена {row['updated_at'][:16]}")
        self._update_buttons()

    def _new(self):
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(None)
        self.tree.clearSelection()
        self.tree.blockSignals(False)
        self.topic.setText(self.topic_filter.currentData() or "")
        self.title.clear()
        self.text.clear()
        self.status.setText("Новая запись")
        self._update_buttons()
        self.title.setFocus()

    def _update_buttons(self):
        self.delete_btn.setEnabled(self.current_id() is not None)

    def _save(self):
        current = self.current_id()
        try:
            with closing(db.connect()) as conn:
                mem = Memory(conn)
                if current is None:
                    saved, updated = mem.save(self.topic.text(), self.title.text(), self.text.toPlainText())
                else:
                    mem.update(current, self.topic.text(), self.title.text(), self.text.toPlainText())
                    saved, updated = current, True
        except MemoryInputError as e:
            self.status.setText(f"Не сохранено: {e}")
            return
        self.reload_keeping(saved)
        self.status.setText(f"{'Обновлена' if updated else 'Добавлена'} запись №{saved}")

    def reload_keeping(self, memory_id: int):
        """Перечитывает список и выбирает запись; фильтры снимаются: после правки запись могла уйти из выбранной темы."""
        self.query.blockSignals(True)
        self.query.clear()
        self.query.blockSignals(False)
        self._load_topics(keep_filter=False)
        self._fill_list(select=memory_id)

    def _delete(self):
        current = self.current_id()
        if current is None:
            return
        answer = QMessageBox.question(self, "Удалить запись", f"Удалить запись «{self.title.text()}» из памяти? Это необратимо.")
        if answer != QMessageBox.Yes:
            return
        with closing(db.connect()) as conn:
            Memory(conn).delete(current)
        self.reload()
        self._new()
        self.status.setText(f"Запись №{current} удалена")
