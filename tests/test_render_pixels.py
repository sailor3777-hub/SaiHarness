"""The layout after streaming must be pixel for pixel the same as after a full redraw."""
import json
from PySide6.QtWidgets import QApplication
from ui.chat_view import ChatView

app = QApplication([])
def spin(n=5):
    for _ in range(n): app.processEvents()
def row(role, content, reasoning="", tool_calls="", tool_name=""):
    return {"role": role, "content": content, "reasoning": reasoning, "tool_calls": tool_calls, "tool_call_id": "", "tool_name": tool_name}
CALLS = json.dumps([{"id": "c", "type": "function", "function": {"name": "call_tool", "arguments": '{"tool": "tb.calc", "arguments": {"expression": "1+1"}}'}}])
HIST = [row("user", "What is in the file?"), row("assistant", "Here is the list:\n\n- one\n- two\n\nAnd the **summary**.", "Let me think.\n1. one\n2. two")]
R = "I need to read the file.\n\n1. find the path\n2. read it\n"
C = "## Result\n\nThe file says `secret`.\n\n```python\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- first\n- second"
TOOLS = [row("assistant", "", "searching", CALLS), row("tool", "2", "", "", "tb.calc")]

SCENARIOS = {
    "after ordinary messages": (HIST + [row("user", "another question")], R, C),
    "empty chat (the live block is first)": ([], R, C),
    "after a tool call and result": (HIST + [row("user", "q")] + TOOLS, R, C),
    "reasoning only": (HIST + [row("user", "q")], R, ""),
    "answer only": (HIST + [row("user", "q")], "", C),
    "a long history": ((HIST + [row("user", "q")]) * 15 + [row("user", "another question")], R, C),
}
ok_all = True
for name, (static, r, c) in SCENARIOS.items():
    ref = ChatView(); ref.resize(800, 700); ref.show(); ref.set_messages(static + [row("assistant", c, r)]); spin()
    s = ChatView(); s.resize(800, 700); s.show(); s.set_messages(static); s.begin_live(); spin()
    for text, kind in ((r, "reasoning"), (c, "content")):
        for i in range(0, len(text), 7):
            s.append_delta(kind, text[i:i + 7]); s._flush()
    s.end_live(True); spin()
    # compare the top, middle and bottom of the document
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
    print(("OK   " if same else "FAIL ") + f"{name}: height {s.history.document().size().height():.0f} vs {ref.history.document().size().height():.0f}; differences (top/middle/bottom): {results}")
print("ALL OK" if ok_all else "SOME FAILED")
