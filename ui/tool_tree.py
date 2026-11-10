"""The tree of MCP servers and tools with checkboxes: server → group → tool.

Used by the Tools tab (the tools of the current chat) and the Telegram tab (the tools of new chats started from
Telegram). The tree only shows and edits a set of tool ids; what the set means is up to the owner.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from core.mcp_manager import ServerState, ToolInfo
from core.tools import TOOL_GROUPS, OTHER_GROUP, tool_group, with_requirements
from ui import themes

ID_ROLE, KIND_ROLE = Qt.UserRole, Qt.UserRole + 1  # KIND: 'server' | 'group' | 'tool' | 'note'
CHECK = {True: Qt.Checked, False: Qt.Unchecked}


def _dim() -> QBrush:
    return QBrush(QColor(themes.current().dim))


def _err() -> QBrush:
    return QBrush(QColor(themes.current().tool_error))


def _walk(item: QTreeWidgetItem):
    """All items below item (depth first)."""
    for i in range(item.childCount()):
        child = item.child(i)
        yield child
        yield from _walk(child)


def _leaf_ids(item: QTreeWidgetItem) -> list[str]:
    """ids of the tools below a server or group item."""
    return [it.data(0, ID_ROLE) for it in _walk(item) if it.data(0, KIND_ROLE) == "tool"]


class ToolTree(QTreeWidget):
    """checked: the chosen tool ids (it may hold ids of servers that are not connected now; they are kept)."""

    toggled = Signal()  # the user changed the set

    def __init__(self, header: str, tag=None, tooltip=None, parent=None):
        super().__init__(parent)
        self.setHeaderLabel(header)
        self.checked: set[str] = set()
        self.states: dict[str, ServerState] = {}
        self._tag = tag or (lambda tool: "")                      # a suffix after the tool name
        self._tooltip = tooltip or (lambda tool: tool.description)
        self._updating = False
        self.itemChanged.connect(self._on_item_changed)
        themes.bus.changed.connect(lambda _theme: self.rebuild())

    def set_states(self, states: dict[str, ServerState]):
        self.states = states
        self.rebuild()

    def set_checked(self, tool_ids):
        """Show a set (without the toggled signal)."""
        self.checked = set(tool_ids)
        self.sync()

    def rebuild(self):
        expanded = {it.data(0, ID_ROLE) for it in _walk(self.invisibleRootItem()) if it.isExpanded()}
        first = self.topLevelItemCount() == 0
        self._updating = True
        self.clear()
        for name, state in self.states.items():
            top = QTreeWidgetItem(self)
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
        self.sync()

    def _add_tools(self, parent: QTreeWidgetItem, tools: list[ToolInfo]):
        for t in tools:
            child = QTreeWidgetItem(parent, [t.name + self._tag(t)])
            child.setData(0, ID_ROLE, t.id)
            child.setData(0, KIND_ROLE, "tool")
            child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            child.setCheckState(0, Qt.Unchecked)
            child.setToolTip(0, self._tooltip(t))

    def sync(self):
        """Puts the check marks from self.checked (dependencies of checked tools are checked too)."""
        self._updating = True
        wanted = with_requirements(self.checked)

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
            for i in range(self.topLevelItemCount()):
                top = self.topLevelItem(i)
                if top.flags() & Qt.ItemIsUserCheckable:
                    sync(top)
        finally:
            self._updating = False

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int):
        if self._updating:
            return
        kind, ident = item.data(0, KIND_ROLE), item.data(0, ID_ROLE)
        checked = item.checkState(0) == Qt.Checked
        if kind == "tool":
            (self.checked.add if checked else self.checked.discard)(ident)
        elif kind in ("server", "group"):
            ids = set(_leaf_ids(item))
            self.checked = (self.checked | ids) if checked else (self.checked - ids)
        else:
            return
        self.sync()
        self.toggled.emit()
