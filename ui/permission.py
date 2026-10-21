import itertools
import json
import textwrap
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

LINE_WIDTH = 100  # longer lines are not shown: the window fits the longest line and would go off screen
VALUE_PREVIEW_MAX = 700  # how many characters of one argument to show in the window
ARG_LABELS = {  # readable argument labels for the confirmation window
    "toolbox.fs_replace": {"path": "File", "old": "Was", "new": "Will be"},
    "toolbox.notes_write": {"name": "Note", "content": "Text"},
    "toolbox.web_extract": {"url": "Address", "char_limit": "Character limit"},
    "toolbox.shell_run": {"command": "PowerShell command", "cwd": "Folder"},
    "toolbox.docx_write": {"path": "File", "markdown": "Text (Markdown)", "overwrite": "Replace existing"},
    "toolbox.memory_save": {"topic": "Topic", "title": "Title", "text": "What to remember"},
    "toolbox.memory_delete": {"id": "Entry number"},
    "toolbox.xlsx_write": {"path": "File", "sheets": "Sheets", "overwrite": "Replace existing"},
}
FULL_ARGUMENTS = {("toolbox.shell_run", "command")}  # the command is shown in full: its tail must not hide behind "…"


def _wrap(text: str) -> str:
    """Long lines are wrapped at words (words longer than a line are cut); the text itself is not changed."""
    lines = []
    for line in text.split("\n"):
        lines.extend(textwrap.wrap(line, LINE_WIDTH, replace_whitespace=False, drop_whitespace=False) or [""])
    return "\n".join(lines)


def format_arguments(tool_id: str, arguments: dict) -> str:
    """Call arguments for the confirmation window: multi-line values as a block, without JSON escaping."""
    labels = ARG_LABELS.get(tool_id, {})
    parts = []
    for key, value in arguments.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > VALUE_PREVIEW_MAX and (tool_id, key) not in FULL_ARGUMENTS:
            text = text[:VALUE_PREVIEW_MAX] + f"… ({len(text) - VALUE_PREVIEW_MAX} more chars)"
        label = labels.get(key, key)
        if "\n" in text or len(text) > 60:
            parts.append(f"{label}:\n" + "\n".join("    " + line for line in text.split("\n")))
        else:
            parts.append(f"{label}: {text}")
    return _wrap("\n\n".join(parts)) if parts else "(no arguments)"


class PermissionBroker(QObject):
    """A bridge between the worker thread and the window: the thread asks to confirm a call and waits for the user's answer."""

    asked = Signal(int, str, str)  # request id, tool, arguments to show

    def __init__(self, parent_widget=None, timeout: float = 300.0):
        super().__init__(parent_widget)
        self._parent_widget = parent_widget
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, list] = {}  # id -> [Event, answer]
        self._lock = threading.Lock()
        self.asked.connect(self._on_asked)

    def ask(self, tool_id: str, arguments: dict, cancel: threading.Event | None = None) -> bool:
        """From the worker thread: True if the user allowed it. Cancelling generation or a timeout means refusal."""
        rid = next(self._ids)
        event = threading.Event()
        with self._lock:
            self._pending[rid] = [event, False]
        self.asked.emit(rid, tool_id, format_arguments(tool_id, arguments))
        waited = 0.0
        while not event.wait(0.2):
            waited += 0.2
            if (cancel is not None and cancel.is_set()) or waited >= self._timeout:
                break
        with self._lock:
            answer = self._pending.pop(rid, [None, False])[1]
        return bool(answer) and event.is_set()

    def _on_asked(self, rid: int, tool_id: str, text: str):
        """In the main thread: shows the question and passes the answer to the waiting thread."""
        allowed = False
        try:
            reply = QMessageBox.question(
                self._parent_widget, "Allow the tool call?", f"{tool_id}\n\n{text}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            allowed = reply == QMessageBox.Yes
        finally:  # the window did not open or crashed: the thread gets a refusal at once instead of waiting for the timeout
            with self._lock:
                entry = self._pending.get(rid)
                if entry is not None:
                    entry[1] = allowed
                    entry[0].set()
