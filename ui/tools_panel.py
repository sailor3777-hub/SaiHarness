import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLabel, QMenu, QComboBox, QLineEdit, QCheckBox,
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


POLICY_TAGS = {"ask": "  [asks]", "deny": "  [blocked]", "auto": ""}
POLICY_NAMES = {"auto": "Run without asking", "ask": "Ask for confirmation", "deny": "Block"}
CHECK = {True: Qt.Checked, False: Qt.Unchecked}
AUTO_APPROVE_KEY = "auto_approve"  # app_settings: "1" = calls that ask for confirmation run without the window
AUTO_APPROVE_WARNING = "Auto-approve is on: write tools, shell_run and web_extract run without asking."


class ToolsPanel(QWidget):
    """MCP servers and their tools. Checkboxes choose which tools the model can use in this chat."""

    selection_changed = Signal()  # the user changed the set of enabled tools
    catalog_changed = Signal()    # servers were (re)connected

    def __init__(self, manager: McpManager, config_path: Path, auto_connect: bool = True, agent=None):
        super().__init__()
        self.manager, self.config_path = manager, config_path
        # Connected remote hosts (core.agent.manager.AgentManager): their tools are always
        # available while the host is connected, so they do not need per-chat checkboxes.
        self.agent = agent
        if agent is not None:
            from core.agent.manager import CompositeManager
            self.router_manager = CompositeManager(manager, agent)
        else:
            self.router_manager = manager
        self.enabled: set[str] = set()
        self.policies: dict[str, str] = db.load_tool_policies()
        self.tools: list[ToolInfo] = []
        self.states: dict[str, ServerState] = {}
        self._updating = False
        self._error = ""

        self.reconnect_btn = QPushButton("Restart")
        self.config_btn = QPushButton("Config")
        buttons = QHBoxLayout()
        buttons.addWidget(self.reconnect_btn)
        buttons.addWidget(self.config_btn)
        self.auto_approve = QCheckBox("Auto-approve all confirmations")
        self.auto_approve.setToolTip("Tools set to \"Ask for confirmation\" run without the window.\n"
                                     "Blocked tools stay blocked. The setting applies to all chats.")
        self.auto_approve.setChecked(db.get_setting(AUTO_APPROVE_KEY) == "1")
        self.auto_warning = QLabel(AUTO_APPROVE_WARNING)
        self.auto_warning.setObjectName("status")
        self.auto_warning.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("MCP servers and tools")
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("status")
        self.vision_combo = QComboBox()
        # a long profile name must not set the width of the whole right panel
        self.vision_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.vision_combo.setMinimumContentsLength(8)
        self.vision_combo.setToolTip("A model with image support that the vision_describe tool calls.\n"
                                     "Profile: its connection, model, temperature and token limit are used.\n"
                                     "Manual: the LM Studio server address, key and model name are entered here.")
        vision_row = QHBoxLayout()
        vision_row.addWidget(QLabel("Vision:"))
        vision_row.addWidget(self.vision_combo, 1)
        self.vision_url = QLineEdit()
        self.vision_url.setPlaceholderText("LM Studio address: 192.168.1.5:1234 or http://host:1234/v1")
        self.vision_key = QLineEdit()
        self.vision_key.setPlaceholderText("API key (empty if not needed)")
        self.vision_key.setEchoMode(QLineEdit.Password)
        self.vision_model = QLineEdit()
        self.vision_model.setPlaceholderText("Name of a model with image support")
        self.vision_status = QLabel("")
        self.vision_status.setObjectName("status")
        self.vision_status.setWordWrap(True)
        self.vision_manual = [self.vision_url, self.vision_key, self.vision_model]
        self.comfy_url = QLineEdit()
        self.comfy_url.setPlaceholderText("Images: ComfyUI address, for example 192.168.1.5:8188")
        self.comfy_url.setToolTip("The ComfyUI server that the image_generate tool calls.\n"
                                  "The workflow (Save (API Format)) lives in data/comfy_workflow.json.")
        self.comfy_status = QLabel("")
        self.comfy_status.setObjectName("status")
        self.comfy_status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.auto_approve)
        layout.addWidget(self.auto_warning)
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
        self.connect_req.TIMEOUT_TEXT = "The servers did not answer within {seconds:g} s"
        self.connect_req.succeeded.connect(self._on_connected)
        self.connect_req.failed.connect(self._on_connect_failed)
        self.connect_req.busy_changed.connect(lambda busy: self.reconnect_btn.setEnabled(not busy))
        self.reconnect_btn.clicked.connect(self.connect_servers)
        self.config_btn.clicked.connect(self._open_config)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.auto_approve.toggled.connect(self._on_auto_approve)
        themes.bus.changed.connect(lambda _theme: self._update_auto_view())
        self._update_auto_view()
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

    # --- connecting ---

    def connect_servers(self):
        self._error = ""
        self.info.setText("Connecting servers…")
        self.connect_req.start()

    def _connect(self) -> dict:
        """In a background thread: reads the config and connects the servers."""
        return self.manager.connect_all(load_config(self.config_path, db.DB_PATH))

    def _on_connected(self, states: dict):
        self.states = states
        self.tools = self.manager.all_tools()  # enabled ids stay in self.enabled even if the server dropped off
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
        """Profiles in the drop-down list (profiles are edited on another tab, so the list is refreshed when shown)."""
        current = db.get_setting(VISION_PROFILE_KEY)
        names = [p["name"] for p in db.list_profiles()]
        self.vision_combo.blockSignals(True)
        self.vision_combo.clear()
        self.vision_combo.addItem("Manual (address, key, model)", "")
        for name in names:
            self.vision_combo.addItem(name, name)
        if current and current not in names:  # the profile was deleted: show that instead of silently resetting
            self.vision_combo.addItem(f"{current} (not found)", current)
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
            self.vision_status.setText("" if ready else "Enter the address and model name: vision_describe does not work without them.")
        else:
            self.vision_status.setText("")
        self.vision_status.setVisible(bool(self.vision_status.text()))

    def _on_vision_chosen(self, _index: int):
        db.set_setting(VISION_PROFILE_KEY, self.vision_combo.currentData() or "")
        self._update_vision_view()

    def _save_vision_manual(self):
        """Manual vision settings: the address is brought to the form http://host:port/v1."""
        raw_url = self.vision_url.text().strip()
        try:
            url = normalize_url(raw_url) if raw_url else ""
        except ValueError as e:
            self.vision_status.setText(f"Invalid address: {e}")
            self.vision_status.setVisible(True)
            return
        self.vision_url.setText(url)
        db.set_setting(vision.VISION_URL_KEY, url)
        db.set_setting(vision.VISION_API_KEY_KEY, self.vision_key.text().strip())
        db.set_setting(vision.VISION_MODEL_KEY, self.vision_model.text().strip())
        self._update_vision_view()

    # --- images (ComfyUI) ---

    def _update_comfy_view(self, error: str = ""):
        if error:
            text = f"Invalid address: {error}"
        elif not self.comfy_url.text().strip():
            text = "Enter the ComfyUI address: image_generate does not work without it."
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

    # --- selection ---

    def set_enabled(self, tool_ids: set[str]):
        """Show the chat's tool set (without the selection_changed signal)."""
        self.enabled = set(tool_ids)
        self._sync()
        self._render()

    def enabled_tools(self) -> list[ToolInfo]:
        """Enabled tools that are actually available right now (the server is connected).

        Connected remote-host tools are always included: a connected host means the
        user set it up and allowed it, so its tools are offered without a per-chat checkbox.
        """
        wanted = with_requirements(self.enabled)
        tools = [t for t in self.tools if t.id in wanted]
        if self.agent is not None:
            tools += self.agent.all_tools()
        return tools

    def policy_for(self, tool: ToolInfo) -> str:
        return self.policies.get(tool.id) or default_policy(tool)

    def effective_policy(self, tool: ToolInfo) -> str:
        """The policy the router applies: with auto-approve "ask" becomes "auto" ("deny" stays)."""
        policy = self.policy_for(tool)
        return "auto" if policy == "ask" and self.auto_approve.isChecked() else policy

    def _on_auto_approve(self, on: bool):
        db.set_setting(AUTO_APPROVE_KEY, "1" if on else "")
        self._update_auto_view()

    def _update_auto_view(self):
        self.auto_warning.setVisible(self.auto_approve.isChecked())
        self.auto_warning.setStyleSheet(f"color: {themes.current().tool_error};")

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
        reset = menu.addAction(f"Reset (default: {POLICY_NAMES[default_policy(tool)].lower()})")
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

    # --- tree ---

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
                top.setText(0, f"● {name} — tools: {len(state.tools)}")
                top.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                top.setCheckState(0, Qt.Unchecked)
                for t in state.tools:
                    child = QTreeWidgetItem(top, [t.name + POLICY_TAGS[self.policy_for(t)]])
                    child.setData(0, ID_ROLE, t.id)
                    child.setData(0, KIND_ROLE, "tool")
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Unchecked)
                    child.setToolTip(0, f"{t.description}\n\nPermissions: {POLICY_NAMES[self.policy_for(t)]}\n"
                                        "Right-click: change permissions.")
            elif state.status == "disabled":
                top.setText(0, f"○ {name} — disabled in the config")
                top.setFlags(Qt.ItemIsEnabled)
                top.setForeground(0, _dim())
            else:
                top.setText(0, f"✗ {name} — error")
                top.setFlags(Qt.ItemIsEnabled)
                top.setForeground(0, _err())
                note = QTreeWidgetItem(top, [state.error or "unknown error"])
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
        wanted = with_requirements(self.enabled)  # dependencies of enabled tools are checked even if they were not chosen
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

    # --- size estimate ---

    def catalog_text(self) -> str:
        return Catalog(self.enabled_tools()).text()

    def prompt_tokens(self) -> int:
        """How many tokens the tools take in the request: meta-tools and catalog (0 if nothing is enabled)."""
        text = self.catalog_text()
        return meta_tools_tokens() + estimate_tokens(text) if text else 0

    def _render(self):
        if self._error:
            self.info.setText(f"Could not connect the servers: {self._error}")
            return
        enabled = self.enabled_tools()
        if not self.states:
            self.info.setText("Servers are not connected yet")
        elif not enabled:
            self.info.setText("No tools enabled: they are not in the request and cost nothing.")
        else:
            full = schema_tokens(enabled)
            self.info.setText(
                f"Enabled: {len(enabled)}. In the request ≈{self.prompt_tokens()} tok. (3 meta-tools and the catalog). "
                f"Full schemas would take ≈{full} tok."
            )
