"""The Telegram tab: the bot token, allowed users, start/stop, the tools of new chats started from Telegram, a log."""
import json
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from core import db, telegram
from core.tools import DEFAULT_CHAT_TOOLS
from ui.tool_tree import ToolTree
from worker import TelegramService

LOG_LINES = 300


def saved_tools() -> set[str]:
    """Tools of new Telegram chats; DEFAULT_CHAT_TOOLS until the set is saved for the first time."""
    raw = db.get_setting(telegram.TOOLS_KEY)
    if not raw:
        return set(DEFAULT_CHAT_TOOLS)
    try:
        value = json.loads(raw)
    except ValueError:
        return set(DEFAULT_CHAT_TOOLS)
    return {t for t in value if isinstance(t, str)} if isinstance(value, list) else set(DEFAULT_CHAT_TOOLS)


class TelegramPanel(QWidget):
    def __init__(self, service: TelegramService | None = None):
        super().__init__()
        self.service = service or TelegramService(parent=self)

        self.token = QLineEdit(db.get_setting(telegram.TOKEN_KEY))
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Token from @BotFather: 123456:ABC-…")
        self.show_token = QPushButton("Show")
        self.show_token.setCheckable(True)
        self.users = QLineEdit(db.get_setting(telegram.USERS_KEY))
        self.users.setPlaceholderText("Telegram user ids, comma separated")
        self.users.setToolTip("Only these users can talk to the bot. To find your id, write to the bot:\n"
                              "the rejected message shows the id in the log below.")
        self.autostart = QCheckBox("Start the bot with the app")
        self.autostart.setChecked(db.get_setting(telegram.AUTOSTART_KEY) == "1")
        self.start_btn = QPushButton("Start")
        self.status = QLabel("Stopped")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        note = QLabel("Messages go to the active chat with the profile chosen in the window. "
                      "Tool confirmations from Telegram are approved automatically (blocked tools stay blocked).")
        note.setObjectName("status")
        note.setWordWrap(True)

        token_row = QHBoxLayout()
        token_row.addWidget(self.token, 1)
        token_row.addWidget(self.show_token)
        run_row = QHBoxLayout()
        run_row.addWidget(self.start_btn)
        run_row.addWidget(self.status, 1)
        form = QFormLayout()
        form.addRow("Bot token", token_row)
        form.addRow("Allowed ids", self.users)
        form.addRow(self.autostart)
        form.addRow(run_row)
        form.addRow(note)

        self.tree = ToolTree("Tools for new chats from Telegram")
        self.tree.set_checked(saved_tools())
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINES)
        self.log_view.setPlaceholderText("Bot log")
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.log_view)
        split.setChildrenCollapsible(False)
        split.setSizes([400, 200])

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(split, 1)

        self.show_token.toggled.connect(
            lambda on: self.token.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        self.token.editingFinished.connect(lambda: db.set_setting(telegram.TOKEN_KEY, self.token.text().strip()))
        self.users.editingFinished.connect(self._save_users)
        self.autostart.toggled.connect(lambda on: db.set_setting(telegram.AUTOSTART_KEY, "1" if on else ""))
        self.start_btn.clicked.connect(self.toggle)
        self.tree.toggled.connect(lambda: db.set_setting(telegram.TOOLS_KEY, json.dumps(sorted(self.tree.checked))))
        self.service.state_changed.connect(self._on_state)
        self.service.log.connect(self.add_log)
        self.service.allowed = telegram.parse_users(self.users.text())

    def tool_ids(self) -> set[str]:
        return set(self.tree.checked)

    def set_states(self, states: dict):
        self.tree.set_states(states)

    def _save_users(self):
        ids = telegram.parse_users(self.users.text())
        text = ", ".join(str(i) for i in sorted(ids))
        self.users.setText(text)
        db.set_setting(telegram.USERS_KEY, text)
        self.service.allowed = ids

    def start(self):
        self._save_users()
        token = self.token.text().strip()
        db.set_setting(telegram.TOKEN_KEY, token)
        if not self.service.allowed:
            self.add_log("No allowed ids yet: every message is rejected, its sender's id is shown here.")
        self.service.start(token)

    def toggle(self):
        if self.service.running:
            self.service.stop()
        else:
            self.start()

    def autostart_if_set(self):
        if self.autostart.isChecked() and self.token.text().strip():
            self.start()

    def _on_state(self, running: bool, text: str):
        self.start_btn.setText("Stop" if running else "Start")
        self.status.setText(text)
        self.add_log(text)

    def add_log(self, text: str):
        self.log_view.appendPlainText(f"{time.strftime('%H:%M:%S')} {text}")
