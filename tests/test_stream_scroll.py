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
if os.environ.get("OLD_BEHAVIOUR"):  # имитация прежней логики: полная перерисовка на каждый кусок текста
    ChatView._render_live = lambda self: self._render()
app = QApplication([])
def spin(n=3):
    for _ in range(n): app.processEvents()

def row(role, content, reasoning=""):
    return {"role": role, "content": content, "reasoning": reasoning, "tool_calls": "", "tool_call_id": "", "tool_name": ""}
HISTORY = [row("user", f"вопрос номер {i}") if i % 2 == 0 else
           row("assistant", f"**ответ** {i}\n\n- пункт один\n- пункт два\n\nещё текст для высоты {i}", f"мысль {i}\nвторая строка мысли") for i in range(60)]
STREAM_R = "Думаю над задачей.\n\n1. сначала разберу условие\n2. затем проверю варианты\n3. после этого отвечу\n" * 12
STREAM_C = "## Итог\n\nВот **подробный** ответ с `кодом`:\n\n```python\nfor i in range(3):\n    print(i)\n```\n\n- первый\n- второй\n" * 6

def new_view(history=HISTORY):
    v = ChatView(); v.resize(900, 600); v.show(); v.set_messages(history); spin(); return v
def chunks(text, n=9): return [text[i:i + n] for i in range(0, len(text), n)]
def reference_text(v):
    """Как выглядел бы документ при полной перерисовке тех же блоков."""
    r = ChatView(); r.resize(900, 600); r.show()
    r.show_reasoning.setChecked(v.show_reasoning.isChecked())
    r._blocks = [dict(b) for b in v._blocks]; r._render(); spin()
    return r.history.toPlainText().rstrip()

# ---------- A. эквивалентность полной перерисовке ----------
v = new_view(); v.begin_live(); spin()
check("живой блок создан, позиция известна", v._live_pos is not None and v._blocks[-1]["live"])
fed = 0; mismatches = []
for kind, text in [("reasoning", STREAM_R), ("content", STREAM_C)]:
    for i, ch in enumerate(chunks(text)):
        v.append_delta(kind, ch); v._flush(); fed += 1
        if fed % 40 == 0 and v.history.toPlainText().rstrip() != reference_text(v): mismatches.append(fed)
check("во время стрима текст всегда совпадает с полной перерисовкой", not mismatches, mismatches[:3])
v.end_live(keep=True); spin()
check("после завершения совпадает с полной перерисовкой", v.history.toPlainText().rstrip() == reference_text(v))
check("после завершения блок не живой, позиция сброшена", not v._blocks[-1]["live"] and v._live_pos is None)

# ---------- B. полных перерисовок во время стрима нет ----------
v = new_view(); v.begin_live(); spin()
calls = []; real_set = v.history.setHtml; v.history.setHtml = lambda h: (calls.append(1), real_set(h))[1]
for ch in chunks(STREAM_R + STREAM_C): v.append_delta("reasoning", ch); v._flush()
check(f"setHtml не вызывается во время стрима (кусков: {len(chunks(STREAM_R + STREAM_C))})", calls == [], len(calls))
v.end_live(True)
check("и при завершении блока", calls == [], len(calls))
v.history.setHtml = real_set

