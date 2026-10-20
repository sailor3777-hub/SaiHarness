import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLabel, QMenu, QComboBox, QLineEdit,
)

from core import db
from core import imagegen, vision
from core.connection import normalize_url
from core.vision import VISION_PROFILE_KEY
from core.mcp_manager import McpManager, ServerState, ToolInfo, load_config
from core.tools import Catalog, default_policy, meta_tools_tokens, schema_tokens, with_requirements
from core.files import estimate_tokens
from ui import themes
from worker import AsyncCall

ID_ROLE, KIND_ROLE = Qt.UserRole, Qt.UserRole + 1  # KIND: 'server' | 'tool' | 'note'


def _dim() -> QBrush:
    return QBrush(QColor(themes.current().dim))


def _err() -> QBrush:
    return QBrush(QColor(themes.current().tool_error))


POLICY_TAGS = {"ask": "  [спросит]", "deny": "  [запрещён]", "auto": ""}
POLICY_NAMES = {"auto": "Выполнять без вопросов", "ask": "Спрашивать подтверждение", "deny": "Запретить"}
CHECK = {True: Qt.Checked, False: Qt.Unchecked}


class ToolsPanel(QWidget):
    """MCP-серверы и их инструменты. Галочки выбирают, какие инструменты доступны модели в этом чате."""

    selection_changed = Signal()  # пользователь изменил набор включённых инструментов
    catalog_changed = Signal()    # серверы (пере)подключены

    def __init__(self, manager: McpManager, config_path: Path, auto_connect: bool = True):
        super().__init__()
        self.manager, self.config_path = manager, config_path
        self.enabled: set[str] = set()
        self.policies: dict[str, str] = db.load_tool_policies()
        self.tools: list[ToolInfo] = []
        self.states: dict[str, ServerState] = {}
        self._updating = False
        self._error = ""

        self.reconnect_btn = QPushButton("Перезапустить")
        self.config_btn = QPushButton("Конфиг")
        buttons = QHBoxLayout()
        buttons.addWidget(self.reconnect_btn)
        buttons.addWidget(self.config_btn)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("MCP-серверы и инструменты")
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("status")
        self.vision_combo = QComboBox()
        self.vision_combo.setToolTip("Модель с поддержкой изображений, которую вызывает инструмент vision_describe.\n"
                                     "Профиль: берутся его подключение, модель, температура и лимит токенов.\n"
                                     "Вручную: адрес сервера LM Studio, ключ и название модели вводятся здесь.")
        vision_row = QHBoxLayout()
        vision_row.addWidget(QLabel("Vision:"))
        vision_row.addWidget(self.vision_combo, 1)
        self.vision_url = QLineEdit()
        self.vision_url.setPlaceholderText("Адрес LM Studio: 192.168.1.5:1234 или http://host:1234/v1")
        self.vision_key = QLineEdit()
        self.vision_key.setPlaceholderText("Ключ API (если не нужен, пусто)")
        self.vision_key.setEchoMode(QLineEdit.Password)
        self.vision_model = QLineEdit()
        self.vision_model.setPlaceholderText("Название модели с поддержкой изображений")
        self.vision_status = QLabel("")
        self.vision_status.setObjectName("status")
        self.vision_status.setWordWrap(True)
        self.vision_manual = [self.vision_url, self.vision_key, self.vision_model]
        self.comfy_url = QLineEdit()
        self.comfy_url.setPlaceholderText("Картинки: адрес ComfyUI, например 192.168.1.5:8188")
        self.comfy_url.setToolTip("Сервер ComfyUI, который вызывает инструмент image_generate.\n"
                                  "Workflow (Save (API Format)) лежит в data/comfy_workflow.json.")
        self.comfy_status = QLabel("")
        self.comfy_status.setObjectName("status")
        self.comfy_status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.tree, 1)
        layout.addLayout(vision_row)
        for w in self.vision_manual:
            layout.addWidget(w)
        layout.addWidget(self.vision_status)
        layout.addWidget(self.comfy_url)
        layout.addWidget(self.comfy_status)
        layout.addWidget(self.info)

        self.connect_req = AsyncCall(self._connect, self)
        self.connect_req.TIMEOUT_MS = 120_000
        self.connect_req.TIMEOUT_TEXT = "Серверы не ответили за {seconds:g} с"
        self.connect_req.succeeded.connect(self._on_connected)
        self.connect_req.failed.connect(self._on_connect_failed)
        self.connect_req.busy_changed.connect(lambda busy: self.reconnect_btn.setEnabled(not busy))
        self.reconnect_btn.clicked.connect(self.connect_servers)
        self.config_btn.clicked.connect(self._open_config)
        self.tree.itemChanged.connect(self._on_item_changed)
        themes.bus.changed.connect(lambda _theme: self._rebuild())
        self.vision_combo.activated.connect(self._on_vision_chosen)
        for w in self.vision_manual:
            w.editingFinished.connect(self._save_vision_manual)
        self.comfy_url.editingFinished.connect(self._save_comfy)
        self.comfy_url.setText(db.get_setting(imagegen.COMFY_URL_KEY))
        self._update_comfy_view()
        self.refresh_vision_profiles()
        self.tree.customContextMenuRequested.connect(self._menu)
        self._render()
        if auto_connect:
            self.connect_servers()

    # --- подключение ---

    def connect_servers(self):
        self._error = ""
        self.info.setText("Подключаю серверы…")
        self.connect_req.start()

    def _connect(self) -> dict:
        """В фоновом потоке: читает конфиг и подключает серверы."""
        return self.manager.connect_all(load_config(self.config_path, db.DB_PATH))

    def _on_connected(self, states: dict):
        self.states = states
        self.tools = self.manager.all_tools()  # включённые id остаются в self.enabled, даже если сервер отвалился
        self._rebuild()
        self.catalog_changed.emit()

    def _on_connect_failed(self, message: str):
        self._error = message
        self._render()

    def _open_config(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.config_path)))

    def wait_threads(self):
        self.connect_req.wait()

    # --- vision ---

    def refresh_vision_profiles(self):
        """Список профилей в выпадающем списке (профили правятся на другой вкладке, поэтому обновляем при показе)."""
        current = db.get_setting(VISION_PROFILE_KEY)
        names = [p["name"] for p in db.list_profiles()]
        self.vision_combo.blockSignals(True)
        self.vision_combo.clear()
        self.vision_combo.addItem("Вручную (адрес, ключ, модель)", "")
        for name in names:
            self.vision_combo.addItem(name, name)
        if current and current not in names:  # профиль удалили: показываем это, а не молча сбрасываем
            self.vision_combo.addItem(f"{current} (не найден)", current)
        self.vision_combo.setCurrentIndex(max(0, self.vision_combo.findData(current)))
        self.vision_combo.blockSignals(False)
        for w, key in zip(self.vision_manual, (vision.VISION_URL_KEY, vision.VISION_API_KEY_KEY, vision.VISION_MODEL_KEY)):
            w.setText(db.get_setting(key))
        self._update_vision_view()

    def _update_vision_view(self):
        manual = not self.vision_combo.currentData()
        for w in self.vision_manual:
            w.setVisible(manual)
        if manual:
            ready = bool(self.vision_url.text().strip() and self.vision_model.text().strip())
            self.vision_status.setText("" if ready else "Задай адрес и название модели: без них vision_describe не работает.")
        else:
            self.vision_status.setText("")
        self.vision_status.setVisible(bool(self.vision_status.text()))

    def _on_vision_chosen(self, _index: int):
        db.set_setting(VISION_PROFILE_KEY, self.vision_combo.currentData() or "")
        self._update_vision_view()

    def _save_vision_manual(self):
        """Ручные настройки vision: адрес приводится к виду http://host:port/v1."""
        raw_url = self.vision_url.text().strip()
        try:
            url = normalize_url(raw_url) if raw_url else ""
        except ValueError as e:
            self.vision_status.setText(f"Адрес не подходит: {e}")
            self.vision_status.setVisible(True)
            return
        self.vision_url.setText(url)
        db.set_setting(vision.VISION_URL_KEY, url)
        db.set_setting(vision.VISION_API_KEY_KEY, self.vision_key.text().strip())
        db.set_setting(vision.VISION_MODEL_KEY, self.vision_model.text().strip())
        self._update_vision_view()

    # --- картинки (ComfyUI) ---

    def _update_comfy_view(self, error: str = ""):
        if error:
            text = f"Адрес не подходит: {error}"
        elif not self.comfy_url.text().strip():
            text = "Задай адрес ComfyUI: без него image_generate не работает."
        else:
            try:
                imagegen.load_workflow()
                text = ""
            except imagegen.ImageGenError as e:
                text = str(e)
        self.comfy_status.setText(text)
        self.comfy_status.setVisible(bool(text))

    def _save_comfy(self):
        raw = self.comfy_url.text().strip()
        try:
            url = imagegen.normalize_url(raw) if raw else ""
        except ValueError as e:
            self._update_comfy_view(str(e))
            return
        self.comfy_url.setText(url)
        db.set_setting(imagegen.COMFY_URL_KEY, url)
        self._update_comfy_view()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_vision_profiles()
        self.comfy_url.setText(db.get_setting(imagegen.COMFY_URL_KEY))
        self._update_comfy_view()

    # --- выбор ---

    def set_enabled(self, tool_ids: set[str]):
        """Показать набор инструментов чата (без сигнала selection_changed)."""
        self.enabled = set(tool_ids)
        self._sync()
        self._render()

    def enabled_tools(self) -> list[ToolInfo]:
        """Включённые инструменты, которые сейчас реально доступны (сервер подключён)."""
        wanted = with_requirements(self.enabled)
        return [t for t in self.tools if t.id in wanted]

    def policy_for(self, tool: ToolInfo) -> str:
        return self.policies.get(tool.id) or default_policy(tool)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int):
        if self._updating:
            return
        kind, ident = item.data(0, KIND_ROLE), item.data(0, ID_ROLE)
        checked = item.checkState(0) == Qt.Checked
        if kind == "tool":
            (self.enabled.add if checked else self.enabled.discard)(ident)
        elif kind == "server":
            ids = {t.id for t in self.tools if t.server == ident}
            self.enabled = (self.enabled | ids) if checked else (self.enabled - ids)
        else:
            return
        self._sync()
        self._render()
        self.selection_changed.emit()

    def _menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None or item.data(0, KIND_ROLE) != "tool":
            return
        tool = next((t for t in self.tools if t.id == item.data(0, ID_ROLE)), None)
        if tool is None:
            return
        menu = QMenu(self)
        current = self.policy_for(tool)
        actions = {}
        for mode, title in POLICY_NAMES.items():
            act = menu.addAction(("✓ " if mode == current else "   ") + title)
            actions[act] = mode
        menu.addSeparator()
        reset = menu.addAction(f"Сбросить (по умолчанию: {POLICY_NAMES[default_policy(tool)].lower()})")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is reset:
            self.set_policy(tool.id, None)
        elif chosen in actions:
            self.set_policy(tool.id, actions[chosen])

    def set_policy(self, tool_id: str, mode: str | None):
        db.set_tool_policy(tool_id, mode)
        if mode is None:
            self.policies.pop(tool_id, None)
        else:
            self.policies[tool_id] = mode
        self._rebuild()

    # --- дерево ---

    def _rebuild(self):
        expanded = {self.tree.topLevelItem(i).data(0, ID_ROLE) for i in range(self.tree.topLevelItemCount())
                    if self.tree.topLevelItem(i).isExpanded()}
        first = self.tree.topLevelItemCount() == 0
        self._updating = True
        self.tree.clear()
        for name, state in self.states.items():
            top = QTreeWidgetItem(self.tree)
            top.setData(0, ID_ROLE, name)
            top.setData(0, KIND_ROLE, "server")
            if state.status == "ready":
                top.setText(0, f"● {name} — инструментов: {len(state.tools)}")
                top.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                top.setCheckState(0, Qt.Unchecked)
                for t in state.tools:
                    child = QTreeWidgetItem(top, [t.name + POLICY_TAGS[self.policy_for(t)]])
                    child.setData(0, ID_ROLE, t.id)
                    child.setData(0, KIND_ROLE, "tool")
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Unchecked)
                    child.setToolTip(0, f"{t.description}\n\nПрава: {POLICY_NAMES[self.policy_for(t)]}\n"
                                        "Правая кнопка мыши: изменить права.")
            elif state.status == "disabled":
                top.setText(0, f"○ {name} — отключён в конфиге")
                top.setFlags(Qt.ItemIsEnabled)
                top.setForeground(0, _dim())
            else:
                top.setText(0, f"✗ {name} — ошибка")
                top.setFlags(Qt.ItemIsEnabled)
                top.setForeground(0, _err())
                note = QTreeWidgetItem(top, [state.error or "неизвестная ошибка"])
                note.setData(0, KIND_ROLE, "note")
                note.setFlags(Qt.ItemIsEnabled)
                note.setForeground(0, _dim())
                top.setExpanded(True)
            if first or name in expanded:
                top.setExpanded(True)
        self._updating = False
        self._sync()
        self._render()

    def _sync(self):
        self._updating = True
        wanted = with_requirements(self.enabled)  # зависимости включённых инструментов отмечены, даже если их не выбирали
        try:
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                if not (top.flags() & Qt.ItemIsUserCheckable):
                    continue
                on = 0
                for j in range(top.childCount()):
                    child = top.child(j)
                    state = child.data(0, ID_ROLE) in wanted
                    child.setCheckState(0, CHECK[state])
                    on += state
                total = top.childCount()
                top.setCheckState(0, Qt.Checked if total and on == total else Qt.PartiallyChecked if on else Qt.Unchecked)
        finally:
            self._updating = False

    # --- оценка размера ---

    def catalog_text(self) -> str:
        return Catalog(self.enabled_tools()).text()

    def prompt_tokens(self) -> int:
        """Сколько токенов инструменты занимают в запросе: мета-инструменты и каталог (0, если ничего не включено)."""
        text = self.catalog_text()
        return meta_tools_tokens() + estimate_tokens(text) if text else 0

    def _render(self):
        if self._error:
            self.info.setText(f"Не удалось подключить серверы: {self._error}")
            return
        enabled = self.enabled_tools()
        if not self.states:
            self.info.setText("Серверы ещё не подключены")
        elif not enabled:
            self.info.setText("Инструменты не включены: в запрос они не попадают и ничего не стоят.")
        else:
            full = schema_tokens(enabled)
            self.info.setText(
                f"Включено: {len(enabled)}. В запросе ≈{self.prompt_tokens()} ток. (3 мета-инструмента и каталог). "
                f"Полные схемы заняли бы ≈{full} ток."
            )
