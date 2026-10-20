from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTabWidget, QLabel, QPlainTextEdit,
    QFormLayout, QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox, QPushButton,
    QHBoxLayout, QMessageBox,
)

from core import db
from core.mcp_manager import McpManager
from core.memory import DEFAULT_RULES
from ui.connection_panel import ConnectionPanel
from ui.files_panel import FilesPanel
from ui.memory_panel import MemoryPanel
from ui.tools_panel import ToolsPanel
from ui.web_panel import WebPanel
from worker import ModelListRequest

SUMMARY_SYSTEM_MAX = 80
MAX_STEPS_LIMIT = 100


class ModelBuilder(QWidget):
    """Настройка профиля модели. Сохранённые профили выбираются во вкладке «Модель»."""

    saved = Signal(str)

    def __init__(self):
        super().__init__()
        self._connections: dict = {}
        self.models_req = ModelListRequest(self)
        self.models_req.succeeded.connect(self._on_models)
        self.models_req.failed.connect(self.fetch_status_set)
        self.models_req.busy_changed.connect(self._on_busy)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Имя профиля")
        self.connection = QComboBox()
        self.base_model = QComboBox()
        self.base_model.setEditable(True)
        self.base_model.setInsertPolicy(QComboBox.NoInsert)
        self.refresh_btn = QPushButton("Обновить")
        self.fetch_status = QLabel("")
        self.fetch_status.setWordWrap(True)
        self.system_prompt = QPlainTextEdit()
        self.system_prompt.setPlaceholderText("System prompt")
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(0.7)
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(1, 131072)
        self.max_tokens.setValue(2048)
        self.max_steps = QSpinBox()
        self.max_steps.setRange(1, MAX_STEPS_LIMIT)
        self.max_steps.setValue(db.DEFAULT_MAX_STEPS)
        self.max_steps.setToolTip(
            "Сколько раундов вызова инструментов модель может сделать за один ответ. Каждый раунд заново "
            "отправляет весь контекст, поэтому большие значения нагружают модель и окно контекста."
        )
        self.memory = QPlainTextEdit()
        self.memory.setPlaceholderText("Память / заметки")
        self.memory_rules = QPlainTextEdit()
        self.memory_rules.setPlaceholderText("Как агенту пользоваться долгосрочной памятью")
        self.memory_rules.setToolTip(
            "Работает, когда в чате включены инструменты memory_* (вкладка «Инструменты»): что и когда сохранять, как "
            "искать. Это часть личности агента, переписывай под себя. Сброс: очистить поле и сохранить.")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.save_btn = QPushButton("Сохранить")

        model_row = QHBoxLayout()
        model_row.addWidget(self.base_model, 1)
        model_row.addWidget(self.refresh_btn)

        form = QFormLayout(self)
        form.addRow("Имя", self.name)
        form.addRow("Подключение", self.connection)
        form.addRow("Базовая модель", model_row)
        form.addRow(self.fetch_status)
        form.addRow("System", self.system_prompt)
        form.addRow("Temperature", self.temperature)
        form.addRow("Max tokens", self.max_tokens)
        form.addRow("Шагов инструментов", self.max_steps)
        form.addRow("Память", self.memory)
        form.addRow("Правила памяти", self.memory_rules)
        form.addRow(self.status, self.save_btn)

        self.save_btn.clicked.connect(self._save)
        self.refresh_btn.clicked.connect(self.refresh_models)
        self.connection.currentIndexChanged.connect(lambda _i: self.refresh_models())
        self.reload_connections()

    # --- подключения и список моделей ---

    def reload_connections(self):
        """Перечитать подключения из БД, сохранив выбор, если подключение ещё существует."""
        keep = self.connection.currentText()
        self._connections = {r["name"]: r for r in db.list_connections()}
        self.connection.blockSignals(True)
        self.connection.clear()
        self.connection.addItems(self._connections)
        if keep in self._connections:
            self.connection.setCurrentText(keep)
        self.connection.blockSignals(False)
        self.refresh_models()

    def refresh_models(self):
        conn = self._connections.get(self.connection.currentText())
        if conn is None:
            self.models_req.cancel()
            msg = "Нет подключений. Создай его во вкладке Connection." if not self._connections else ""
            self.fetch_status.setText(msg)
            return
        self.fetch_status.setText("Загружаю список моделей...")
        self.models_req.start(conn["url"], conn["api_key"])

    def fetch_status_set(self, message: str):
        self.fetch_status.setText(message)

    def _on_models(self, models: list):
        current = self.base_model.currentText()
        self.base_model.clear()
        self.base_model.addItems(models)
        self.base_model.setCurrentText(current)
        self.fetch_status.setText(f"Моделей на сервере: {len(models)}")

    def _on_busy(self, busy: bool):
        self.refresh_btn.setEnabled(not busy)

    def wait_threads(self):
        self.models_req.wait()

    # --- профиль ---

    def load(self, p):
        self.name.setText(p["name"])
        idx = self.connection.findText(p["connection"])
        self.connection.setCurrentIndex(idx)  # -1, если подключение удалено
        self.base_model.setCurrentText(p["base_model"])
        self.system_prompt.setPlainText(p["system_prompt"])
        self.temperature.setValue(p["temperature"])
        self.max_tokens.setValue(p["max_tokens"])
        self.max_steps.setValue(p["max_steps"])
        self.memory.setPlainText(p["memory"])
        self.memory_rules.setPlainText(p["memory_rules"] or DEFAULT_RULES)
        note = ""
        if idx < 0:
            note = f" (подключение «{p['connection']}» не найдено, выбери другое)" if p["connection"] else \
                " (выбери подключение)"
        self.status.setText(f"Редактирование: {p['name']}{note}")

    def _save(self):
        name = self.name.text().strip()
        connection = self.connection.currentText()
        base = self.base_model.currentText().strip()
        if not self._connections:
            self.status.setText("Сначала создай подключение во вкладке Connection")
            return
        if not name or not connection or not base:
            self.status.setText("Нужны имя, подключение и базовая модель")
            return
        rules = self.memory_rules.toPlainText().strip()
        db.save_profile(
            name, connection, base, self.system_prompt.toPlainText(), self.temperature.value(),
            self.max_tokens.value(), self.memory.toPlainText(), self.max_steps.value(),
            "" if rules == DEFAULT_RULES.strip() else rules,  # не изменённые правила не копируем: обновление текста по умолчанию дойдёт
        )
        self.status.setText(f"Сохранено: {name}")
        self.saved.emit(name)


