import sqlite3
import sys

from core.memory import Memory, MemoryInputError, stem, stems

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def raises(fn):
    try:
        fn()
        return False
    except MemoryInputError:
        return True


# --- основы слов ---
check("падежи одного слова совпадают", stem("Маккум") == stem("Маккуме") == stem("Маккума"))
check("ё = е", stem("пётр") == stem("петр"))
check("числа не режутся", stem("2024") == "2024")
check("короткие слова целиком", stem("дом") == "дом")
check("stems разбирает фразу", stems("Ездили в Маккум!") == ["езди", "в", "макк"])

mem = Memory(sqlite3.connect(":memory:"))
check("пусто: тем нет", mem.topics() == [] and mem.topics_line() == "" and mem.count() == 0)

a, upd = mem.save("поездки", "Поездка в Маккум", "Ездили в Маккум в сентябре втроём, ночевали в палатке у озера.", source_chat=7)
b, _ = mem.save("поездки", "Байкал", "Летом 2023 был на Байкале с братом, видели нерпу.")
c, _ = mem.save("проект SaiHarness", "Решение про память", "Память общая для всех профилей, поиск через FTS5 со стеммингом.")
check("три записи", mem.count() == 3 and not upd)
check("ids разные", len({a, b, c}) == 3)

# --- поиск ---
r = mem.search("помнишь, ездили в Маккуме?")
check("находит по другой словоформе", r and r[0]["id"] == a, r)
check("в результате тема и заголовок", r and r[0]["topic"] == "поездки" and r[0]["title"] == "Поездка в Маккум")
check("по слову из заголовка", mem.search("байкал")[0]["id"] == b)
check("по теме", {x["id"] for x in mem.search("поездки")} >= {a, b})
check("OR-запасной вариант: часть слов не найдена", mem.search("Маккум вертолёт")[0]["id"] == a)
check("ничего не найдено", mem.search("квантовая хромодинамика") == [])
check("пустой запрос", mem.search("  ") == [] and mem.search("...") == [])
check("фильтр по теме", [x["id"] for x in mem.search("память", topic="поездки")] == [])
check("фильтр по теме находит", [x["id"] for x in mem.search("память", topic="ПРОЕКТ saiharness")] == [c])
check("limit", len(mem.search("поездки", limit=1)) == 1)
check("спецсимволы FTS не ломают поиск", mem.search('"маккум" OR (x) * NEAR') is not None)

# --- обновление, удаление ---
a2, upd2 = mem.save("Поездки", "поездка в маккум", "Дополнено: ездили на двух машинах.")
check("тот же заголовок и тема: обновление", upd2 and a2 == a and mem.count() == 3)
check("новый текст ищется", mem.search("машинах")[0]["id"] == a)
check("старый текст из индекса ушёл", mem.search("палатке") == [])
mem.update(b, "поездки", "Байкал 2023", "Теперь про Алтай.")
check("update меняет индекс", mem.search("алтай")[0]["id"] == b and mem.search("нерпу") == [])
check("delete", mem.delete(c) and mem.get(c) is None and mem.search("стеммингом") == [])
check("delete несуществующей", not mem.delete(9999))
check("update несуществующей", raises(lambda: mem.update(9999, "т", "з", "т")))

# --- темы ---
check("topics: регистр не плодит темы", [(t.lower(), n) for t, n in mem.topics()] == [("поездки", 2)], mem.topics())
check("topics_line", mem.topics_line().lower() == "поездки (2)")
for i in range(15):
    mem.save(f"тема{i}", "з", "т")
check("topics_line обрезается", "ещё тем" in mem.topics_line(limit=5))
check("list по теме", len(mem.list("Поездки")) == 2)

# --- проверки ввода ---
check("пустая тема", raises(lambda: mem.save("", "з", "т")))
check("пустой текст", raises(lambda: mem.save("т", "з", "   ")))
check("длинный заголовок", raises(lambda: mem.save("т", "з" * 200, "т")))

print("\nALL OK" if ok_all else "\nЕСТЬ ОШИБКИ")
sys.exit(0 if ok_all else 1)