# ---------- C. прокрутка вверх не возвращается ----------
v = new_view(); bar = v.history.verticalScrollBar(); v.begin_live(); spin()
check("есть что прокручивать", bar.maximum() > 1000, bar.maximum())
bar.setValue(bar.maximum() // 2); spin(); anchor = bar.value(); moved = []
for ch in chunks(STREAM_R + STREAM_C):
    v.append_delta("content", ch); v._flush()
    if bar.value() != anchor: moved.append((bar.value(), anchor))
check("прокрутка посреди истории не сдвигается ни на пиксель за весь стрим", not moved, moved[:3])
check("при этом документ вырос", bar.maximum() > 1000)

# ---------- D. у нижнего края — следит за текстом ----------
v = new_view(); bar = v.history.verticalScrollBar(); v.begin_live(); spin(); bar.setValue(bar.maximum()); lag = []
for ch in chunks(STREAM_R):
    v.append_delta("reasoning", ch); v._flush()
    if bar.value() < bar.maximum() - 4: lag.append((bar.value(), bar.maximum()))
check("у нижнего края просмотр идёт за текстом", not lag, lag[:3])

# ---------- E. колесо мыши посреди генерации ----------
def wheel(view, dy):
    vp = view.history.viewport()
    app.sendEvent(vp, QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
v = new_view(); bar = v.history.verticalScrollBar(); v.begin_live(); spin(); bar.setValue(bar.maximum())
for ch in chunks(STREAM_R)[:30]: v.append_delta("reasoning", ch); v._flush()
before = bar.value(); wheel(v, 360); spin(); after_wheel = bar.value()
check("колесо вверх работает во время стрима", after_wheel < before - 20, (before, after_wheel))
held = []
for ch in chunks(STREAM_R)[30:150]:
    v.append_delta("reasoning", ch); v._flush()
    if bar.value() != after_wheel: held.append((bar.value(), after_wheel))
check("после прокрутки колесом позиция удерживается, нет «возврата» вниз", not held, held[:3])
wheel(v, 360); spin(); second = bar.value()
check("повторные прокрутки колесом продолжают работать", second < after_wheel, (after_wheel, second))
for _ in range(60): wheel(v, -360)
spin(); check("прокрутка вниз до конца снова включает слежение", bar.value() >= bar.maximum() - 4, (bar.value(), bar.maximum()))
for ch in chunks(STREAM_R)[150:200]: v.append_delta("reasoning", ch); v._flush()
check("и дальше просмотр идёт за текстом", bar.value() >= bar.maximum() - 4)

# ---------- F. пустой ответ убирается без следа ----------
v = new_view(); before_txt = v.history.toPlainText().rstrip(); nb = len(v._blocks)
v.begin_live(); spin(); check("заглушка «…» пока нет текста", v.history.toPlainText().rstrip().endswith("…"))
v.append_delta("reasoning", "чуть"); v._flush()
v.end_live(keep=False); spin()
check("end_live(keep=False) возвращает документ к исходному", v.history.toPlainText().rstrip() == before_txt and len(v._blocks) == nb and v._live_pos is None)
v.begin_live(); v.append_delta("content", "x"); v._flush(); spin()
check("после удаления можно начать новый живой блок", v.history.toPlainText().rstrip() == reference_text(v))

# ---------- G. переключатель размышлений посреди стрима ----------
v = new_view(); v.begin_live(); spin()
for ch in chunks(STREAM_R)[:20]: v.append_delta("reasoning", ch); v._flush()
v.show_reasoning.setChecked(False); spin()
check("выключение размышлений посреди стрима: текст верный", v.history.toPlainText().rstrip() == reference_text(v) and "Думаю над задачей" not in v.history.toPlainText().split("Модель")[-1])
for ch in chunks(STREAM_C)[:20]: v.append_delta("content", ch); v._flush()
check("стрим продолжается после переключения", "Итог" in v.history.toPlainText() and v.history.toPlainText().rstrip() == reference_text(v))
v.show_reasoning.setChecked(True); spin()
check("включение обратно: размышления вернулись", "Думаю над задачей" in v.history.toPlainText() and v.history.toPlainText().rstrip() == reference_text(v))
v.append_delta("content", "ещё"); v._flush(); check("после включения живой блок обновляется", v.history.toPlainText().rstrip().endswith("ещё") or "ещё" in v.history.toPlainText()[-40:])

# ---------- H. работа через таймер (как в приложении) ----------
v = new_view(); v.begin_live(); spin()
for ch in chunks(STREAM_C): v.append_delta("content", ch)
t0 = time.time()
while v._dirty and time.time() - t0 < 3: app.processEvents(); time.sleep(0.005)
spin(); v.end_live(True)
check("таймер сам досчитывает отрисовку", v.history.toPlainText().rstrip() == reference_text(v))

# ---------- I. раунд инструментов: перезагрузка и новый живой блок ----------
v = new_view(); v.begin_live(); v.append_delta("content", "промежуточный"); v._flush()
v.set_messages(HISTORY + [row("user", "q"), row("assistant", "", "мысль")]); v.begin_live(); spin()
check("set_messages во время стрима + новый живой блок", v._live_pos is not None and v.history.toPlainText().rstrip() == reference_text(v))
v.append_delta("content", "финал"); v._flush(); v.end_live(True)
check("раунд завершён корректно", v.history.toPlainText().rstrip().endswith("финал"))

# ---------- J. скорость ----------
v = new_view(HISTORY * 3); v.begin_live(); spin(); stream = chunks(STREAM_R + STREAM_C)
t0 = time.perf_counter()
for ch in stream: v.append_delta("content", ch); v._flush()
per = (time.perf_counter() - t0) * 1000 / len(stream)
print(f"      документ {len(HISTORY) * 3} блоков, живой блок до {len(STREAM_R + STREAM_C)} симв.: {per:.2f} мс на обновление")
check("обновление живого блока быстрее 15 мс", per < 15, per)
print("ALL OK" if ok_all else "SOME FAILED")