class ModelSelector(QWidget):
    """Выбор готового профиля модели."""

    edit_requested = Signal(object)
    user_changed = Signal(str)  # профиль выбран пользователем (не программно)
    profiles_changed = Signal()  # список профилей перечитан из БД

    def __init__(self):
        super().__init__()
        layout = QFormLayout(self)
        self.profile = QComboBox()
        self.lock_note = QLabel("")
        self.lock_note.setWordWrap(True)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.edit_btn = QPushButton("Изменить")
        self.delete_btn = QPushButton("Удалить")
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn.clicked.connect(self._delete)
        buttons = QHBoxLayout()
        buttons.addWidget(self.edit_btn)
        buttons.addWidget(self.delete_btn)
        layout.addRow("Модель", self.profile)
        layout.addRow(self.lock_note)
        layout.addRow(self.summary)
        layout.addRow(buttons)
        self.profile.currentIndexChanged.connect(self._show_summary)
        self.profile.activated.connect(lambda _i: self.user_changed.emit(self.profile.currentText()))
        self.reload()

    def select(self, name: str):
        """Программно выбрать профиль (без user_changed)."""
        if name in self._profiles and self.profile.currentText() != name:
            self.profile.blockSignals(True)
            self.profile.setCurrentText(name)
            self.profile.blockSignals(False)
            self._show_summary()

    def set_locked(self, locked: bool, note: str = ""):
        self.profile.setEnabled(not locked)
        self.lock_note.setText(note if locked else "")

    def _edit(self):
        p = self.current_profile()
        if p is not None:
            self.edit_requested.emit(p)

    def _delete(self):
        p = self.current_profile()
        if p is None:
            return
        answer = QMessageBox.question(self, "Удалить профиль", f"Удалить профиль «{p['name']}»?")
        if answer == QMessageBox.Yes:
            db.delete_profile(p["name"])
            self.reload()

    def reload(self, select: str | None = None):
        self._profiles = {r["name"]: r for r in db.list_profiles()}
        self._connection_names = {r["name"] for r in db.list_connections()}
        current = select or self.profile.currentText()
        self.profile.blockSignals(True)
        self.profile.clear()
        self.profile.addItems(self._profiles)
        if current in self._profiles:
            self.profile.setCurrentText(current)
        self.profile.blockSignals(False)
        self._show_summary()
        if select:  # новый или изменённый профиль выбран пользователем (из Model Builder): запомнить как выбор
            self.user_changed.emit(select)
        self.profiles_changed.emit()

    def current_profile(self):
        return self._profiles.get(self.profile.currentText())

    def _show_summary(self):
        p = self.current_profile()
        self.edit_btn.setEnabled(p is not None)
        self.delete_btn.setEnabled(p is not None)
        if p is None:
            self.summary.setText("Нет профилей. Создай их во вкладке Model Builder.")
            return
        if not p["connection"]:
            conn = "не задано"
        elif p["connection"] in self._connection_names:
            conn = p["connection"]
        else:
            conn = f"{p['connection']} (удалено)"
        system = " ".join(p["system_prompt"].split())
        if len(system) > SUMMARY_SYSTEM_MAX:
            system = system[:SUMMARY_SYSTEM_MAX] + "…"
        memory = len(p["memory"].strip())
        self.summary.setText(
            f"Подключение: {conn}\nМодель: {p['base_model']}\nTemperature: {p['temperature']}\n"
            f"Max tokens: {p['max_tokens']}\nШагов инструментов: {p['max_steps']}\nSystem: {system or '(пусто)'}\n"
            f"Память: {f'{memory} симв.' if memory else '(пусто)'}"
        )


