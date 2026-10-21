import os, sys, time, json, pathlib, tempfile, subprocess
from core import db
from core.mcp_manager import default_config, load_config
from model_view import McpManager  # errors with the prefix, as the model sees them

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
proj = tmp / "proj"; (proj / "sub").mkdir(parents=True); (proj / ".git").mkdir()
(proj / "hello.txt").write_text("hello\nCode word: PURPLE-MAMMOTH-42\n", encoding="utf-8")
(proj / "sub" / "code.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
(proj / ".git" / "config").write_text("secret", encoding="utf-8")
outside = tmp / "outside.txt"; outside.write_text("not for models", encoding="utf-8")
db.add_root(str(proj))

cfg_path = tmp / "mcp.json"
cfg = load_config(cfg_path, db.DB_PATH)
check("default config created", cfg_path.exists() and "toolbox" in cfg["mcpServers"] and cfg == default_config(db.DB_PATH))

m = McpManager(log_dir=tmp / "logs")
t0 = time.time()
def python_procs():
    """How many tools_server.py processes are on the machine now (including others, for example from an open app)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tools_server.py*' }).Count"],
        capture_output=True, text=True).stdout.strip()
    return int(out or 0)
baseline = python_procs()   # other processes are neither touched nor counted as ours
states = m.connect_all(cfg)
print(f"      connection: {time.time()-t0:.2f} s (other tools_server.py processes: {baseline})")
st = states["toolbox"]
check("server connected", st.status == "ready" and not st.error, st.error)
names = sorted(t.name for t in st.tools)
check("22 tools", names == ["calc", "docx_write", "fs_list", "fs_read", "fs_replace", "fs_roots", "fs_search", "image_generate", "memory_delete", "memory_read", "memory_save", "memory_search", "memory_topics", "notes_write", "shell_output", "shell_run", "time_now", "vision_describe", "web_extract", "web_search", "web_search_internet", "xlsx_write"], names)
by = {t.name: t for t in st.tools}
check("id = server.tool", by["calc"].id == "toolbox.calc")
check("annotations: read / write", by["fs_read"].read_only is True and by["notes_write"].read_only is False and by["notes_write"].destructive is True)
check("argument schema received", by["fs_read"].input_schema["properties"]["path"]["type"] == "string"
      and by["fs_read"].input_schema["required"] == ["path"], by["fs_read"].input_schema)
check("description received", "Reads a text file" in by["fs_read"].description)

call = lambda tool, **a: m.call("toolbox", tool, a)
txt, err = call("calc", expression="(12+8)*3/4"); check("calc", (txt, err) == ("15.0", False), (txt, err))
txt, err = call("calc", expression="2**5000"); check("calc: a large exponent is refused", "ERROR" in txt, txt)
txt, err = call("calc", expression="__import__('os')"); check("calc: does not run code", "ERROR" in txt, txt)
txt, err = call("calc", expression="1/0"); check("calc: division by zero", "division by zero" in txt)
txt, err = call("time_now"); check("time_now", txt[:2] == "20" and not err, txt)
txt, err = call("fs_roots"); check("fs_roots from the DB", txt.strip() == str(proj.resolve()), txt)
txt, err = call("fs_list", path=str(proj)); check("fs_list: .git hidden, folders first", txt.splitlines()[0] == "sub/" and "hello.txt" in txt and ".git" not in txt, txt)
txt, err = call("fs_read", path=str(proj / "hello.txt")); check("fs_read", "PURPLE-MAMMOTH-42" in txt, txt)
txt, err = call("fs_read", path=str(proj / "hello.txt"), max_chars=6); check("fs_read: truncation", txt.startswith("hello") and "[truncated" in txt, txt)
txt, err = call("fs_read", path=str(outside)); check("fs_read: outside the roots is refused", "outside the allowed" in txt and "not for models" not in txt, txt)
txt, err = call("fs_read", path=str(proj / ".." / "outside.txt")); check("fs_read: escaping with .. is refused", "outside the allowed" in txt, txt)
txt, err = call("fs_list", path=str(tmp)); check("fs_list: the root's parent is refused", "outside the allowed" in txt, txt)
txt, err = call("fs_read", path="hello.txt"); check("fs_read: a relative path inside the root", "PURPLE-MAMMOTH-42" in txt, txt)
txt, err = call("fs_read", path="sub/code.py"); check("fs_read: a relative path with a subfolder", "def foo" in txt, txt)
txt, err = call("fs_read", path="../outside.txt"); check("fs_read: a relative escape from the root is refused", "outside the allowed" in txt and "not for models" not in txt, txt)
check("the error suggests how to fix the call", "Pass a full path" in txt and str(proj.resolve()) in txt, txt)
txt, err = call("fs_list", path="."); check("fs_list: \".\" = the only root", "hello.txt" in txt and "sub/" in txt, txt)
txt, err = call("fs_list", path=""); check("fs_list: empty path = root", "hello.txt" in txt, txt)
txt, err = call("fs_read", path="nope.txt"); check("a missing relative file: a clear error", "ERROR" in txt and "file not found" in txt and "Das System" not in txt and "fs_list" in txt, txt)
name = proj.name   # the root's name; models write paths as "root_name/file", as in the context file list
txt, err = call("fs_read", path=f"{name}/hello.txt"); check("fs_read: a path with the root name", "PURPLE-MAMMOTH-42" in txt, txt)
txt, err = call("fs_read", path=f"{name.upper()}/HELLO.TXT"); check("fs_read: root and file names are case-insensitive", "PURPLE-MAMMOTH-42" in txt, txt)
txt, err = call("fs_read", path=f"{name}/sub/code.py"); check("fs_read: a path with the root name and a subfolder", "def foo" in txt, txt)
txt, err = call("fs_list", path=name); check("fs_list: the root name alone = the root", "hello.txt" in txt and "sub/" in txt, txt)
txt, err = call("fs_list", path=f"{name}/sub"); check("fs_list: root name + subfolder", "code.py" in txt, txt)
txt, err = call("fs_read", path=f"{name}/nope.txt"); check("a missing file with the root name: an error pointing to the path", "file not found" in txt and "nope.txt" in txt, txt)
txt, err = call("fs_read", path="sub"); check("fs_read of a folder: a hint about fs_list", "this is a folder" in txt and "fs_list" in txt, txt)
txt, err = call("fs_list", path="hello.txt"); check("fs_list of a file: a hint about fs_read", "this is a file" in txt and "fs_read" in txt, txt)
txt, err = call("fs_list", path="nosuchdir"); check("fs_list of a missing folder", "folder not found" in txt, txt)
txt, err = call("fs_search", path="nosuchdir", pattern="x"); check("fs_search on a missing path: an error, not \"no matches\"", "path not found" in txt, txt)
txt, err = call("fs_search", path=str(proj), pattern="def \\w+"); check("fs_search", txt.endswith("code.py:1: def foo():"), txt)
txt, err = call("fs_search", path=str(proj), pattern="secret", ignore_case=True); check("fs_search: .git is not traversed", txt == "No matches", txt)
txt, err = call("fs_search", path=str(proj), pattern="(["); check("fs_search: a bad regular expression", "ERROR" in txt, txt)
txt, err = call("notes_write", name="my note/../x", content="text"); saved = json.loads(txt)["saved"] if not err else ""
check("notes_write: writes to data/notes, the name is cleaned", saved.startswith(str(tmp / "notes")) and ".." not in os.path.basename(saved) and pathlib.Path(saved).read_text(encoding="utf-8") == "text", txt)
txt, err = m.call("toolbox", "nope", {}); check("unknown tool -> error", err and txt, txt)
txt, err = m.call("toolbox", "calc", {}); check("invalid arguments -> server error", err, (txt, err))
txt, err = m.call("ghost", "x", {}); check("unknown server", err and "is not connected" in txt, txt)

# a tool error arrives as the is_error flag without the prefix, and the router counts such errors in a row
from core.mcp_manager import McpManager as RawManager
from core.markers import is_error
from core.tools import Catalog, ToolRouter
raw_txt, raw_err = RawManager.call(m, "toolbox", "fs_read", {"path": "nope.txt"})
check("server error: the is_error flag, text without the prefix", raw_err and not is_error(raw_txt) and "file not found" in raw_txt, (raw_txt, raw_err))
raw_txt, raw_err = RawManager.call(m, "toolbox", "fs_read", {"path": "hello.txt"})
check("success: no flag", not raw_err and "PURPLE-MAMMOTH-42" in raw_txt, raw_txt)
raw_view = type("Raw", (), {"call": lambda self, *a: RawManager.call(m, *a)})()
router = ToolRouter(raw_view, Catalog(m.all_tools()))
missing = json.dumps({"tool": "toolbox.fs_read", "arguments": {"path": "nope.txt"}})
results = [router.handle("call_tool", missing) for _ in range(3)]
check("router: a server error with the prefix", all(is_error(r) for r in results), results)
check("router: the third identical error blocks the tool", router.stuck == "toolbox.fs_read", results[-1])

# several roots: an empty path = all allowed folders
second = tmp / "second"; second.mkdir(); (second / "more.txt").write_text("NEEDLE-2 lives here\n", encoding="utf-8")
db.add_root(str(second))
txt, err = call("fs_list"); check("fs_list without path with several roots: the list of roots", str(proj.resolve()) in txt and str(second.resolve()) in txt and "Allowed folders" in txt, txt)
txt, err = call("fs_list", path="."); check("fs_list \".\" with several roots: also the list of roots", "Allowed folders" in txt, txt)
txt, err = call("fs_search", pattern="NEEDLE-2"); check("fs_search without path searches all roots", "more.txt:1" in txt, txt)
txt, err = call("fs_search", pattern="PURPLE"); check("... including the first root", "hello.txt:2" in txt, txt)
txt, err = call("fs_search", pattern="NEEDLE-2", path=str(proj)); check("fs_search with path is limited to that folder", txt == "No matches", txt)
txt, err = call("fs_search", pattern="NEEDLE-2", path="second"); check("fs_search with a relative path = the second root's name", "more.txt:1" in txt, txt)
txt, err = call("fs_read", path="second/more.txt"); check("fs_read by the second root's name", "NEEDLE-2" in txt, txt)
txt, err = call("fs_search", pattern="x", path="nosuch"); check("fs_search on a missing path with several roots", "ERROR" in txt, txt)
sch = {t.name: t.input_schema for t in m.all_tools()}
check("pattern is now the first argument, path is optional", list(sch["fs_search"]["properties"])[:2] == ["pattern", "path"] and sch["fs_search"]["required"] == ["pattern"], sch["fs_search"])
check("fs_list: path is optional", "required" not in sch["fs_list"] or sch["fs_list"]["required"] == [], sch["fs_list"])
(second / "deep").mkdir(); (second / "deep" / "inner.txt").write_text("deep inside", encoding="utf-8")
txt, err = call("fs_read", path="inner.txt")
check("a relative path without the subfolder with several roots: \"not found\", not \"outside the folders\"", "Path not found" in txt and "outside the allowed" not in txt, txt)
check("... with a hint where the file actually is", "inner.txt" in txt and "deep" in txt and "Did you mean" in txt, txt)
txt, err = call("fs_read", path="deep/inner.txt"); check("and with the correct relative path it is found", "deep inside" in txt, txt)
txt, err = call("fs_read", path="totally_missing.txt")
check("the file is nowhere: a hint about fs_search / fs_list", "Path not found" in txt and "fs_search" in txt and "Did you mean" not in txt, txt)
txt, err = call("fs_list", path="inner.txt"); check("fs_list with a file name from a subfolder: a hint too", "Path not found" in txt and "deep" in txt, txt)
txt, err = call("fs_replace", path="inner.txt", old="deep inside", new="x"); check("fs_replace with a name without a path: a hint, the file is intact", "Path not found" in txt and (second / "deep" / "inner.txt").read_text(encoding="utf-8") == "deep inside", txt)
txt, err = call("fs_read", path=str(tmp / "outside.txt")); check("an absolute path outside the folders is still \"outside the allowed\"", "outside the allowed" in txt, txt)
db.remove_root(str(second))

# config fault tolerance
bad = {"mcpServers": {"nocmd": {"args": []}, "off": {"command": "x", "disabled": True},
                      "broken": {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]},
                      "missing": {"command": "no-such-binary-xyz"}, "toolbox": cfg["mcpServers"]["toolbox"]}}
t0 = time.time(); states = m.connect_all(bad)
print(f"      reconnecting with bad servers: {time.time()-t0:.2f} s")
check("no command", states["nocmd"].status == "error" and "command" in states["nocmd"].error)
check("disabled", states["off"].status == "disabled")
check("a server that crashes at once", states["broken"].status == "error" and states["broken"].error, states["broken"])
check("a program that does not exist", states["missing"].status == "error" and states["missing"].error, states["missing"])
check("a working server runs next to the bad ones", states["toolbox"].status == "ready" and m.call("toolbox", "calc", {"expression": "2+2"}) == ("4", False))
check("all_tools only from the ready ones", {t.server for t in m.all_tools()} == {"toolbox"})

# closing ends the processes
n_before = python_procs()
m.close_servers(); time.sleep(0.5)
check("close_servers: the server process ended (relative to the baseline)", n_before > baseline and python_procs() == baseline, (baseline, n_before, python_procs()))
check("after closing a call gives a clear error", m.call("toolbox", "calc", {"expression": "1"})[1])
m.connect_all(cfg); check("reconnecting after closing", m.states["toolbox"].status == "ready")
m.shutdown(); time.sleep(0.5)
check("shutdown: none of our processes are left", python_procs() == baseline, (baseline, python_procs()))
print("ALL OK" if ok_all else "SOME FAILED")


