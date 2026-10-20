import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLabel, QFileDialog,
)

from core import db, docs, files
from core.files import Selection
from ui import themes
from worker import AsyncCall

PATH_ROLE = Qt.UserRole
KIND_ROLE = Qt.UserRole + 1  # 'root' | 'dir' | 'file' | 'placeholder' | 'note'
CHECK_STATES = {files.CHECKED: Qt.Checked, files.PARTIAL: Qt.PartiallyChecked, files.UNCHECKED: Qt.Unchecked}
COLOR_NORMAL, COLOR_WARN, COLOR_OVER = themes.DARK.normal, themes.DARK.warn, themes.DARK.over  # цвета тёмной темы (по умолчанию)


def _dim() -> QBrush:
    return QBrush(QColor(themes.current().dim))
WARN_SHARE = 0.8  # доля лимита контекста, после которой строка становится жёлтой
REVEAL_MAX = 30   # сколько отмеченных путей раскрывать в дереве при загрузке чата


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


class FilesPanel(QWidget):
    """Корни-папки, дерево файлов с галочками и оценка размера контекста."""

    selection_changed = Signal()

    def __init__(self):
        super().__init__()
        self.roots: list[str] = db.list_roots()
        self.selection = Selection()
        self._updating = False
        self._summary: dict | None = None
        self._summary_error = ""
        self._extra_tokens = 0
        self._limit_info: tuple | None = None
        self._note = ""
        self._scale = 1.0              # поправка нашей оценки по замерам сервера
        self._actual: int | None = None  # сколько токенов в последнем запросе насчитал сервер

        self.add_btn = QPushButton("+ Папка")
        self.remove_btn = QPushButton("− Папка")
        self.refresh_btn = QPushButton("Обновить")
        buttons = QHBoxLayout()
        for b in (self.add_btn, self.remove_btn, self.refresh_btn):
            buttons.addWidget(b)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Файлы и папки")
        self.info = QLabel("")
        self.info.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.info)

        self.summary_req = AsyncCall(files.summarize, self)
        self.summary_req.TIMEOUT_MS = 30000
        self.summary_req.TIMEOUT_TEXT = "Подсчёт занял больше {seconds:g} с"
        self.summary_req.succeeded.connect(self._on_summary)
        self.summary_req.failed.connect(self._on_summary_failed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._start_summary)

        self.add_btn.clicked.connect(self._add_root)
        self.remove_btn.clicked.connect(self._remove_root)
        self.refresh_btn.clicked.connect(self.rebuild)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        themes.bus.changed.connect(lambda _theme: (self.rebuild(), self._render()))
        self.rebuild()

    # --- дерево ---

    def rebuild(self):
        """Перестроить дерево с диска, сохранив раскрытые папки."""
        expanded = self._expanded_paths()
        self._updating = True
        self.tree.clear()
        for root in self.roots:
            self._make_root_item(root)
        self._updating = False
        for path in sorted(expanded, key=len):
            self._reveal(path, expand_last=True)
        self._sync_all()
        self._update_buttons()
        self._schedule_summary()

    def _expanded_paths(self) -> set[str]:
        out = set()
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.isExpanded() and item.data(0, KIND_ROLE) in ("root", "dir"):
                out.add(item.data(0, PATH_ROLE))
                stack += [item.child(i) for i in range(item.childCount())]
        return out

    def _make_root_item(self, path: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self.tree, [os.path.basename(path.rstrip("\\/")) or path])
        self._setup(item, path, "root", checkable=os.path.isdir(path))
        item.setToolTip(0, path)
        if os.path.isdir(path):
            self._service_item(item, "placeholder")
        else:
            item.setText(0, item.text(0) + "  (папка недоступна)")
            item.setForeground(0, _dim())
        return item

    @staticmethod
    def _service_item(parent: QTreeWidgetItem, kind: str, text: str = "") -> QTreeWidgetItem:
        """Служебная строка (заглушка ленивой загрузки или заметка): без галочки и без выбора."""
        item = QTreeWidgetItem(parent, [text])
        item.setData(0, KIND_ROLE, kind)
        item.setFlags(Qt.ItemIsEnabled)
        if kind == "note":
            item.setForeground(0, _dim())
        return item

    @staticmethod
    def _setup(item: QTreeWidgetItem, path: str, kind: str, checkable: bool):
        item.setData(0, PATH_ROLE, path)
        item.setData(0, KIND_ROLE, kind)
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable  # явно: флаги по умолчанию разрешают галочку
        if checkable:
            flags |= Qt.ItemIsUserCheckable
            item.setCheckState(0, Qt.Unchecked)
        item.setFlags(flags)

    def _populate(self, item: QTreeWidgetItem):
        path = item.data(0, PATH_ROLE)
        self._updating = True
        item.takeChildren()
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError as e:
            self._service_item(item, "note", f"(нет доступа: {e.strerror or e})")
            self._updating = False
            return
        for e in entries:
            is_dir = e.is_dir(follow_symlinks=False)
            if is_dir and files.is_ignored_dir(e.name):
                continue
            child = QTreeWidgetItem(item, [e.name])
            if is_dir:
                self._setup(child, e.path, "dir", checkable=True)
                self._service_item(child, "placeholder")
                continue
            reason = self._file_problem(e)
            self._setup(child, e.path, "file", checkable=not reason)
            if reason:
                child.setText(0, f"{e.name}  ({reason})")
                child.setForeground(0, _dim())
            elif docs.is_document_name(e.name):
                child.setText(0, f"{e.name}  (документ: в запрос идёт извлечённый текст)")
            elif files.looks_binary_by_name(e.name):
                child.setText(0, f"{e.name}  ({'картинка' if files.is_image_name(e.name) else 'бинарный'}: в запрос идёт путь)")
        self._updating = False
        self._sync_item(item)

    @staticmethod
    def _file_problem(entry: os.DirEntry) -> str:
        try:
            size = entry.stat().st_size
        except OSError:
            return "нет доступа"
        limit = docs.MAX_DOC_BYTES if docs.is_document_name(entry.name) else files.MAX_FILE_BYTES
        return f"слишком большой, {size // 1024} КБ" if size > limit else ""

    def _on_expanded(self, item: QTreeWidgetItem):
        if item.childCount() == 1 and item.child(0).data(0, KIND_ROLE) == "placeholder":
            self._populate(item)

    def _reveal(self, path: str, expand_last: bool = False):
        """Раскрыть в дереве путь до path (для отмеченных узлов выбранного чата)."""
        root = max((r for r in self.roots if path == r or path.startswith(r + os.sep)), key=len, default=None)
        if root is None:
            return
        item = next((self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
                     if self.tree.topLevelItem(i).data(0, PATH_ROLE) == root), None)
        parts = [] if path == root else os.path.relpath(path, root).split(os.sep)
        targets = parts if expand_last else parts[:-1]
        current = root
        for part in [None] + targets:
            if part is not None:
                current = os.path.join(current, part)
                item = next((item.child(i) for i in range(item.childCount())
                             if item.child(i).data(0, PATH_ROLE) == current), None)
            if item is None:
                return
            if item.data(0, KIND_ROLE) in ("root", "dir"):
                self._on_expanded(item)
                item.setExpanded(True)

    # --- выбор ---

    def set_selection(self, sel: Selection):
        """Показать выбор чата (без сигнала selection_changed)."""
        self.selection = sel.copy()
        self.selection.prune(self.roots)
        for path in sorted(self.selection.include)[:REVEAL_MAX]:
            self._reveal(path)
        self._sync_all()
        self._schedule_summary()

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int):
        if self._updating or item.data(0, KIND_ROLE) not in ("root", "dir", "file") \
                or not (item.flags() & Qt.ItemIsUserCheckable):
            return
        self.selection.toggle(item.data(0, PATH_ROLE), item.checkState(0) == Qt.Checked)
        self._sync_all()
        self._schedule_summary()
        self.selection_changed.emit()

    def _sync_all(self):
        for i in range(self.tree.topLevelItemCount()):
            self._sync_item(self.tree.topLevelItem(i))

    def _sync_item(self, item: QTreeWidgetItem):
        self._updating = True
        try:
            stack = [item]
            while stack:
                it = stack.pop()
                if it.data(0, KIND_ROLE) in ("root", "dir", "file") and it.flags() & Qt.ItemIsUserCheckable:
                    it.setCheckState(0, CHECK_STATES[self.selection.state(it.data(0, PATH_ROLE))])
                stack += [it.child(i) for i in range(it.childCount())]
        finally:
            self._updating = False

    # --- корни ---

    def _update_buttons(self):
        self.remove_btn.setEnabled(self.tree.currentItem() is not None)

    def _add_root(self):
        path = QFileDialog.getExistingDirectory(self, "Папка с файлами для контекста")
        if path:
            self.add_root(os.path.abspath(path))

    def add_root(self, path: str) -> bool:
        key = os.path.normcase(path)
        for r in self.roots:
            rk = os.path.normcase(r)
            if key == rk or key.startswith(rk + os.sep):
                self._set_note(f"Папка уже входит в «{os.path.basename(r) or r}»")
                return False
            if rk.startswith(key + os.sep):
                self._set_note(f"Внутри уже добавлена «{os.path.basename(r) or r}»: сначала убери её")
                return False
        db.add_root(path)
        self.roots = db.list_roots()
        self._set_note("")
        self.rebuild()
        return True

    def _remove_root(self):
        item = self.tree.currentItem()
        while item is not None and item.parent() is not None:
            item = item.parent()
        if item is None:
            return
        path = item.data(0, PATH_ROLE)
        db.remove_root(path)
        self.roots = db.list_roots()
        had = bool(self.selection.include)
        self.selection.prune(self.roots)
        self._set_note(f"Папка «{os.path.basename(path) or path}» убрана из списка (файлы на диске не тронуты)")
        self.rebuild()
        if had:
            self.selection_changed.emit()

    def _set_note(self, text: str):
        self._note = text
        self._render()

    # --- оценка размера ---

    def set_extra_tokens(self, tokens: int):
        """Токены, которые занимает всё остальное в запросе (system, память, история)."""
        self._extra_tokens = tokens
        self._render()

    def set_scale(self, scale: float):
        """Поправка оценки токенов, накопленная по настоящим замерам сервера для выбранной модели."""
        self._scale = scale
        self._render()

    def set_actual(self, prompt_tokens: int | None):
        """Сколько токенов в последнем запросе этого чата насчитал сервер (None, если ещё не было)."""
        self._actual = prompt_tokens
        self._render()

    def set_limit(self, info: tuple | None):
        """info = (рабочий лимит | None, модель загружена, максимум контекста | None)."""
        self._limit_info = info
        self._render()

    def load_bundle(self) -> files.ContextBundle:
        """Прочитать выбранные файлы для отправки (в главном потоке: размер ограничен лимитами)."""
        return files.load_context(self.selection, self.roots)

    def wait_threads(self):
        self.summary_req.wait()

    def _schedule_summary(self):
        self._summary = None
        self._summary_error = ""
        if self.selection.is_empty():
            self.summary_req.cancel()
            self._debounce.stop()
        else:
            self._debounce.start()
        self._render()

    def _start_summary(self):
        self.summary_req.start(
            frozenset(self.selection.include), frozenset(self.selection.exclude), tuple(self.roots)
        )
        self._render()

    def _on_summary(self, summary: dict):
        self._summary = summary
        self._render()

    def _on_summary_failed(self, message: str):
        self._summary_error = message
        self._render()

    def _render(self):
        lines = []
        scale = self._scale
        tokens = int(self._extra_tokens * scale)
        if self._note:
            lines.append(self._note)
        if self.selection.is_empty():
            lines.append("Файлы не выбраны")
        elif self._summary is not None:
            s = self._summary
            files_tokens = int(s["tokens"] * scale)
            tokens += files_tokens
            line = f"Файлов: {s['files']} · ≈{_fmt(files_tokens)} ток."
            if s["skipped"]:
                line += f" · пропущено: {s['skipped']}"
            if s["truncated"]:
                line += " · выбрано слишком много, отправка будет отклонена"
            lines.append(line)
        elif self._summary_error:
            lines.append(f"Не удалось посчитать: {self._summary_error}")
        else:
            lines.append("Считаю…")
        limit = self._limit_info
        total = f"Запрос целиком ≈{_fmt(tokens)} ток."
        th = themes.current()
        color = th.normal
        if limit and limit[0]:
            total += f" из {_fmt(limit[0])}"
            color = th.over if tokens > limit[0] else th.warn if tokens > limit[0] * WARN_SHARE else color
        elif limit and not limit[1] and limit[2]:
            total += f" (лимит неизвестен: модель не загружена, максимум {_fmt(limit[2])})"
        lines.append(total)
        if self._actual:
            lines.append(f"Последний запрос по счёту сервера: {_fmt(self._actual)} ток.")
        if abs(scale - 1.0) > 0.05:
            lines.append(f"Оценка скорректирована по замерам сервера (×{scale:.2f}).")
        self.info.setText("\n".join(lines))
        self.info.setStyleSheet(f"color: {color};")
