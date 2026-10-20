import json

from PySide6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QLabel, QPushButton


class RequestDialog(QDialog):
    """Показывает, что именно ушло модели в последнем запросе этого чата."""

    def __init__(self, parent, request: dict | None):
        super().__init__(parent)
        self.setWindowTitle("Последний запрос к модели")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        if request is None:
            layout.addWidget(QLabel("В этом чате ещё не было запросов с момента запуска приложения."))
        else:
            tools = ", ".join(request.get("tools") or []) or "нет"
            layout.addWidget(QLabel(
                f"{request['url']}  ·  модель: {request['model']}  ·  "
                f"temperature: {request['temperature']}  ·  max_tokens: {request['max_tokens']}  ·  "
                f"функции: {tools}"
            ))
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(json.dumps(request["messages"], ensure_ascii=False, indent=2))
            layout.addWidget(text, 1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
