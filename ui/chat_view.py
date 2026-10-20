import json
from html import escape

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap, QTextBlockFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPlainTextEdit, QPushButton, QLabel, QCheckBox, QFileDialog,
)

from core import imagegen, images
from core.richtext import render_markdown
from core.vision import IMAGE_EXTENSIONS
from core.tools import describe_call
from ui import themes

ROLE_LABELS = {"user": "Вы", "assistant": "Модель", "system": "Система"}
TOOL_RESULT_PREVIEW = 600  # сколько символов результата инструмента показывать в чате
RENDER_INTERVAL_MS = 60  # во время стрима перерисовываем не чаще этого
STICK_QUIET_MS = 400     # сколько мс диапазон прокрутки не должен расти, чтобы отпустить нижний край
THUMB_WIDTH = 180        # ширина миниатюры картинки в истории чата
GENERATED_WIDTH = 320    # ширина картинки, которую нарисовал image_generate
PREVIEW_SIZE = 56        # размер превью приложенной картинки над полем ввода
IMAGE_FILTER = "Изображения (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;Все файлы (*)"


def _image_files(mime) -> list[str]:
    """Пути картинок из перетащенных или вставленных файлов."""
    return [u.toLocalFile() for u in mime.urls()
            if u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower() in IMAGE_EXTENSIONS]


class _Input(QPlainTextEdit):
    """Enter отправляет, Shift+Enter переносит строку."""

    submit = Signal()
    media = Signal(object)  # QMimeData с картинкой или файлами-картинками (вставка Ctrl+V, перетаскивание)

    def canInsertFromMimeData(self, source):
        return source.hasImage() or bool(_image_files(source)) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasImage() or _image_files(source):
            self.media.emit(source)  # обработчик отрабатывает сразу: QMimeData живёт только во время вставки
            return
        super().insertFromMimeData(source)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & Qt.ShiftModifier:
            self.submit.emit()
            return
        super().keyPressEvent(e)


def _text_html(text: str) -> str:
    return escape(text).replace("\n", "<br>")


