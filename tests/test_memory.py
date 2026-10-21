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


# --- stems of Russian words (entries saved by the Russian version) ---
check("case forms of one Russian word match", stem("Маккум") == stem("Маккуме") == stem("Маккума"))
check("Russian yo = ye", stem("пётр") == stem("петр"))
check("numbers are not cut", stem("2024") == "2024")
check("short words whole", stem("дом") == "дом")
check("stems splits a Russian phrase", stems("Ездили в Маккум!") == ["езди", "в", "макк"])
ru = Memory(sqlite3.connect(":memory:")); rid, _ = ru.save("поездки", "Поездка в Маккум", "Ездили в Маккум в сентябре.")
check("a Russian entry is found by another word form", [x["id"] for x in ru.search("помнишь, ездили в Маккуме?")] == [rid])

mem = Memory(sqlite3.connect(":memory:"))
check("empty: no topics", mem.topics() == [] and mem.topics_line() == "" and mem.count() == 0)

a, upd = mem.save("trips", "Trip to Makkum", "We went to Makkum in September, the three of us, and camped in a tent by the lake.", source_chat=7)
b, _ = mem.save("trips", "Baikal", "In summer 2023 I was at Lake Baikal with my brother, we saw a seal.")
c, _ = mem.save("project SaiHarness", "Memory decision", "Memory is shared by all profiles, search uses FTS5 with stemming.")
check("three entries", mem.count() == 3 and not upd)
check("ids differ", len({a, b, c}) == 3)

# --- search ---
r = mem.search("remember, we went to Makkum?")
check("found by the words of a question", r and r[0]["id"] == a, r)
check("the result has the topic and title", r and r[0]["topic"] == "trips" and r[0]["title"] == "Trip to Makkum")
check("by a word from the title", mem.search("baikal")[0]["id"] == b)
check("by topic", {x["id"] for x in mem.search("trips")} >= {a, b})
check("the OR fallback: some words are not found", mem.search("Makkum helicopter")[0]["id"] == a)
check("nothing found", mem.search("quantum chromodynamics") == [])
check("an empty query", mem.search("  ") == [] and mem.search("...") == [])
check("topic filter", [x["id"] for x in mem.search("memory", topic="trips")] == [])
check("the topic filter finds", [x["id"] for x in mem.search("memory", topic="PROJECT saiharness")] == [c])
check("limit", len(mem.search("trips", limit=1)) == 1)
check("FTS special characters do not break the search", mem.search('"makkum" OR (x) * NEAR') is not None)

# --- update, delete ---
a2, upd2 = mem.save("Trips", "trip to makkum", "Update: we went in two cars.")
check("the same title and topic: an update", upd2 and a2 == a and mem.count() == 3)
check("the new text is searchable", mem.search("cars")[0]["id"] == a)
check("the old text left the index", mem.search("tent") == [])
mem.update(b, "trips", "Baikal 2023", "Now about Altai.")
check("update changes the index", mem.search("altai")[0]["id"] == b and mem.search("seal") == [])
check("delete", mem.delete(c) and mem.get(c) is None and mem.search("stemming") == [])
check("delete a missing one", not mem.delete(9999))
check("update a missing one", raises(lambda: mem.update(9999, "t", "h", "t")))

# --- topics ---
check("topics: case does not create extra topics", [(t.lower(), n) for t, n in mem.topics()] == [("trips", 2)], mem.topics())
check("topics_line", mem.topics_line().lower() == "trips (2)")
for i in range(15):
    mem.save(f"topic{i}", "h", "t")
check("topics_line is cut", "more topics" in mem.topics_line(limit=5))
check("list by topic", len(mem.list("Trips")) == 2)

# --- input checks ---
check("an empty topic", raises(lambda: mem.save("", "h", "t")))
check("an empty text", raises(lambda: mem.save("t", "h", "   ")))
check("a long title", raises(lambda: mem.save("t", "h" * 200, "t")))
check("line breaks are kept in the text", mem.get(mem.save("t", "lines", "a" + chr(10) + "b")[0])["text"] == "a" + chr(10) + "b")

# --- English stems ---
check("name/names/named/naming", stem("name") == stem("names") == stem("named") == stem("naming"), [stem(w) for w in ("name", "names", "named", "naming")])
check("plan/planned/planning", stem("plan") == stem("planned") == stem("planning"))
check("city/cities, box/boxes", stem("city") == stem("cities") and stem("box") == stem("boxes"))
check("computer/computing", stem("computer") == stem("computing"))
check("company and computer differ", stem("company") != stem("computer"))
check("class and this keep their s", stem("class") == "class" and stem("this") == "this")
en = Memory(sqlite3.connect(":memory:"))
en.save("trips", "Spain", "We planned a trip to Valencia and visited three cities.")
check("English search by word form", [r["title"] for r in en.search("planning trips to the city")] == ["Spain"])

# --- reindexing an old database (version 1: every word cut to 4 letters) ---
old = sqlite3.connect(":memory:")
Memory(old).save("поездки", "Испания", "Planned trip to Valencia, ездили в Маккум")
old.execute("UPDATE memories SET i_text='plan trip to vale езди в макк'")
old.execute("UPDATE memory_meta SET value='1'")
old.commit()
re_mem = Memory(old)
check("after reindexing an English word is found", len(re_mem.search("Valencia")) == 1)
check("and a Russian one still is", len(re_mem.search("в Маккуме")) == 1)
check("the index version is updated", old.execute("SELECT value FROM memory_meta").fetchone()[0] == "2")

print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
