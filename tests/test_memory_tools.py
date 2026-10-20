"""Инструменты memory_* через настоящий MCP-сервер toolbox, правила в профиле, блок промпта и зависимости инструментов."""
import os, pathlib, sqlite3, tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from core import db
from core.mcp_manager import McpManager, default_config
from core.memory import DEFAULT_RULES, Memory, prompt_block
from core.tools import Catalog, ToolRouter, default_policy, with_requirements

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
db.connect().close()

# --- зависимости ---
req = {"a": ("b",), "b": ("c",), "c": ("a",), "x": ()}
check("зависимости транзитивны и терпят цикл", with_requirements({"a"}, req) == {"a", "b", "c"})
check("без зависимостей набор тот же", with_requirements({"x", "z"}, req) == {"x", "z"})
check("пустой набор", with_requirements(set(), req) == set())

# --- профиль: правила памяти ---
db.save_profile("П", "c", "m", "SYS", 0.7, 100, "", 5)
check("правила по умолчанию пусты", db.get_profile("П")["memory_rules"] == "")
db.save_profile("П", "c", "m", "SYS", 0.7, 100, "", 5, memory_rules="Мои правила")
check("правила сохраняются", db.get_profile("П")["memory_rules"] == "Мои правила")

# --- блок промпта ---
mem = Memory(db.connect())
check("без инструментов памяти блока нет", prompt_block("", ["toolbox.calc"], mem) == "")
b = prompt_block("", ["toolbox.memory_search"], mem)
check("правила по умолчанию и пустая память", DEFAULT_RULES[:40] in b and "пока пусто" in b)
mem.save("поездки", "Маккум", "Ездили в Маккум")
b = prompt_block("Свои правила", ["toolbox.memory_save"], mem)
check("свои правила и темы", b.startswith("Свои правила") and "Темы в памяти: поездки (1)" in b, b)

# --- настоящий сервер ---
manager = McpManager(log_dir=tmp / "logs")
manager.connect_all(default_config(db.DB_PATH))
tools = list(manager.states["toolbox"].tools)
by = {t.name: t for t in tools}
check("инструменты памяти зарегистрированы", all(n in by for n in ("memory_save", "memory_search", "memory_topics", "memory_read", "memory_delete")))
check("запись и удаление спрашивают, чтение нет",
      default_policy(by["memory_save"]) == "ask" and default_policy(by["memory_delete"]) == "ask"
      and default_policy(by["memory_search"]) == "auto" and default_policy(by["memory_read"]) == "auto")

router = ToolRouter(manager, Catalog(tools), confirm=lambda tool_id, args: True)
def call(name, **args):
    return router.handle("call_tool", {"tool": f"toolbox.{name}", "arguments": args})

r = call("memory_save", topic="проекты", title="Решение про память", text="Память общая для всех профилей, поиск через FTS5.")
check("save", r.startswith("Сохранено №") and "проекты" in r, r)
r = call("memory_search", query="помнишь, как решили про память?")
check("search находит", "Решение про память" in r and "FTS5" in r, r)
check("search без совпадений подсказывает", "ничего нет" in call("memory_search", query="вертолёт"))
check("topics", "поездки" in call("memory_topics") and "проекты" in call("memory_topics"))
r = call("memory_topics", topic="Проекты")
check("topics по теме", "Решение про память" in r, r)
mid = int(call("memory_search", query="Маккум").split("№")[1].split(" ")[0])
check("read", "Ездили в Маккум" in call("memory_read", id=mid))
r = call("memory_save", topic="проекты", title="решение про память", text="Обновлено.")
check("повторный save обновляет", r.startswith("Обновлена запись"), r)
check("пустой текст: ошибка", "ОШИБКА" in call("memory_save", topic="т", title="з", text=" "))
check("delete", call("memory_delete", id=mid).startswith("Удалена запись"))
check("после delete не находится", "ничего нет" in call("memory_search", query="Маккум"))
check("read несуществующей", "ОШИБКА" in call("memory_read", id=9999))

declined = ToolRouter(manager, Catalog(tools), confirm=lambda tool_id, args: False)
r = declined.handle("call_tool", {"tool": "toolbox.memory_save", "arguments": {"topic": "т", "title": "з", "text": "т"}})
check("отказ пользователя: ничего не сохранено", "отклонил" in r and Memory(sqlite3.connect(db.DB_PATH)).search("т з") == [], r)

manager.shutdown()
print("\nALL OK" if ok_all else "\nЕСТЬ ОШИБКИ")
