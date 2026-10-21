"""The memory_* tools through the real toolbox MCP server, the rules in the profile, the prompt block and tool dependencies."""
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

# --- dependencies ---
req = {"a": ("b",), "b": ("c",), "c": ("a",), "x": ()}
check("dependencies are transitive and tolerate a cycle", with_requirements({"a"}, req) == {"a", "b", "c"})
check("without dependencies the set is the same", with_requirements({"x", "z"}, req) == {"x", "z"})
check("an empty set", with_requirements(set(), req) == set())

# --- profile: memory rules ---
db.save_profile("P", "c", "m", "SYS", 0.7, 100, "", 5)
check("the default rules are empty", db.get_profile("P")["memory_rules"] == "")
db.save_profile("P", "c", "m", "SYS", 0.7, 100, "", 5, memory_rules="My rules")
check("the rules are saved", db.get_profile("P")["memory_rules"] == "My rules")

# --- prompt block ---
mem = Memory(db.connect())
check("without memory tools there is no block", prompt_block("", ["toolbox.calc"], mem) == "")
b = prompt_block("", ["toolbox.memory_search"], mem)
check("the default rules and an empty memory", DEFAULT_RULES[:40] in b and "none yet" in b)
mem.save("trips", "Makkum", "We went to Makkum")
b = prompt_block("Custom rules", ["toolbox.memory_save"], mem)
check("custom rules and topics", b.startswith("Custom rules") and "Topics in memory: trips (1)" in b, b)

# --- the real server ---
manager = McpManager(log_dir=tmp / "logs")
manager.connect_all(default_config(db.DB_PATH))
tools = list(manager.states["toolbox"].tools)
by = {t.name: t for t in tools}
check("memory tools are registered", all(n in by for n in ("memory_save", "memory_search", "memory_topics", "memory_read", "memory_delete")))
check("writing and deleting ask, reading does not",
      default_policy(by["memory_save"]) == "ask" and default_policy(by["memory_delete"]) == "ask"
      and default_policy(by["memory_search"]) == "auto" and default_policy(by["memory_read"]) == "auto")

router = ToolRouter(manager, Catalog(tools), confirm=lambda tool_id, args: True)
def call(name, **args):
    return router.handle("call_tool", {"tool": f"toolbox.{name}", "arguments": args})

r = call("memory_save", topic="projects", title="Memory decision", text="Memory is shared by all profiles, search uses FTS5.")
check("save", r.startswith("Saved #") and "projects" in r, r)
r = call("memory_search", query="remember what we decided about memory?")
check("search finds", "Memory decision" in r and "FTS5" in r, r)
check("search without matches gives a hint", "Nothing in memory" in call("memory_search", query="helicopter"))
check("topics", "trips" in call("memory_topics") and "projects" in call("memory_topics"))
r = call("memory_topics", topic="Projects")
check("topics of a topic", "Memory decision" in r, r)
mid = int(call("memory_search", query="Makkum").split("#")[1].split(" ")[0])
check("read", "We went to Makkum" in call("memory_read", id=mid))
r = call("memory_save", topic="projects", title="memory decision", text="Updated.")
check("saving again updates", r.startswith("Updated entry"), r)
check("an empty text: an error", "ERROR" in call("memory_save", topic="t", title="h", text=" "))
check("delete", call("memory_delete", id=mid).startswith("Deleted entry"))
check("not found after delete", "Nothing in memory" in call("memory_search", query="Makkum"))
check("read a missing one", "ERROR" in call("memory_read", id=9999))

declined = ToolRouter(manager, Catalog(tools), confirm=lambda tool_id, args: False)
r = declined.handle("call_tool", {"tool": "toolbox.memory_save", "arguments": {"topic": "t", "title": "h", "text": "t"}})
check("the user refused: nothing saved", "declined" in r and Memory(sqlite3.connect(db.DB_PATH)).search("t h") == [], r)

manager.shutdown()
print("\nALL OK" if ok_all else "\nSOME FAILED")
