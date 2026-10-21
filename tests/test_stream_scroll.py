import time
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication
from ui.chat_view import ChatView

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

import os
if os.environ.get("OLD_BEHAVIOUR"):  # imitates the old logic: a full redraw for every chunk of text
    ChatView._render_live = lambda self: self._render()
app = QApplication([])
def spin(n=3):
    for _ in range(n): app.processEvents()

def row(role, content, reasoning=""):
    return {"role": role, "content": content, "reasoning": reasoning, "tool_calls": "", "tool_call_id": "", "tool_name": ""}
HISTORY = [row("user", f"question number {i}") if i % 2 == 0 else
           row("assistant", f"**answer** {i}\n\n- item one\n- item two\n\nmore text for height {i}", f"thought {i}\nsecond line of the thought") for i in range(60)]
STREAM_R = "Thinking about the task.\n\n1. first I will parse the problem\n2. then I will check the options\n3. after that I will answer\n" * 12
STREAM_C = "## Summary\n\nHere is a **detailed** answer with `code`:\n\n```python\nfor i in range(3):\n    print(i)\n```\n\n- first\n- second\n" * 6

def new_view(history=HISTORY):
    v = ChatView(); v.resize(900, 600); v.show(); v.set_messages(history); spin(); return v
def chunks(text, n=9): return [text[i:i + n] for i in range(0, len(text), n)]
def reference_text(v):
    """What the document would look like after a full redraw of the same blocks."""
    r = ChatView(); r.resize(900, 600); r.show()
    r.show_reasoning.setChecked(v.show_reasoning.isChecked())
    r._blocks = [dict(b) for b in v._blocks]; r._render(); spin()
    return r.history.toPlainText().rstrip()

# ---------- A. equivalence to a full redraw ----------
v = new_view(); v.begin_live(); spin()
check("the live block is created, its position is known", v._live_pos is not None and v._blocks[-1]["live"])
fed = 0; mismatches = []
for kind, text in [("reasoning", STREAM_R), ("content", STREAM_C)]:
    for i, ch in enumerate(chunks(text)):
        v.append_delta(kind, ch); v._flush(); fed += 1
        if fed % 40 == 0 and v.history.toPlainText().rstrip() != reference_text(v): mismatches.append(fed)
check("while streaming the text always matches a full redraw", not mismatches, mismatches[:3])
v.end_live(keep=True); spin()
check("after finishing it matches a full redraw", v.history.toPlainText().rstrip() == reference_text(v))
check("after finishing the block is not live, the position is reset", not v._blocks[-1]["live"] and v._live_pos is None)

# ---------- B. no full redraws while streaming ----------
v = new_view(); v.begin_live(); spin()
calls = []; real_set = v.history.setHtml; v.history.setHtml = lambda h: (calls.append(1), real_set(h))[1]
for ch in chunks(STREAM_R + STREAM_C): v.append_delta("reasoning", ch); v._flush()
check(f"setHtml is not called while streaming (chunks: {len(chunks(STREAM_R + STREAM_C))})", calls == [], len(calls))
v.end_live(True)
check("nor when the block is finished", calls == [], len(calls))
v.history.setHtml = real_set