class ContextPanel(QWidget):
    def __init__(self, mcp_config: Path | None = None, connect_mcp: bool = True):
        super().__init__()
        layout = QVBoxLayout(self)
        title = QLabel("Контекст")
        title.setObjectName("title")
        tabs = QTabWidget()

        self.files_panel = FilesPanel()
        tabs.addTab(self.files_panel, "Файлы")

        self.mcp = McpManager(log_dir=db.DB_PATH.parent / "logs")
        self.tools_panel = ToolsPanel(self.mcp, mcp_config or db.DB_PATH.parent / "mcp.json", auto_connect=connect_mcp)
        tabs.addTab(self.tools_panel, "Тулы")

        self.web_panel = WebPanel()
        tabs.addTab(self.web_panel, "Веб")

        self.memory_panel = MemoryPanel()
        tabs.addTab(self.memory_panel, "Память")

        self.model_selector = ModelSelector()
        tabs.addTab(self.model_selector, "Модель")

        self.connections = ConnectionPanel()
        tabs.addTab(self.connections, "Connection")

        self.builder = ModelBuilder()
        self.builder.saved.connect(lambda name: self.model_selector.reload(select=name))
        tabs.addTab(self.builder, "Model Builder")

        def on_connections_changed():
            self.builder.reload_connections()
            self.model_selector.reload()

        self.connections.changed.connect(on_connections_changed)

        def edit(profile):
            self.builder.load(profile)
            tabs.setCurrentWidget(self.builder)

        self.model_selector.edit_requested.connect(edit)

        layout.addWidget(title)
        layout.addWidget(tabs)

    def shutdown(self):
        """Дождаться фоновых запросов перед закрытием окна."""
        self.builder.wait_threads()
        self.connections.wait_threads()
        self.files_panel.wait_threads()
        self.tools_panel.wait_threads()
        self.web_panel.wait_threads()
        self.mcp.shutdown()
