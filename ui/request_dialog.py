import json

from PySide6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QLabel, QPushButton


class RequestDialog(QDialog):
    """Shows exactly what was sent to the model in this chat's last request."""

    def __init__(self, parent, request: dict | None):
        super().__init__(parent)
        self.setWindowTitle("Last request to the model")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        if request is None:
            layout.addWidget(QLabel("There have been no requests in this chat since the app started."))
        else:
            tools = ", ".join(request.get("tools") or []) or "none"
            layout.addWidget(QLabel(
                f"{request['url']}  ·  model: {request['model']}  ·  "
                f"temperature: {request['temperature']}  ·  max_tokens: {request['max_tokens']}  ·  "
                f"functions: {tools}"
            ))
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(json.dumps(request["messages"], ensure_ascii=False, indent=2))
            layout.addWidget(text, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
