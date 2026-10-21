"""Switching to a long chat: the feed is at the bottom even while Qt finishes the layout; the user's scrolling is not taken away."""
import json, time
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent, QKeyEvent
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from ui.chat_view import ChatView

app = QApplication([])
ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
def row(role, content, reasoning="", tool_calls="", tool_name=""):
    return {"role": role, "content": content, "reasoning": reasoning, "tool_calls": tool_calls, "tool_call_id": "", "tool_name": tool_name}
CALLS = json.dumps([{"id": "c", "type": "function", "function": {"name": "call_tool", "arguments": '{"tool": "tb.calc", "arguments": {"expression": "1+1"}}'}}])
def chat(n):
    out = []
    for i in range(n):
        out += [row("user", f"question {i}"),
                row("assistant", f"## Answer {i}\n\n" + ("A long paragraph of text for height. " * 12 + "\n\n") * 3 + "- item\n- item\n\n```python\nprint(1)\n```", "thought\n" * 6),
                row("assistant", "", "", CALLS), row("tool", "result " * 40, "", "", "tb.calc")]
    return out
def settle(ms):
    t = time.time()
    while time.time() - t < ms / 1000: app.processEvents(); time.sleep(0.005)
def at_bottom(bar): return bar.value() >= bar.maximum() - 4
def new_view():
    v = ChatView(); v.resize(900, 600); v.show(); settle(100); return v
def wheel(v, dy):
    app.sendEvent(v.history.viewport(), QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))

for n in (5, 40, 150, 400, 1000):
    v = new_view(); bar = v.history.verticalScrollBar(); v.set_messages(chat(n))
    states = [at_bottom(bar)]
    for ms in (30, 100, 300, 800):
        settle(ms); states.append(at_bottom(bar))
    check(f"{n:>4} pairs: the feed is at the bottom at once and at every layout step", all(states), (bar.value(), bar.maximum(), states))
v = new_view(); bar = v.history.verticalScrollBar(); v.set_messages(chat(400)); settle(1500)
check("after the layout is finished the range does not change and the feed is at the bottom", at_bottom(bar) and bar.maximum() > 200000, (bar.value(), bar.maximum()))
check("sticking is released after a pause", v._stick_bottom is False)

# ---- the user's scrolling interrupts sticking ----
v = new_view(); bar = v.history.verticalScrollBar(); v.set_messages(chat(400)); settle(30)
calls = []; orig_set = bar.setValue; bar.setValue = lambda x: (calls.append(x), orig_set(x))
wheel(v, 360); settle(1000)
check("the wheel right after opening: sticking released and the chat no longer scrolls by itself", v._stick_bottom is False and calls == [], calls[:3])
bar.setValue = orig_set
v = new_view(); bar = v.history.verticalScrollBar(); v.set_messages(chat(150)); settle(800)   # the layout is already finished
wheel(v, 1200); settle(200); pos = bar.value()
check("after the layout the wheel takes the view up and the position holds", not at_bottom(bar) and bar.maximum() - pos > 100, (pos, bar.maximum()))
settle(600); check("... without jumping back down", bar.value() == pos)
v = new_view(); bar = v.history.verticalScrollBar(); v.set_messages(chat(400)); settle(30)
app.sendEvent(v.history, QKeyEvent(QEvent.KeyPress, Qt.Key_Up, Qt.NoModifier));
check("a key releases sticking too", v._stick_bottom is False)

# ---- switching there and back ----
v = new_view(); bar = v.history.verticalScrollBar(); a, b = chat(300), chat(8)
for payload in (a, b, a, b, a):
    v.set_messages(payload); settle(60)
settle(800)
check("fast switching between chats: the feed ends up at the bottom", at_bottom(bar), (bar.value(), bar.maximum()))

# ---- streaming still works ----
v = new_view(); bar = v.history.verticalScrollBar(); v.set_messages(chat(40)); settle(500); v.begin_live(); settle(50)
for i in range(60): v.append_delta("content", "a line of streamed text\n"); v._flush()
check("stream: the window follows the new text", at_bottom(bar))
bar.setValue(bar.maximum() // 2); settle(100); anchor = bar.value()
for i in range(60): v.append_delta("content", "another line\n"); v._flush()
check("stream: scrolling up is not taken away", bar.value() == anchor, (bar.value(), anchor))
print("ALL OK" if ok_all else "SOME FAILED")
