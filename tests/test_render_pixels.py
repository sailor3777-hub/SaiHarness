"""Вёрстка после стрима должна быть пиксель в пиксель такой же, как при полной перерисовке."""
import json
from PySide6.QtWidgets import QApplication
from ui.chat_view import ChatView

app = QApplication([])
def spin(n=5):
    for _ in range(n): app.processEvents()
def row(role, content, reasoning="", tool_calls="", tool_name=""):
    return {"role": role, "content": content, "reasoning": reasoning, "tool_calls": tool_calls, "tool_call_id": "", "tool_name": tool_name}
CALLS = json.dumps([{"id": "c", "type": "function", "function": {"name": "call_tool", "arguments": '{"tool": "tb.calc", "arguments": {"expression": "1+1"}}'}}])
HIST = [row("user", "Что в файле?"), row("assistant", "Вот список:\n\n- один\n- два\n\nИ **итог**.", "Подумаю.\n1. раз\n2. два")]
R = "Нужно прочитать файл.\n\n1. найду путь\n2. прочитаю\n"
C = "## Результат\n\nВ файле написано `секрет`.\n\n```python\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- первый\n- второй"
TOOLS = [row("assistant", "", "ищу", CALLS), row("tool", "2", "", "", "tb.calc")]

SCENARIOS = {
    "после обычных сообщений": (HIST + [row("user", "ещё вопрос")], R, C),
    "чат пуст (живой блок первый)": ([], R, C),
    "после вызова и результата инструмента": (HIST + [row("user", "q")] + TOOLS, R, C),
    "только размышления": (HIST + [row("user", "q")], R, ""),
    "только ответ": (HIST + [row("user", "q")], "", C),
    "длинная история": ((HIST + [row("user", "q")]) * 15 + [row("user", "ещё вопрос")], R, C),
}
ok_all = True
for name, (static, r, c) in SCENARIOS.items():
    ref = ChatView(); ref.resize(800, 700); ref.show(); ref.set_messages(static + [row("assistant", c, r)]); spin()
    s = ChatView(); s.resize(800, 700); s.show(); s.set_messages(static); s.begin_live(); spin()
    for text, kind in ((r, "reasoning"), (c, "content")):
        for i in range(0, len(text), 7):
            s.append_delta(kind, text[i:i + 7]); s._flush()
    s.end_live(True); spin()
    # сравниваем верх, середину и низ документа
    results = []
    for pos in ("top", "middle", "bottom"):
        for v in (ref, s):
            bar = v.history.verticalScrollBar()
            bar.setValue({"top": 0, "middle": bar.maximum() // 2, "bottom": bar.maximum()}[pos])
        spin()
        ia, ib = ref.history.viewport().grab().toImage(), s.history.viewport().grab().toImage()
        results.append(-1 if ia.size() != ib.size() else sum(1 for y in range(ia.height()) for x in range(ia.width()) if ia.pixel(x, y) != ib.pixel(x, y)))
    same = (ref.history.document().size().height() == s.history.document().size().height()
            and ref.history.toPlainText().rstrip() == s.history.toPlainText().rstrip() and results == [0, 0, 0])
    ok_all &= same
    print(("OK   " if same else "FAIL ") + f"{name}: высота {s.history.document().size().height():.0f} vs {ref.history.document().size().height():.0f}; отличий (верх/середина/низ): {results}")
print("ALL OK" if ok_all else "SOME FAILED")
