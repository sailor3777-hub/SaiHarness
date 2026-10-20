import json, os, sys, time, pathlib, tempfile, threading
import httpx
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget
from core import db, llm
tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t23.db"
from ui.main_window import MainWindow
from ui.permission import PermissionBroker
from ui.tools_panel import ID_ROLE, KIND_ROLE
from core.tools import META_TOOLS

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

app = QApplication([])
def wait(cond, timeout=30.0):
    t = time.time()
    while not cond() and time.time() - t < timeout: app.processEvents(); time.sleep(0.005)
    app.processEvents(); return cond()

proj = tmp / "proj"; proj.mkdir(); (proj / "secret.txt").write_text("Код доступа: ФИОЛЕТОВЫЙ-МАМОНТ-42\n", encoding="utf-8")
db.add_root(str(proj))

# ---------- скриптованная «модель» ----------
def sse(*chunks): return ("".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
def tcall(i, name, args): return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": i, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}
def say(text, reasoning=""):
    ch = ([{"choices": [{"delta": {"reasoning_content": reasoning}}]}] if reasoning else []) + [{"choices": [{"delta": {"content": text}}]}]
    return ch
class LM:
    def __init__(self): self.steps = []; self.requests = []
    def script(self, *steps): self.steps = list(steps); self.requests = []
    def handler(self, req):
        self.requests.append(json.loads(req.content))
        return httpx.Response(200, content=sse(*self.steps.pop(0)))
lm = LM(); real_stream = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))

w = MainWindow(mcp_config=tmp / "mcp.json"); tp = w.tools_panel; v = w.chat_view; sel = w.selector; ctx = w.context
check("серверы подключаются в фоне", wait(lambda: tp.states and not tp.connect_req.busy, 40) and tp.states["toolbox"].status == "ready", tp.states)
top = tp.tree.topLevelItem(0)
check("дерево: сервер и 22 инструмента, всё выключено", top.childCount() == 22 and top.checkState(0) == Qt.Unchecked
      and all(top.child(i).checkState(0) == Qt.Unchecked for i in range(22)) and "toolbox" in top.text(0))
names = {top.child(i).text(0) for i in range(22)}
check("метка прав у записывающего инструмента", any(n.startswith("notes_write") and "[спросит]" in n for n in names) and "calc" in names, names)
check("инфо: инструменты не включены", "не включены" in tp.info.text() and tp.prompt_tokens() == 0, tp.info.text())

# профиль и подключение (сервер моделей подменён, адрес фиктивный)
c = ctx.connections; c.name.setText("lm"); c.url.setText("127.0.0.1:1"); c.save_btn.click()
b = ctx.builder; wait(lambda: not b.models_req.busy)
b.name.setText("p"); b.base_model.setCurrentText("fake-model"); b.max_tokens.setValue(500); b.save_btn.click()

# включаем весь сервер ДО создания чата (черновик)
top.setCheckState(0, Qt.Checked)
check("галочка сервера включает все инструменты", len(tp.enabled) == 22 and tp.enabled == {t.id for t in tp.tools} and w.chat_id is None)
check("инфо: токены каталога и экономия", "Включено: 22" in tp.info.text() and "Полные схемы заняли бы" in tp.info.text() and tp.prompt_tokens() > 0, tp.info.text())
extra_tokens = w.files_panel._extra_tokens
check("«запрос целиком» учитывает каталог", extra_tokens >= tp.prompt_tokens() > 0)

# ---------- сценарий 1: поиск -> вызов -> ответ ----------
lm.script(
    say("", "Нужно посчитать") + [tcall("t1", "find_tool", {"query": "вычислить"})],
    [tcall("t2", "call_tool", {"tool": "toolbox.calc", "arguments": {"expression": "17*3"}})],
    say("Ответ: **51**"))
