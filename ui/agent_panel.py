"""The Agent tab: create remote agents, see their state, revoke or delete them.

Creating an agent makes a one-time enrollment token and writes a per-agent
``installer.ps1`` the user runs on the target host. The list shows each agent's
status (pending / active / revoked) and whether it is connected right now. The
listener (the bridge) is started/stopped here; while it runs, connected hosts'
tools appear in the catalog (see ui.tools_panel / core.agent.manager).

Dialog-free logic (``create_agent``, ``revoke``, ``delete``, ``start_listener`` …) is kept
separate from the button handlers so it can be driven headless in tests.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPushButton, QLabel,
    QTreeWidget, QTreeWidgetItem, QInputDialog, QMessageBox, QFileDialog,
)

from core import db
from core.agent import enroll, installer
from core.agent.registry import Registry, RegistryInputError
from ui import themes

ADDRESS_KEY = "agent_address"
PORT_KEY = "agent_port"
DEFAULT_PORT = "8765"
BIND_HOST = "0.0.0.0"  # reachable from the LAN; the address field is what hosts dial

ID_ROLE = Qt.UserRole


class AgentPanel(QWidget):
    def __init__(self, agent_manager, identity: dict):
        super().__init__()
        self.agent = agent_manager
        self.identity = identity

        self.address = QLineEdit(db.get_setting(ADDRESS_KEY))
        self.address.setPlaceholderText("How hosts reach this machine, e.g. 192.168.1.10:8765")
        self.port = QLineEdit(db.get_setting(PORT_KEY) or DEFAULT_PORT)
        self.port.setPlaceholderText(DEFAULT_PORT)
        self.port.setMaximumWidth(90)
        self.listen_btn = QPushButton("Start listener")
        addr_form = QFormLayout()
        addr_form.addRow("Address", self.address)
        port_row = QHBoxLayout()
        port_row.addWidget(self.port)
        port_row.addWidget(self.listen_btn)
        addr_form.addRow("Port", port_row)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Agent", "Status", "Connected", "OS"])
        self.tree.setRootIsDecorated(False)

        self.new_btn = QPushButton("New agent…")
        self.revoke_btn = QPushButton("Revoke")
        self.delete_btn = QPushButton("Delete")
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.new_btn)
        btn_row.addWidget(self.revoke_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(addr_form)
        layout.addWidget(self.status)
        layout.addWidget(self.tree, 1)
        layout.addLayout(btn_row)

        self.listen_btn.clicked.connect(self._toggle_listener)
        self.new_btn.clicked.connect(self._new_agent)
        self.revoke_btn.clicked.connect(self._revoke_selected)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.address.editingFinished.connect(lambda: db.set_setting(ADDRESS_KEY, self.address.text().strip()))
        self.port.editingFinished.connect(lambda: db.set_setting(PORT_KEY, self.port.text().strip()))
        themes.bus.changed.connect(lambda _t: self._render_status())

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    # --- dialog-free logic (used by tests and the handlers) ----------------

    def _registry(self) -> Registry:
        return Registry(sqlite3.connect(db.DB_PATH))

    def _installer_address(self) -> str:
        """Host-reachable address for the installer: append the port if none was typed."""
        addr = self.address.text().strip() or "MAIN-ADDRESS"
        if ":" not in addr:
            addr = f"{addr}:{self.port.text().strip() or DEFAULT_PORT}"
        return addr

    def create_agent(self, name: str, out_path: str) -> str:
        """Create (or re-arm a still-pending) agent and write its installer. Returns the token.

        Re-generating for an agent that has not enrolled yet reissues its token; a name
        already taken by an active or revoked agent raises RegistryInputError.
        """
        with closing(self._registry()) as reg:
            existing = reg.get_by_name(name)
            if existing is not None and existing["status"] == "pending":
                token = enroll.reissue(reg, existing["id"])
            elif existing is not None:
                raise RegistryInputError(f'the name "{name}" is already taken by a {existing["status"]} agent')
            else:
                _, token = enroll.create_agent(reg, name)
        script = installer.render(self._installer_address(), self.identity["ca_cert_pem"], token, name)
        Path(out_path).write_text(script, encoding="utf-8")
        return token

    def revoke(self, agent_id: int) -> bool:
        with closing(self._registry()) as reg:
            done = reg.revoke(agent_id)
        self.refresh()
        return done

    def delete(self, agent_id: int) -> bool:
        """Remove the agent's record and drop its live connection; the name becomes free again."""
        with closing(self._registry()) as reg:
            row = reg.get(agent_id)
            done = reg.delete(agent_id)
        if done and self.agent is not None:
            self.agent.disconnect(row["name"])
        self.refresh()
        return done

    def start_listener(self):
        port = int(self.port.text().strip() or DEFAULT_PORT)
        self.agent.start(BIND_HOST, port)

    def stop_listener(self):
        self.agent.stop()

    # --- view ---------------------------------------------------------------

    def refresh(self):
        connected = set(self.agent.connected_names()) if self.agent is not None else set()
        with closing(self._registry()) as reg:
            rows = reg.list()
        selected = self._selected_id()
        self.tree.clear()
        for r in rows:
            item = QTreeWidgetItem([
                r["name"], r["status"],
                "yes" if r["name"] in connected else "", r["os"] or "",
            ])
            item.setData(0, ID_ROLE, r["id"])
            if r["fingerprint"]:
                item.setToolTip(0, f"fingerprint {r['fingerprint'][:16]}…")
            self.tree.addTopLevelItem(item)
            if r["id"] == selected:
                self.tree.setCurrentItem(item)
        self._render_status()

    def _render_status(self):
        if self.agent is not None and self.agent.is_running():
            self.status.setText(f"Listening on port {self.port.text().strip() or DEFAULT_PORT}. "
                                "Hosts reach this machine at the address above.")
        else:
            self.status.setText("Listener stopped. Start it so agents can connect.")
        self.listen_btn.setText("Stop listener" if (self.agent and self.agent.is_running()) else "Start listener")

    def _selected_id(self):
        item = self.tree.currentItem()
        return item.data(0, ID_ROLE) if item is not None else None

    # --- handlers -----------------------------------------------------------

    def _toggle_listener(self):
        try:
            if self.agent.is_running():
                self.stop_listener()
            else:
                self.start_listener()
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Listener", f"Could not start the listener: {e}")
        self._render_status()

    def _new_agent(self):
        name, ok = QInputDialog.getText(self, "New agent", "Agent name (letters, digits, '-', '_'):")
        if not ok or not name.strip():
            return
        default = str(db.DB_PATH.parent / "agent" / f"installer_{name.strip()}.ps1")
        out_path, _ = QFileDialog.getSaveFileName(self, "Save installer", default, "PowerShell (*.ps1)")
        if not out_path:
            return
        try:
            token = self.create_agent(name.strip(), out_path)
        except RegistryInputError as e:
            QMessageBox.warning(self, "New agent", str(e))
            return
        self.refresh()
        QMessageBox.information(
            self, "Agent created",
            f"Installer written to:\n{out_path}\n\nRun it on the target host. One-time enrollment token:\n\n{token}\n\n"
            "The token works once and is not stored in full.",
        )

    def _revoke_selected(self):
        item = self.tree.currentItem()
        if item is None:
            return
        name = item.text(0)
        if QMessageBox.question(self, "Revoke agent",
                                f"Revoke \"{name}\"? Its certificate will stop being accepted.") == QMessageBox.Yes:
            self.revoke(item.data(0, ID_ROLE))

    def _delete_selected(self):
        item = self.tree.currentItem()
        if item is None:
            return
        name = item.text(0)
        if QMessageBox.question(self, "Delete agent",
                                f"Delete \"{name}\"? It is disconnected and removed from the list; "
                                "the host must be reinstalled to connect again.") == QMessageBox.Yes:
            self.delete(item.data(0, ID_ROLE))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