# ---------- C. scrolling up does not jump back ----------
v = new_view(); bar = v.history.verticalScrollBar(); v.begin_live(); spin()
check("there is something to scroll", bar.maximum() > 1000, bar.maximum())
bar.setValue(bar.maximum() // 2); spin(); anchor = bar.value(); moved = []
for ch in chunks(STREAM_R + STREAM_C):
    v.append_delta("content", ch); v._flush()
    if bar.value() != anchor: moved.append((bar.value(), anchor))
check("scrolling in the middle of the history does not move by a pixel during the whole stream", not moved, moved[:3])
check("and the document did grow", bar.maximum() > 1000)

# ---------- D. at the bottom edge it follows the text ----------
v = new_view(); bar = v.history.verticalScrollBar(); v.begin_live(); spin(); bar.setValue(bar.maximum()); lag = []
for ch in chunks(STREAM_R):
    v.append_delta("reasoning", ch); v._flush()
    if bar.value() < bar.maximum() - 4: lag.append((bar.value(), bar.maximum()))
check("at the bottom edge the view follows the text", not lag, lag[:3])

# ---------- E. the mouse wheel in the middle of generation ----------
def wheel(view, dy):
    vp = view.history.viewport()
    app.sendEvent(vp, QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
v = new_view(); bar = v.history.verticalScrollBar(); v.begin_live(); spin(); bar.setValue(bar.maximum())
for ch in chunks(STREAM_R)[:30]: v.append_delta("reasoning", ch); v._flush()
before = bar.value(); wheel(v, 360); spin(); after_wheel = bar.value()
check("the wheel up works while streaming", after_wheel < before - 20, (before, after_wheel))
held = []
for ch in chunks(STREAM_R)[30:150]:
    v.append_delta("reasoning", ch); v._flush()
    if bar.value() != after_wheel: held.append((bar.value(), after_wheel))
check("after scrolling with the wheel the position holds, no \"jump\" back down", not held, held[:3])
wheel(v, 360); spin(); second = bar.value()
check("repeated wheel scrolls keep working", second < after_wheel, (after_wheel, second))
for _ in range(60): wheel(v, -360)
spin(); check("scrolling down to the end turns following on again", bar.value() >= bar.maximum() - 4, (bar.value(), bar.maximum()))
for ch in chunks(STREAM_R)[150:200]: v.append_delta("reasoning", ch); v._flush()
check("and then the view follows the text", bar.value() >= bar.maximum() - 4)

# ---------- F. an empty reply is removed without a trace ----------
v = new_view(); before_txt = v.history.toPlainText().rstrip(); nb = len(v._blocks)
v.begin_live(); spin(); check("a \"…\" placeholder while there is no text", v.history.toPlainText().rstrip().endswith("…"))
v.append_delta("reasoning", "a bit"); v._flush()
v.end_live(keep=False); spin()
check("end_live(keep=False) returns the document to the original", v.history.toPlainText().rstrip() == before_txt and len(v._blocks) == nb and v._live_pos is None)
v.begin_live(); v.append_delta("content", "x"); v._flush(); spin()
check("after removal a new live block can be started", v.history.toPlainText().rstrip() == reference_text(v))

# ---------- G. the reasoning toggle in the middle of a stream ----------
v = new_view(); v.begin_live(); spin()
for ch in chunks(STREAM_R)[:20]: v.append_delta("reasoning", ch); v._flush()
v.show_reasoning.setChecked(False); spin()
check("turning reasoning off mid-stream: the text is right", v.history.toPlainText().rstrip() == reference_text(v) and "Thinking about the task" not in v.history.toPlainText().split("Model")[-1])
for ch in chunks(STREAM_C)[:20]: v.append_delta("content", ch); v._flush()
check("the stream goes on after the toggle", "Summary" in v.history.toPlainText() and v.history.toPlainText().rstrip() == reference_text(v))
v.show_reasoning.setChecked(True); spin()
check("turning it back on: the reasoning is back", "Thinking about the task" in v.history.toPlainText() and v.history.toPlainText().rstrip() == reference_text(v))
v.append_delta("content", chr(10) * 2 + "more")  # a new paragraph: the cut-off chunk may end inside a code fence line; v._flush(); check("after turning it on the live block updates", v.history.toPlainText().rstrip().endswith("more") or "more" in v.history.toPlainText()[-40:])

# ---------- H. working through the timer (as in the app) ----------
v = new_view(); v.begin_live(); spin()
for ch in chunks(STREAM_C): v.append_delta("content", ch)
t0 = time.time()
while v._dirty and time.time() - t0 < 3: app.processEvents(); time.sleep(0.005)
spin(); v.end_live(True)
check("the timer finishes the drawing by itself", v.history.toPlainText().rstrip() == reference_text(v))

# ---------- I. a tool round: reload and a new live block ----------
v = new_view(); v.begin_live(); v.append_delta("content", "intermediate"); v._flush()
v.set_messages(HISTORY + [row("user", "q"), row("assistant", "", "thought")]); v.begin_live(); spin()
check("set_messages while streaming + a new live block", v._live_pos is not None and v.history.toPlainText().rstrip() == reference_text(v))
v.append_delta("content", "final"); v._flush(); v.end_live(True)
check("the round finished correctly", v.history.toPlainText().rstrip().endswith("final"))

# ---------- J. speed ----------
v = new_view(HISTORY * 3); v.begin_live(); spin(); stream = chunks(STREAM_R + STREAM_C)
t0 = time.perf_counter()
for ch in stream: v.append_delta("content", ch); v._flush()
per = (time.perf_counter() - t0) * 1000 / len(stream)
print(f"      document of {len(HISTORY) * 3} blocks, live block up to {len(STREAM_R + STREAM_C)} chars: {per:.2f} ms per update")
check("updating the live block takes less than 15 ms", per < 15, per)
print("ALL OK" if ok_all else "SOME FAILED")