v.input.setPlainText("Сколько будет 17*3?"); v.send_btn.click()
check("чат создан, инструменты перенесены из черновика", w.chat_id is not None and db.load_chat_tools(w.chat_id) == tp.enabled)
check("модель закончила", wait(lambda: w.stream is None, 60))
rows = db.list_messages(w.chat_id)
check("в БД: user, (вызов, результат)×2, ответ", [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant", "tool", "assistant"], [r["role"] for r in rows])
check("первый вызов сохранён вместе с размышлениями", json.loads(rows[1]["tool_calls"])[0]["function"]["name"] == "find_tool" and rows[1]["reasoning"] == "Нужно посчитать")
check("результат поиска найден по-русски", "toolbox.calc(expression: str)" in rows[2]["content"] and rows[2]["tool_name"] == "find_tool", rows[2]["content"])
check("результат calc от настоящего MCP-сервера", rows[4]["content"] == "51" and rows[4]["tool_name"] == "toolbox.calc", rows[4]["content"])
check("финальный ответ", rows[5]["content"] == "Ответ: **51**")
req1, req3 = lm.requests[0], lm.requests[2]
check("в запросе только 3 мета-инструмента", req1["tools"] == META_TOOLS and len(req1["tools"]) == 3)
sysmsg = req1["messages"][0]["content"]
check("каталог в system-сообщении", "toolbox.calc(expression: str)" in sysmsg and "toolbox.fs_read(path: str, max_chars?: int, offset?: int)" in sysmsg and "toolbox.notes_write(name: str, content: str)" in sysmsg, sysmsg[:300])
check("3-й запрос: вся цепочка вызовов и результатов", [m["role"] for m in req3["messages"]] == ["system", "user", "assistant", "tool", "assistant", "tool"]
      and req3["messages"][5]["content"] == "51")
hist = v.history.toPlainText()
check("в чате видны вызовы и результаты", "поиск инструмента: «вычислить»" in hist and "toolbox.calc({\"expression\": \"17*3\"})" in hist and "51" in hist, hist)
check("размышления раунда видны", "Нужно посчитать" in hist)
check("статус после завершения", "вызовов инструментов" not in v.status.text() and w.stream is None and v.send_btn.isEnabled(), v.status.text())
check("«Запрос» хранит список функций", w.last_requests[w.chat_id]["tools"] == ["find_tool", "describe_tool", "call_tool"])

# ---------- чтение файла из разрешённой папки; чужой файл недоступен ----------
outside = tmp / "outside.txt"; outside.write_text("секрет вне корней", encoding="utf-8")
lm.script([tcall("f1", "call_tool", {"tool": "toolbox.fs_read", "arguments": {"path": str(proj / "secret.txt")}})],
          [tcall("f2", "call_tool", {"tool": "toolbox.fs_read", "arguments": {"path": str(outside)}})],
          say("готово"))
v.input.setPlainText("прочитай файлы"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
tools_rows = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-2:]
check("fs_read читает файл из корня", "ФИОЛЕТОВЫЙ-МАМОНТ-42" in tools_rows[0]["content"], tools_rows[0]["content"])
check("fs_read вне корней запрещён сервером", "вне разрешённых" in tools_rows[1]["content"] and "секрет вне корней" not in tools_rows[1]["content"], tools_rows[1]["content"])

# ---------- неверные аргументы: ошибка со схемой, модель может исправиться ----------
lm.script([tcall("e1", "call_tool", {"tool": "toolbox.calc", "arguments": {}})],
          [tcall("e2", "call_tool", {"tool": "toolbox.calc", "arguments": {"expression": "2+2"}})], say("4"))
v.input.setPlainText("посчитай"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
tr = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-2:]
check("неверные аргументы: подсказка со схемой, затем успех", tr[0]["content"].startswith("ОШИБКА: неверные аргументы") and '"required"' in tr[0]["content"] and tr[1]["content"] == "4", tr)
check("ошибка в чате выделена", "ОШИБКА: неверные аргументы" in v.history.toPlainText())

# ---------- права: спросить -> отказ / разрешить; auto; deny ----------
notes = tmp / "notes" / "idea.md"
asked = []
def answer(result):
    def q(parent, title, text, *a, **k): asked.append(text); return result
    return staticmethod(q)
write = lambda: [tcall("w1", "call_tool", {"tool": "toolbox.notes_write", "arguments": {"name": "idea", "content": "заметка"}})]
QMessageBox.question = answer(QMessageBox.No)
lm.script(write(), say("ок")); v.input.setPlainText("запиши"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("запись: спросили, пользователь отказал, файла нет", len(asked) == 1 and "toolbox.notes_write" in asked[0] and "Текст: заметка" in asked[0] and "Заметка: idea" in asked[0]
      and "отклонил" in last["content"] and not notes.exists(), (asked, last["content"]))
QMessageBox.question = answer(QMessageBox.Yes); asked.clear()
lm.script(write(), say("ок")); v.input.setPlainText("запиши ещё раз"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("запись: разрешено, файл создан", len(asked) == 1 and notes.exists() and notes.read_text(encoding="utf-8") == "заметка" and '"saved"' in last["content"], last["content"])
tp.set_policy("toolbox.notes_write", "auto"); asked.clear(); notes.unlink()
lm.script(write(), say("ок")); v.input.setPlainText("и ещё"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
check("auto: без вопроса", asked == [] and notes.exists())
tp.set_policy("toolbox.notes_write", "deny"); notes.unlink()
lm.script(write(), say("ок")); v.input.setPlainText("и ещё раз"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("deny: без вопроса, запрещено, файла нет", asked == [] and "запрещён" in last["content"] and not notes.exists(), last["content"])
check("политика сохраняется в БД и видна в дереве", db.load_tool_policies() == {"toolbox.notes_write": "deny"}
      and any("[запрещён]" in tp.tree.topLevelItem(0).child(i).text(0) for i in range(22)))
tp.set_policy("toolbox.notes_write", None); check("сброс политики", db.load_tool_policies() == {} and tp.policy_for(next(t for t in tp.tools if t.name == "notes_write")) == "ask")

# ---------- модель зовёт инструмент, которого нет в чате ----------
tp.enabled.discard("toolbox.time_now"); tp._sync(); w._on_tools_changed()
lm.script([tcall("n1", "call_tool", {"tool": "toolbox.time_now", "arguments": {}})], say("нет"))
v.input.setPlainText("время?"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
last = [r for r in db.list_messages(w.chat_id) if r["role"] == "tool"][-1]
check("выключенный инструмент недоступен модели", "нет в каталоге" in last["content"], last["content"])
check("каталог в запросе без выключенного", "time_now" not in lm.requests[0]["messages"][0]["content"])

# ---------- второй чат: инструменты не включены, tools в запрос не идут ----------
w.new_chat(); chat2 = w.chat_id
check("новый чат: инструменты выключены", tp.enabled == set() and "не включены" in tp.info.text())
lm.script(say("привет!")); v.input.setPlainText("привет"); v.send_btn.click(); wait(lambda: w.stream is None, 60)
check("без инструментов: нет tools и каталога в запросе", "tools" not in lm.requests[0] and "toolbox" not in json.dumps(lm.requests[0], ensure_ascii=False)
      and w.last_requests[chat2]["tools"] == [])
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) != chat2: w.chat_list.list.setCurrentRow(i)
check("возврат в первый чат: набор инструментов восстановлен", len(tp.enabled) == 21 and "toolbox.time_now" not in tp.enabled
      and tp.tree.topLevelItem(0).checkState(0) == Qt.PartiallyChecked)
check("история чата с вызовами отображается после перезагрузки из БД", "поиск инструмента" in v.history.toPlainText() and "◂ toolbox.calc" in v.history.toPlainText())

# ---------- плохой конфиг и перезапуск ----------
cfg = json.loads((tmp / "mcp.json").read_text(encoding="utf-8"))
cfg["mcpServers"]["broken"] = {"command": sys.executable, "args": ["-c", "import sys; print('boom', file=sys.stderr); sys.exit(2)"]}
cfg["mcpServers"]["off"] = {"command": "x", "disabled": True}
(tmp / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
tp.reconnect_btn.click(); check("кнопка блокируется на время подключения", not tp.reconnect_btn.isEnabled())
check("перезапуск завершился", wait(lambda: not tp.connect_req.busy and "broken" in tp.states, 60))
texts = [tp.tree.topLevelItem(i).text(0) for i in range(tp.tree.topLevelItemCount())]
check("в дереве: рабочий, сломанный, отключённый", any(t.startswith("●") for t in texts) and any(t.startswith("✗ broken") for t in texts) and any(t.startswith("○ off") for t in texts), texts)
broken = next(tp.tree.topLevelItem(i) for i in range(tp.tree.topLevelItemCount()) if tp.tree.topLevelItem(i).text(0).startswith("✗"))
check("у сломанного виден текст ошибки", broken.childCount() == 1 and broken.child(0).text(0), broken.child(0).text(0))
check("включённые инструменты пережили перезапуск", len(tp.enabled) == 21 and len(tp.enabled_tools()) == 21)
(tmp / "mcp.json").write_text("{ это не json", encoding="utf-8"); tp.reconnect_btn.click(); wait(lambda: not tp.connect_req.busy)
check("битый конфиг: понятная ошибка, приложение живо", "Не удалось подключить серверы" in tp.info.text(), tp.info.text())

# ---------- ошибка и остановка оставляют строку «Система» в чате ----------
tp.enabled = set(); tp._sync()            # без инструментов, чтобы проверять только это
w.new_chat(); sys_chat = w.chat_id
healthy = llm.httpx.stream
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))).stream(m, u, json=k.get("json"), headers=k.get("headers"))
v.input.setPlainText("первый вопрос"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
rows = db.list_messages(sys_chat)
check("ошибка сервера: строка «Система» в БД", [r["role"] for r in rows] == ["user", "system"] and rows[1]["content"] == "Ошибка сервера 500: boom", [(r["role"], r["content"]) for r in rows])
check("ошибка видна в чате", "Система" in v.history.toPlainText() and "Ошибка сервера 500: boom" in v.history.toPlainText())
check("статус тоже показывает ошибку", "boom" in v.status.text())
def slow(req):
    time.sleep(1.5); return httpx.Response(200, content=sse(say("поздно")))
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(slow)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
v.input.setPlainText("второй вопрос"); v.send_btn.click(); wait(lambda: w.stream is not None and False, 0.3); w.stop_stream()
rows = db.list_messages(sys_chat)
check("остановка до первого токена: строка «Остановлено», ответа нет", [r["role"] for r in rows][-2:] == ["user", "system"] and rows[-1]["content"] == "Остановлено", [(r["role"], r["content"]) for r in rows])
check("чат показывает «Остановлено»", v.history.toPlainText().count("Остановлено") == 1)
llm.httpx.stream = lambda m, u, **k: httpx.Client(transport=httpx.MockTransport(lm.handler)).stream(m, u, json=k.get("json"), headers=k.get("headers"))
time.sleep(1.7); app.processEvents()
lm.script(say("ответ после ошибок")); v.input.setPlainText("третий вопрос"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
sent = lm.requests[0]["messages"]
check("строки «Система» не уходят модели", [m["role"] for m in sent] == ["user", "user", "user"], [m["role"] for m in sent])
check("после ошибок диалог продолжается", db.list_messages(sys_chat)[-1]["content"] == "ответ после ошибок")
reopened = MainWindow.__new__  # (формальность: проверяем перезагрузку из БД ниже)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) != sys_chat: w.chat_list.list.setCurrentRow(i)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == sys_chat: w.chat_list.list.setCurrentRow(i)
lm.script([{"choices": [{"delta": {}}]}]); v.input.setPlainText("а теперь пустой ответ"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
rows = db.list_messages(sys_chat)
check("пустой ответ модели: строка «Система», а не тишина", rows[-1]["role"] == "system" and rows[-1]["content"] == "Модель вернула пустой ответ", [(r["role"], r["content"]) for r in rows[-2:]])
check("пустой ответ виден в чате", "Модель вернула пустой ответ" in v.history.toPlainText())
lm.script([{"choices": [{"delta": {}, "finish_reason": "length"}]}]); v.input.setPlainText("и ещё раз"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
check("пустой ответ из-за лимита токенов: причина в строке", db.list_messages(sys_chat)[-1]["content"] == "Модель вернула пустой ответ (Лимит max tokens исчерпан, ответ оборван)", db.list_messages(sys_chat)[-1]["content"])
lm.script(say("нормальный ответ")); v.input.setPlainText("и нормальный"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
check("после пустых ответов диалог продолжается, лишних строк нет", [r["role"] for r in db.list_messages(sys_chat)][-2:] == ["user", "assistant"])
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) != sys_chat: w.chat_list.list.setCurrentRow(i)
for i in range(w.chat_list.list.count()):
    if w.chat_list.list.item(i).data(0x100) == sys_chat: w.chat_list.list.setCurrentRow(i)
check("строки «Система» восстанавливаются из БД", v.history.toPlainText().count("Остановлено") == 1 and "boom" in v.history.toPlainText())

# ответ пришёл как размышления при пустом content (Qwen 3.5 в LM Studio): сохраняется как ответ
check("answer_in_reasoning: чистые случаи", llm.answer_in_reasoning("", "текст") and not llm.answer_in_reasoning("ответ", "думы")
      and not llm.answer_in_reasoning("", "  ") and not llm.answer_in_reasoning("", "думы", llm.LENGTH_NOTICE) and not llm.answer_in_reasoning(" \n", ""))
lm.script([{"choices": [{"delta": {"reasoning_content": "Код доставки: KAPPA-731"}}]}]); v.input.setPlainText("ответ в размышлениях"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
last = db.list_messages(sys_chat)[-1]
check("ответ в размышлениях сохраняется как ответ", last["role"] == "assistant" and last["content"] == "Код доставки: KAPPA-731" and last["reasoning"] == "", (last["role"], last["content"], last["reasoning"]))
check("и виден в чате без «Размышлений»", "KAPPA-731" in v.history.toPlainText() and "Модель вернула пустой ответ" not in db.list_messages(sys_chat)[-1]["content"])
lm.script(say("следующий ответ")); v.input.setPlainText("а что ты ответил?"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
check("модель видит свой прошлый ответ в истории", any(m["role"] == "assistant" and m.get("content") == "Код доставки: KAPPA-731" for m in lm.requests[-1]["messages"]), lm.requests[-1]["messages"][-4:])
lm.script([{"choices": [{"delta": {"reasoning_content": "Начал думать и упёрся в лимит"}, "finish_reason": "length"}]}]); v.input.setPlainText("оборванные размышления"); v.send_btn.click(); wait(lambda: w.stream is None, 30)
last = db.list_messages(sys_chat)[-1]
check("размышления, оборванные лимитом, ответом не считаются", last["role"] == "assistant" and last["content"] == "" and "упёрся" in last["reasoning"], (last["role"], last["content"], last["reasoning"]))

# ---------- брокер прав: отмена во время ожидания ----------
br = PermissionBroker(w); br._on_asked = lambda *a: None
br.asked.disconnect(); ev = threading.Event(); res = []
th = threading.Thread(target=lambda: res.append(br.ask("t", {}, ev))); th.start(); time.sleep(0.5); ev.set(); th.join(3)
check("брокер: отмена генерации = отказ без зависания", res == [False] and not th.is_alive())
br2 = PermissionBroker(w, timeout=0.6); br2.asked.disconnect(); t0 = time.time()
check("брокер: тайм-аут = отказ", br2.ask("t", {}) is False and time.time() - t0 < 3)

llm.httpx.stream = real_stream
w.close()
print("ALL OK" if ok_all else "SOME FAILED")



