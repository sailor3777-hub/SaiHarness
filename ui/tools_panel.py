import logging
import os
from pathlib import Path

import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLabel, QMenu, QComboBox, QLineEdit, QCheckBox,
    QScrollArea, QSplitter, QToolButton, QWidgetAction,
)

from core import db, subagent
from core import imagegen, vision
from core.connection import normalize_url
from core.vision import VISION_PROFILE_KEY
from core.mcp_manager import McpManager, ServerState, ToolInfo, load_config
from core.tools import (DEFAULT_CHAT_TOOLS, TOOL_GROUPS, OTHER_GROUP, Catalog, default_policy, meta_tools_tokens,
                         schema_tokens, tool_group, with_requirements)
from core.files import estimate_tokens
from ui import themes
from worker import AsyncCall

ID_ROLE, KIND_ROLE = Qt.UserRole, Qt.UserRole + 1  # KIND: 'server' | 'group' | 'tool' | 'note'
log = logging.getLogger(__name__)


def _dim() -> QBrush:
    return QBrush(QColor(themes.current().dim))


def _err() -> QBrush:
    return QBrush(QColor(themes.current().tool_error))


POLICY_TAGS = {"ask": "  [asks]", "deny": "  [blocked]", "auto": ""}
POLICY_NAMES = {"auto": "Run without asking", "ask": "Ask for confirmation", "deny": "Block"}
CHECK = {True: Qt.Checked, False: Qt.Unchecked}
AUTO_APPROVE_KEY = "auto_approve"  # app_settings: "1" = calls that ask for confirmation run without the window
AUTO_APPROVE_WARNING = "Auto-approve is on: write tools, shell_run and web_extract run without asking."
SPLIT_KEY = "tools_split"  # app_settings: heights of the tool tree and the settings below it (JSON list)


def _walk(item: QTreeWidgetItem):
    """All items below item (depth first)."""
    for i in range(item.childCount()):
        child = item.child(i)
        yield child
        yield from _walk(child)


def _leaf_ids(item: QTreeWidgetItem) -> list[str]:
    """ids of the tools below a server or group item."""
    return [it.data(0, ID_ROLE) for it in _walk(item) if it.data(0, KIND_ROLE) == "tool"]