class ChatView(QWidget):
    message_submitted = Signal(str)
    stop_requested = Signal()
    request_view_requested = Signal()

    def __init__(self):
        super().__init__()
        self._blocks: list[dict] = []  # {role, content, reasoning, live, images}
        self.pending: list[str] = []   # пути приложенных, но ещё не отправленных картинок
        self._dirty = False
        self._live_pos: int | None = None  # позиция в документе, с которой начинается живой блок
        self._first_format_cache: dict[str, QTextBlockFormat] = {}
        # Qt достраивает раскладку длинного документа по частям, и диапазон прокрутки растёт уже после загрузки.
        # Пока он растёт, держим нижний край; пользователь прервёт это колесом, мышью или клавишей.
        self._stick_bottom = False

        layout = QVBoxLayout(self)
        self.title = QLabel("Диалог")
        self.title.setObjectName("title")
        self.show_reasoning = QCheckBox("Размышления")
        self.show_reasoning.setChecked(True)
        self.request_btn = QPushButton("Запрос")
        self.request_btn.setToolTip("Показать, что ушло модели в последнем запросе (system prompt, история)")
        header = QHBoxLayout()
        header.addWidget(self.title, 1)
        header.addWidget(self.show_reasoning)
        header.addWidget(self.request_btn)

        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.document().setDefaultStyleSheet(themes.current().richtext_css())
        self.input = _Input()
        self.input.setPlaceholderText("Сообщение... (Enter — отправить, Shift+Enter — новая строка)")
        self.input.setFixedHeight(90)
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.attach_btn = QPushButton("📎")
        self.attach_btn.setToolTip("Приложить изображение (можно вставить Ctrl+V или перетащить в поле ввода)")
        self.attach_btn.setFixedWidth(40)
        self.preview = QWidget()  # полоса превью приложенных картинок
        self.preview_row = QHBoxLayout(self.preview)
        self.preview_row.setContentsMargins(0, 0, 0, 0)
        self.preview.hide()
        self.send_btn = QPushButton("Отправить")
        self.stop_btn = QPushButton("Стоп")
        self.stop_btn.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.attach_btn)
        row.addWidget(self.status, 1)
        row.addWidget(self.stop_btn)
        row.addWidget(self.send_btn)
        layout.addLayout(header)
        layout.addWidget(self.history, 1)
        layout.addWidget(self.preview)
        layout.addWidget(self.input)
        layout.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_INTERVAL_MS)
        self._timer.timeout.connect(self._flush)

        self._stick_timer = QTimer(self)
        self._stick_timer.setSingleShot(True)
        self._stick_timer.setInterval(STICK_QUIET_MS)
        self._stick_timer.timeout.connect(lambda: setattr(self, "_stick_bottom", False))
        self.history.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        for source in (self.history, self.history.viewport(), self.history.verticalScrollBar()):
            source.installEventFilter(self)

        themes.bus.changed.connect(self._on_theme)
        self.send_btn.clicked.connect(self._submit)
        self.input.submit.connect(self._submit)
        self.input.media.connect(self.attach_from_mime)
        self.attach_btn.clicked.connect(self._choose_files)
        self.stop_btn.clicked.connect(self.stop_requested)
        self.request_btn.clicked.connect(self.request_view_requested)
        self.show_reasoning.toggled.connect(lambda _on: self._render())

    # --- прокрутка ---

    def _on_range_changed(self, _low: int, high: int):
        if self._stick_bottom:
            self.history.verticalScrollBar().setValue(high)
            self._stick_timer.start()  # прилипание заканчивается, когда диапазон перестал расти

    def eventFilter(self, obj, event):
        if self._stick_bottom and event.type() in (QEvent.Wheel, QEvent.MouseButtonPress, QEvent.KeyPress):
            self._stick_bottom = False  # пользователь взял прокрутку на себя
        return super().eventFilter(obj, event)

    # --- ввод ---

    def _submit(self):
        """Отправка. Текст из поля убирает владелец (после проверки), чтобы не терять ввод."""
        text = self.input.toPlainText().strip()
        if (text or self.pending) and self.send_btn.isEnabled():
            self.message_submitted.emit(text)

    # --- приложенные картинки ---

    def _choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Приложить изображение", "", IMAGE_FILTER)
        self.attach_files(paths)

    def attach_from_mime(self, mime):
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.attach_image(image)
                return
        self.attach_files(_image_files(mime))

    def attach_files(self, paths):
        for path in paths:
            image = QImage(path)
            if image.isNull():
                self.set_status(f"Не удалось открыть изображение: {os.path.basename(path)}")
                continue
            self.attach_image(image, jpeg=os.path.splitext(path)[1].lower() in (".jpg", ".jpeg"))

    def attach_image(self, image: QImage, jpeg: bool = False):
        """Сохраняет картинку в data/images (крупную уменьшает) и добавляет её к приложенным."""
        if len(self.pending) >= images.MAX_IMAGES_PER_MESSAGE:
            self.set_status(f"К одному сообщению можно приложить до {images.MAX_IMAGES_PER_MESSAGE} изображений")
            return
        if max(image.width(), image.height()) > images.MAX_SIDE:
            image = image.scaled(images.MAX_SIDE, images.MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        path = images.new_image_path(".jpg" if jpeg else ".png")
        if not image.save(str(path), "JPG" if jpeg else "PNG", 90 if jpeg else -1):
            self.set_status("Не удалось сохранить изображение в data/images")
            return
        self.pending.append(str(path))
        self._rebuild_preview()

    def remove_pending(self, path: str):
        if path in self.pending:
            self.pending.remove(path)
            self._rebuild_preview()

    def clear_pending(self):
        self.pending = []
        self._rebuild_preview()

    def _rebuild_preview(self):
        while self.preview_row.count():
            item = self.preview_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for path in self.pending:
            thumb = QLabel()
            thumb.setPixmap(QPixmap(path).scaled(QSize(PREVIEW_SIZE, PREVIEW_SIZE), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            thumb.setToolTip(os.path.basename(path))
            remove = QPushButton("×")
            remove.setFixedSize(22, 22)
            remove.setToolTip("Убрать изображение")
            remove.clicked.connect(lambda _=False, p=path: self.remove_pending(p))
            self.preview_row.addWidget(thumb)
            self.preview_row.addWidget(remove)
        self.preview_row.addStretch(1)
        self.preview.setVisible(bool(self.pending))

    def set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    def set_status(self, text: str):
        self.status.setText(text)

    def set_title(self, text: str):
        self.title.setText(text)

    # --- история ---

    def set_messages(self, messages):
        self._blocks = [self._block_from_row(m) for m in messages]
        self._render(scroll_to_end=True)

    @staticmethod
    def _block_from_row(m) -> dict:
        def get(key):
            try:
                return m[key] or ""
            except (KeyError, IndexError):
                return ""
        calls = get("tool_calls")
        return {
            "role": m["role"], "content": m["content"], "reasoning": get("reasoning"), "live": False,
            "tool_calls": json.loads(calls) if calls else [], "tool_name": get("tool_name"),
            "images": images.decode_paths(get("images")),
        }

    def append_message(self, role: str, content: str, reasoning: str = "", image_paths=None):
        self._blocks.append({"role": role, "content": content, "reasoning": reasoning, "live": False,
                             "tool_calls": [], "tool_name": "", "images": list(image_paths or [])})
        self._render(scroll_to_end=True)

    def begin_live(self, reasoning: str = "", content: str = ""):
        """Начинает блок ответа модели, который будет дополняться по мере стрима."""
        self._blocks.append({"role": "assistant", "content": content, "reasoning": reasoning, "live": True,
                             "tool_calls": [], "tool_name": ""})
        self._render(scroll_to_end=True)

    def append_delta(self, kind: str, text: str):
        if not self._blocks or not self._blocks[-1]["live"]:
            return
        key = "reasoning" if kind == "reasoning" else "content"
        self._blocks[-1][key] += text
        self._dirty = True
        if not self._timer.isActive():
            self._timer.start()

    def end_live(self, keep: bool = True):
        """Завершает живой блок. keep=False убирает его (если ответ пустой)."""
        self._timer.stop()
        self._dirty = False
        live = self._live_block()
        if live is None or self._live_pos is None:
            self._render()
            return
        if keep:
            self._render_live()  # показать последние накопленные куски
            live["live"] = False
        else:
            cursor = QTextCursor(self.history.document())
            cursor.setPosition(self._live_pos)
            cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            self._blocks.pop()
        self._live_pos = None

    def _on_theme(self, theme):
        """Смена темы: стили документа и цвета подписей в уже показанной истории."""
        self.history.document().setDefaultStyleSheet(theme.richtext_css())
        self._first_format_cache.clear()
        self._render()

    def _flush(self):
        if self._dirty:
            self._dirty = False
            self._render_live()

    def _block_html(self, b: dict) -> str:
        if b["role"] == "tool":
            return self._tool_result_html(b)
        th = themes.current()
        color = {"user": th.user, "assistant": th.assistant}.get(b["role"], th.system)
        label = ROLE_LABELS.get(b["role"], b["role"])
        out = [f'<p style="margin-bottom:0"><b style="color:{color}">{escape(label)}</b></p>']
        if b["reasoning"] and self.show_reasoning.isChecked():
            out.append(
                f'<div style="color:{th.reasoning}"><i>Размышления:</i>{self._md(b, "reasoning")}</div>'
            )
        if b["content"]:
            out.append(self._md(b, "content") if b["role"] == "assistant" else f"<p>{_text_html(b['content'])}</p>")
        elif b["live"] and not b["reasoning"]:
            out.append(f'<p style="color:{th.reasoning}">…</p>')
        if b.get("images"):
            out.append(self._images_html(b["images"]))
        for call in b["tool_calls"]:
            fn = call.get("function", {})
            out.append(
                f'<p style="color:{th.tool}">▸ {escape(describe_call(fn.get("name", "?"), fn.get("arguments", "")))}</p>'
            )
        return "".join(out)

    @staticmethod
    def _images_html(paths: list[str], width: int = THUMB_WIDTH) -> str:
        """Миниатюры приложенных картинок (файл мог исчезнуть с диска: тогда подпись вместо картинки)."""
        parts = []
        for path in paths:
            if os.path.exists(path):
                parts.append(f'<img src="{Path(path).as_uri()}" width="{width}">')
            else:
                parts.append(f'<i style="color:{themes.current().reasoning}">[изображение {escape(os.path.basename(path))} не найдено]</i>')
        return "<p>" + "&nbsp;".join(parts) + "</p>"

    @staticmethod
    def _tool_result_html(b: dict) -> str:
        """Результат инструмента: кратко и приглушённо (полный текст виден в окне «Запрос»)."""
        text = b["content"]
        shown = text if len(text) <= TOOL_RESULT_PREVIEW else text[:TOOL_RESULT_PREVIEW].rstrip() + f"… (ещё {len(text) - TOOL_RESULT_PREVIEW} симв.)"
        failed = text.startswith("ОШИБКА")
        th = themes.current()
        color = th.tool_error if failed else th.reasoning
        picture = imagegen.result_path(text)  # результат image_generate: под текстом показываем саму картинку
        extra = ChatView._images_html([picture], GENERATED_WIDTH) if picture else ""
        return (f'<p style="color:{color}"><b>◂ {escape(b["tool_name"] or "результат")}</b><br>'
                f'{_text_html(shown)}</p>{extra}')

    @staticmethod
    def _md(b: dict, field: str) -> str:
        """Markdown -> HTML. Для завершённых сообщений результат кешируется в блоке."""
        text = b[field]
        if b["live"]:
            return render_markdown(text)
        cache = b.setdefault("_cache", {})
        hit = cache.get(field)
        if hit is None or hit[0] != text:
            hit = cache[field] = (text, render_markdown(text))
        return hit[1]

    def _live_block(self) -> dict | None:
        return self._blocks[-1] if self._blocks and self._blocks[-1]["live"] else None

    def _render(self, scroll_to_end: bool = False):
        """Полная перерисовка. Нужна при структурных изменениях; во время стрима обновляется только живой блок."""
        bar = self.history.verticalScrollBar()
        at_bottom = scroll_to_end or bar.value() >= bar.maximum() - 4
        old = bar.value()
        live = self._live_block()
        static = self._blocks[:-1] if live else self._blocks
        # хвостовой перевод строки в разметке Qt превращает в лишний пробел в конце последнего абзаца
        self.history.setHtml("".join(self._block_html(b) for b in static).rstrip())
        self._live_pos = None
        if live is not None:
            cursor = QTextCursor(self.history.document())
            cursor.movePosition(QTextCursor.End)
            self._live_pos = cursor.position()
            self._set_live_html(self._block_html(live))
        bar.setValue(bar.maximum() if at_bottom else old)
        if scroll_to_end:
            self._stick_bottom = True
            self._stick_timer.start()

    def _set_live_html(self, html: str):
        """Заменяет разметку живого блока на месте, остальной документ не трогает."""
        doc = self.history.document()
        cursor = QTextCursor(doc)
        cursor.setPosition(self._live_pos)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        if self._live_pos > 0:
            cursor.insertBlock()  # иначе первый абзац склеится с последним абзацем предыдущего блока
        start = cursor.position()
        html = html.rstrip()
        cursor.insertHtml(html)
        # Qt оставляет первому вставленному абзацу формат блока-предшественника (теряются отступы, например
        # margin-bottom:0 у подписи «Модель»), поэтому применяем формат, который задан в самой разметке
        fmt_cursor = QTextCursor(doc)
        fmt_cursor.setPosition(start)
        fmt_cursor.setBlockFormat(self._first_block_format(html))

    def _first_block_format(self, html: str) -> QTextBlockFormat:
        """Формат блока первого абзаца разметки (кешируется по открывающему тегу: он у блоков всегда один и тот же)."""
        tag = html[: html.index(">") + 1] if html.startswith("<") else ""
        fmt = self._first_format_cache.get(tag)
        if fmt is None:
            tmp = QTextDocument()
            tmp.setDefaultStyleSheet(themes.current().richtext_css())
            tmp.setHtml(f"{tag}x</p>" if tag.startswith("<p") else html)
            fmt = tmp.begin().blockFormat()
            fmt.setObjectIndex(-1)  # ссылка на список из временного документа в этом документе недействительна
            self._first_format_cache[tag] = fmt
        return fmt

    def _render_live(self):
        """Обновление при стриме: меняется только живой блок, положение прокрутки сохраняется.

        Раньше на каждый кусок текста заново собирался весь документ (setHtml): он сбрасывает прокрутку
        и перекладывает всё, отсюда дёргание и «съеденное» колесо мыши.
        """
        live = self._live_block()
        if live is None or self._live_pos is None:
            self._render()
            return
        bar = self.history.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4  # пользователь у нижнего края: следим за текстом
        old = bar.value()
        self._set_live_html(self._block_html(live))
        bar.setValue(bar.maximum() if at_bottom else old)
