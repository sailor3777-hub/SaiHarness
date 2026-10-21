from contextlib import closing

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core import db
from core.memory import Memory, MemoryInputError

ID_ROLE = Qt.UserRole
ALL_TOPICS = "All topics"
SEARCH_LIMIT = 200


class MemoryPanel(QWidget):
    """The agent's long-term memory: view, search, edit and delete entries without the model."""

    def __init__(self):
        super().__init__()
        self._loading = False

        self.query = QLineEdit()
        self.query.setPlaceholderText("Search memory")
        self.query.setClearButtonEnabled(True)
        self.topic_filter = QComboBox()
        self.refresh_btn = QPushButton("Refresh")

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Topic", "Title", "Updated"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 110)
        self.tree.setColumnWidth(1, 170)
        self.info = QLabel("")
        self.info.setWordWrap(True)

        self.topic = QLineEdit()
        self.topic.setPlaceholderText("Topic")
        self.title = QLineEdit()
        self.title.setPlaceholderText("Title")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("What to remember: self-contained text that makes sense without the original conversation")
        self.new_btn = QPushButton("New entry")
        self.save_btn = QPushButton("Save")
        self.delete_btn = QPushButton("Delete")
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
        self.reload()  # the model adds entries from another process: re-read when the tab is opened

    # --- list ---

    def reload(self):
        """Re-reads topics and entries from the database, keeping the filter and the selected entry."""
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
        shown = f"showing {len(rows)} of {total}" if (query or topic) else f"entries: {total}"
        self.info.setText(shown if total else "Memory is empty. Ask the agent to \"remember this\" (the memory_* tools "
                                              "must be on, Tools tab) or add an entry here.")
        if chosen is not None:
            self.tree.blockSignals(True)  # without re-reading: the editor may hold unsaved text
            self.tree.setCurrentItem(chosen)
            self.tree.blockSignals(False)
        elif select is not None:
            self._new()  # the selected entry is gone (deleted or filtered out)
        self._update_buttons()

    def current_id(self) -> int | None:
        item = self.tree.currentItem()
        return item.data(0, ID_ROLE) if item is not None else None

    # --- editor ---

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
        self.status.setText(f"#{row['id']} · created {row['created_at'][:16]} · updated {row['updated_at'][:16]}")
        self._update_buttons()

    def _new(self):
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(None)
        self.tree.clearSelection()
        self.tree.blockSignals(False)
        self.topic.setText(self.topic_filter.currentData() or "")
        self.title.clear()
        self.text.clear()
        self.status.setText("New entry")
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
            self.status.setText(f"Not saved: {e}")
            return
        self.reload_keeping(saved)
        self.status.setText(f"{'Updated' if updated else 'Added'} entry #{saved}")

    def reload_keeping(self, memory_id: int):
        """Re-reads the list and selects the entry; filters are cleared: after editing the entry may have left the selected topic."""
        self.query.blockSignals(True)
        self.query.clear()
        self.query.blockSignals(False)
        self._load_topics(keep_filter=False)
        self._fill_list(select=memory_id)

    def _delete(self):
        current = self.current_id()
        if current is None:
            return
        answer = QMessageBox.question(self, "Delete entry", f"Delete the entry \"{self.title.text()}\" from memory? This cannot be undone.")
        if answer != QMessageBox.Yes:
            return
        with closing(db.connect()) as conn:
            Memory(conn).delete(current)
        self.reload()
        self._new()
        self.status.setText(f"Entry #{current} deleted")