class ToolsPanel(QWidget):
    """MCP servers and their tools. Checkboxes choose which tools the model can use in this chat."""

    selection_changed = Signal()  # the user changed the set of enabled tools or agents
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
        self.agents: set[str] = set()  # names of the profiles the chat's model may delegate to
        self.main_profile = lambda: None  # the chat's profile (set by the window): for the "model reload" warning
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
        self.agents_btn = QToolButton()
        self.agents_btn.setPopupMode(QToolButton.InstantPopup)
        self.agents_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.agents_btn.setSizePolicy(self.vision_combo.sizePolicy())
        self.agents_btn.setToolTip("Model profiles the chat's model may hand tasks to (delegate).\n"
                                   "An agent works with this chat's tools and permissions, but does not see the chat.\n"
                                   "Its description for the model is the \"As an agent\" field in Model Builder.")
        self.agents_menu = QMenu(self)
        self.agents_menu.aboutToShow.connect(self._fill_agents_menu)
        self.agents_btn.setMenu(self.agents_menu)
        agents_row = QHBoxLayout()
        agents_row.addWidget(QLabel("Agents:"))
        agents_row.addWidget(self.agents_btn, 1)
        self.agents_status = QLabel("")
        self.agents_status.setObjectName("status")
        self.agents_status.setWordWrap(True)

        settings = QWidget()  # everything under the tree; the splitter handle above it changes its height
        lower = QVBoxLayout(settings)
        lower.setContentsMargins(0, 0, 0, 0)
        lower.addLayout(agents_row)
        lower.addWidget(self.agents_status)
        lower.addLayout(vision_row)
        for w in self.vision_manual:
            lower.addWidget(w)
        lower.addWidget(self.vision_status)
        lower.addWidget(self.comfy_url)
        lower.addWidget(self.comfy_status)
        lower.addWidget(self.info)
        lower.addStretch(1)
        scroll = QScrollArea()  # a low panel scrolls instead of squeezing the fields (vertically only)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(settings)
        # the panel keeps its old minimum width: it is not squeezed narrower than the fields under the tree
        scroll.setMinimumWidth(settings.minimumSizeHint().width() + scroll.verticalScrollBar().sizeHint().width())
        self.split = QSplitter(Qt.Vertical)
        self.split.addWidget(self.tree)
        self.split.addWidget(scroll)
        self.split.setChildrenCollapsible(False)
        self.split.setStretchFactor(0, 1)
        try:
            sizes = json.loads(db.get_setting(SPLIT_KEY) or "null")
        except ValueError:
            sizes = None
        valid = isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(v, int) and v > 0 for v in sizes)
        self.split.setSizes(sizes if valid else [500, 400])  # by default the settings get a good part of the height
        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.setInterval(400)
        self._split_timer.timeout.connect(lambda: db.set_setting(SPLIT_KEY, json.dumps(self.split.sizes())))
        self.split.splitterMoved.connect(lambda *_: self._split_timer.start())

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.auto_approve)
        layout.addWidget(self.auto_warning)
        layout.addWidget(self.split, 1)
        self.refresh_agents()

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
        self.refresh_agents()

    # --- selection ---

    def set_enabled(self, tool_ids: set[str]):
        """Show the chat's tool set (without the selection_changed signal)."""
        self.enabled = set(tool_ids)
        self._sync()
        self._render()

    # --- agents (subagents for delegate) ---

    def set_agents(self, names):
        """Show the chat's agents (without the selection_changed signal)."""
        self.agents = set(names)
        self.refresh_agents()
        self._render()

    def enabled_agents(self) -> list:
        """Profiles of the enabled agents that still exist (a deleted profile is skipped)."""
        return [p for p in db.list_profiles() if p["name"] in self.agents]

    def _fill_agents_menu(self):
        """The profile list is read when the menu opens: profiles change in Model Builder."""
        self.agents_menu.clear()
        profiles = db.list_profiles()
        if not profiles:
            act = self.agents_menu.addAction("No model profiles yet: create one in Model Builder")
            act.setEnabled(False)
            return
        for p in profiles:
            box = QCheckBox(p["name"])  # a checkbox inside the menu: clicking it does not close the menu
            box.setChecked(p["name"] in self.agents)
            box.setToolTip(subagent.description(p))
            box.setContentsMargins(6, 2, 6, 2)
            box.toggled.connect(lambda on, name=p["name"]: self._toggle_agent(name, on))
            act = QWidgetAction(self.agents_menu)
            act.setDefaultWidget(box)
            self.agents_menu.addAction(act)

    def _toggle_agent(self, name: str, on: bool):
        (self.agents.add if on else self.agents.discard)(name)
        self.refresh_agents()
        self._render()
        self.selection_changed.emit()

    def refresh_agents(self):
        enabled = self.enabled_agents()
        names = [p["name"] for p in enabled]
        self.agents_btn.setText(", ".join(names) if names else "none")
        notes = []
        main = self.main_profile()
        main_conn = db.get_connection(main["connection"]) if main is not None and main["connection"] else None
        for p in enabled:
            conn = db.get_connection(p["connection"]) if p["connection"] else None
            if conn is None:
                notes.append(f"{p['name']}: no connection, choose one in Model Builder.")
            elif main_conn is not None and conn["url"] == main_conn["url"] and p["base_model"] != main["base_model"]:
                notes.append(f"{p['name']}: another model on the same server, LM Studio may reload models "
                             "on every call.")
        self.agents_status.setText("\n".join(notes))
        self.agents_status.setVisible(bool(notes))

    def default_tool_ids(self) -> set[str]:
        """The tool set a new chat starts with: DEFAULT_CHAT_TOOLS minus the tools the connected servers do not have.

        Before the servers are connected the registry is unknown, so the whole set is kept
        (an unavailable id is ignored by enabled_tools anyway).
        """
        if not self.states:
            return set(DEFAULT_CHAT_TOOLS)
        known = {t.id for t in self.tools}
        missing = [t for t in DEFAULT_CHAT_TOOLS if t not in known]
        if missing:
            log.warning("default chat tools not found and skipped: %s", ", ".join(missing))
            from core import diag
            diag.note(f"warning: default chat tools not found and skipped: {', '.join(missing)}")
        return {t for t in DEFAULT_CHAT_TOOLS if t in known}

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
        elif kind in ("server", "group"):
            ids = set(_leaf_ids(item))
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
        expanded = {it.data(0, ID_ROLE) for it in _walk(self.tree.invisibleRootItem()) if it.isExpanded()}
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
                groups: dict[str, list[ToolInfo]] = {}
                for t in state.tools:
                    groups.setdefault(tool_group(t), []).append(t)
                order = [g for g in (*TOOL_GROUPS, OTHER_GROUP) if g in groups]
                if len(order) == 1:  # a server with one kind of tools: no extra level
                    self._add_tools(top, groups[order[0]])
                for group in order if len(order) > 1 else ():
                    item = QTreeWidgetItem(top)
                    item.setData(0, ID_ROLE, f"{name}/{group}")
                    item.setData(0, KIND_ROLE, "group")
                    item.setText(0, f"{group} ({len(groups[group])})")
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.Unchecked)
                    self._add_tools(item, groups[group])
                    item.setExpanded(f"{name}/{group}" in expanded)
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

    def _add_tools(self, parent: QTreeWidgetItem, tools: list[ToolInfo]):
        for t in tools:
            child = QTreeWidgetItem(parent, [t.name + POLICY_TAGS[self.policy_for(t)]])
            child.setData(0, ID_ROLE, t.id)
            child.setData(0, KIND_ROLE, "tool")
            child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            child.setCheckState(0, Qt.Unchecked)
            child.setToolTip(0, f"{t.description}\n\nPermissions: {POLICY_NAMES[self.policy_for(t)]}\n"
                                "Right-click: change permissions.")

    def _sync(self):
        self._updating = True
        wanted = with_requirements(self.enabled)  # dependencies of enabled tools are checked even if they were not chosen

        def sync(item: QTreeWidgetItem) -> tuple[int, int]:
            """Sets the check marks below item and on item itself; returns (enabled, total) tools."""
            if item.data(0, KIND_ROLE) == "tool":
                state = item.data(0, ID_ROLE) in wanted
                item.setCheckState(0, CHECK[state])
                return int(state), 1
            on = total = 0
            for j in range(item.childCount()):
                a, b = sync(item.child(j))
                on, total = on + a, total + b
            item.setCheckState(0, Qt.Checked if total and on == total else Qt.PartiallyChecked if on else Qt.Unchecked)
            return on, total

        try:
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                if top.flags() & Qt.ItemIsUserCheckable:
                    sync(top)
        finally:
            self._updating = False

    # --- size estimate ---

    def catalog_text(self) -> str:
        return Catalog(self.enabled_tools()).text()

    def prompt_tokens(self) -> int:
        """How many tokens the tools take in the request: meta-tools, catalog and agents (0 if nothing is enabled)."""
        text = self.catalog_text()
        agents = subagent.catalog_block(self.enabled_agents())
        if not text and not agents:
            return 0
        return meta_tools_tokens(with_delegate=bool(agents)) + estimate_tokens(text) + estimate_tokens(agents)

    def _render(self):
        if self._error:
            self.info.setText(f"Could not connect the servers: {self._error}")
            return
        enabled = self.enabled_tools()
        agents = len(self.enabled_agents())
        if not self.states:
            self.info.setText("Servers are not connected yet")
        elif not enabled and not agents:
            self.info.setText("No tools enabled: they are not in the request and cost nothing.")
        else:
            full = schema_tokens(enabled)
            meta = "4 meta-tools, the catalog and the agents" if agents else "3 meta-tools and the catalog"
            self.info.setText(
                f"Enabled: {len(enabled)}" + (f", agents: {agents}" if agents else "") +
                f". In the request ≈{self.prompt_tokens()} tok. ({meta}). Full schemas would take ≈{full} tok."
            )
