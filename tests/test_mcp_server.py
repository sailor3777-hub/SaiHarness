import os, sys, time, json, pathlib, tempfile, subprocess
from core import db
from core.mcp_manager import McpManager, default_config, load_config

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "saiharness.db"
proj = tmp / "proj"; (proj / "sub").mkdir(parents=True); (proj / ".git").mkdir()
(proj / "hello.txt").write_text("привет\nСекрет: ФИОЛЕТОВЫЙ-МАМОНТ-42\n", encoding="utf-8")
(proj / "sub" / "code.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
(proj / ".git" / "config").write_text("secret", encoding="utf-8")
outside = tmp / "outside.txt"; outside.write_text("не для моделей", encoding="utf-8")
db.add_root(str(proj))

cfg_path = tmp / "mcp.json"
cfg = load_config(cfg_path, db.DB_PATH)
check("конфиг по умолчанию создан", cfg_path.exists() and "toolbox" in cfg["mcpServers"] and cfg == default_config(db.DB_PATH))

m = McpManager(log_dir=tmp / "logs")
t0 = time.time()
def python_procs():
    """Сколько процессов tools_server.py сейчас на машине (в том числе чужих, например из открытого приложения)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tools_server.py*' }).Count"],
        capture_output=True, text=True).stdout.strip()
    return int(out or 0)
baseline = python_procs()   # чужие процессы не трогаем и не считаем своими
states = m.connect_all(cfg)
print(f"      подключение: {time.time()-t0:.2f} с (посторонних процессов tools_server.py: {baseline})")
st = states["toolbox"]
check("сервер подключён", st.status == "ready" and not st.error, st.error)
names = sorted(t.name for t in st.tools)
check("22 инструмента", names == ["calc", "docx_write", "fs_list", "fs_read", "fs_replace", "fs_roots", "fs_search", "image_generate", "memory_delete", "memory_read", "memory_save", "memory_search", "memory_topics", "notes_write", "shell_output", "shell_run", "time_now", "vision_describe", "web_extract", "web_search", "web_search_internet", "xlsx_write"], names)
by = {t.name: t for t in st.tools}
check("id = сервер.инструмент", by["calc"].id == "toolbox.calc")
check("аннотации: чтение / запись", by["fs_read"].read_only is True and by["notes_write"].read_only is False and by["notes_write"].destructive is True)
check("схема аргументов получена", by["fs_read"].input_schema["properties"]["path"]["type"] == "string"
      and by["fs_read"].input_schema["required"] == ["path"], by["fs_read"].input_schema)
check("описание получено", "Читает текстовый файл" in by["fs_read"].description)

call = lambda tool, **a: m.call("toolbox", tool, a)
txt, err = call("calc", expression="(12+8)*3/4"); check("calc", (txt, err) == ("15.0", False), (txt, err))
txt, err = call("calc", expression="2**5000"); check("calc: большая степень отклонена", "ОШИБКА" in txt, txt)
txt, err = call("calc", expression="__import__('os')"); check("calc: не выполняет код", "ОШИБКА" in txt, txt)
txt, err = call("calc", expression="1/0"); check("calc: деление на ноль", "деление на ноль" in txt)
txt, err = call("time_now"); check("time_now", txt[:2] == "20" and not err, txt)
txt, err = call("fs_roots"); check("fs_roots из БД", txt.strip() == str(proj.resolve()), txt)
txt, err = call("fs_list", path=str(proj)); check("fs_list: .git скрыт, папки первыми", txt.splitlines()[0] == "sub/" and "hello.txt" in txt and ".git" not in txt, txt)
txt, err = call("fs_read", path=str(proj / "hello.txt")); check("fs_read", "ФИОЛЕТОВЫЙ-МАМОНТ-42" in txt, txt)
txt, err = call("fs_read", path=str(proj / "hello.txt"), max_chars=6); check("fs_read: обрезка", txt.startswith("привет") and "[обрезано" in txt, txt)
txt, err = call("fs_read", path=str(outside)); check("fs_read: вне корней запрещено", "вне разрешённых" in txt and "не для моделей" not in txt, txt)
txt, err = call("fs_read", path=str(proj / ".." / "outside.txt")); check("fs_read: обход через .. запрещён", "вне разрешённых" in txt, txt)
txt, err = call("fs_list", path=str(tmp)); check("fs_list: родитель корня запрещён", "вне разрешённых" in txt, txt)
txt, err = call("fs_read", path="hello.txt"); check("fs_read: относительный путь внутри корня", "ФИОЛЕТОВЫЙ-МАМОНТ-42" in txt, txt)
txt, err = call("fs_read", path="sub/code.py"); check("fs_read: относительный путь с подпапкой", "def foo" in txt, txt)
txt, err = call("fs_read", path="../outside.txt"); check("fs_read: относительный выход за корень запрещён", "вне разрешённых" in txt and "не для моделей" not in txt, txt)
check("ошибка подсказывает, как исправить вызов", "Передай полный путь" in txt and str(proj.resolve()) in txt, txt)
txt, err = call("fs_list", path="."); check("fs_list: «.» = единственный корень", "hello.txt" in txt and "sub/" in txt, txt)
txt, err = call("fs_list", path=""); check("fs_list: пустой путь = корень", "hello.txt" in txt, txt)
txt, err = call("fs_read", path="nope.txt"); check("несуществующий относительный файл: понятная ошибка", "ОШИБКА" in txt and "файл не найден" in txt and "Das System" not in txt and "fs_list" in txt, txt)
name = proj.name   # имя корня; модели пишут пути в виде «имя_корня/файл», как в списке файлов контекста
txt, err = call("fs_read", path=f"{name}/hello.txt"); check("fs_read: путь с именем корня", "ФИОЛЕТОВЫЙ-МАМОНТ-42" in txt, txt)
txt, err = call("fs_read", path=f"{name.upper()}/HELLO.TXT"); check("fs_read: имя корня и файла без учёта регистра", "ФИОЛЕТОВЫЙ-МАМОНТ-42" in txt, txt)
txt, err = call("fs_read", path=f"{name}/sub/code.py"); check("fs_read: путь с именем корня и подпапкой", "def foo" in txt, txt)
txt, err = call("fs_list", path=name); check("fs_list: имя корня само по себе = корень", "hello.txt" in txt and "sub/" in txt, txt)
txt, err = call("fs_list", path=f"{name}/sub"); check("fs_list: имя корня + подпапка", "code.py" in txt, txt)
txt, err = call("fs_read", path=f"{name}/nope.txt"); check("несуществующий файл с именем корня: ошибка со ссылкой на путь", "файл не найден" in txt and "nope.txt" in txt, txt)
txt, err = call("fs_read", path="sub"); check("fs_read папки: подсказка про fs_list", "это папка" in txt and "fs_list" in txt, txt)
txt, err = call("fs_list", path="hello.txt"); check("fs_list файла: подсказка про fs_read", "это файл" in txt and "fs_read" in txt, txt)
txt, err = call("fs_list", path="nosuchdir"); check("fs_list несуществующей папки", "папка не найдена" in txt, txt)
txt, err = call("fs_search", path="nosuchdir", pattern="x"); check("fs_search по несуществующему пути: ошибка, а не «совпадений нет»", "путь не найден" in txt, txt)
txt, err = call("fs_search", path=str(proj), pattern="def \\w+"); check("fs_search", txt.endswith("code.py:1: def foo():"), txt)
txt, err = call("fs_search", path=str(proj), pattern="secret", ignore_case=True); check("fs_search: .git не обходится", txt == "Совпадений нет", txt)
txt, err = call("fs_search", path=str(proj), pattern="(["); check("fs_search: плохое регулярное выражение", "ОШИБКА" in txt, txt)
txt, err = call("notes_write", name="my note/../x", content="текст"); saved = json.loads(txt)["saved"] if not err else ""
check("notes_write: пишет в data/notes, имя очищено", saved.startswith(str(tmp / "notes")) and ".." not in os.path.basename(saved) and pathlib.Path(saved).read_text(encoding="utf-8") == "текст", txt)
txt, err = m.call("toolbox", "nope", {}); check("неизвестный инструмент -> ошибка", err and txt, txt)
txt, err = m.call("toolbox", "calc", {}); check("неверные аргументы -> ошибка сервера", err, (txt, err))
txt, err = m.call("ghost", "x", {}); check("неизвестный сервер", err and "не подключён" in txt, txt)

# несколько корней: пустой path = все разрешённые папки
second = tmp / "second"; second.mkdir(); (second / "more.txt").write_text("тут лежит NEEDLE-2\n", encoding="utf-8")
db.add_root(str(second))
txt, err = call("fs_list"); check("fs_list без path при нескольких корнях: список корней", str(proj.resolve()) in txt and str(second.resolve()) in txt and "Разрешённые папки" in txt, txt)
txt, err = call("fs_list", path="."); check("fs_list «.» при нескольких корнях: тоже список корней", "Разрешённые папки" in txt, txt)
txt, err = call("fs_search", pattern="NEEDLE-2"); check("fs_search без path ищет во всех корнях", "more.txt:1" in txt, txt)
txt, err = call("fs_search", pattern="ФИОЛЕТОВЫЙ"); check("... в том числе в первом корне", "hello.txt:2" in txt, txt)
txt, err = call("fs_search", pattern="NEEDLE-2", path=str(proj)); check("fs_search с path ограничен этой папкой", txt == "Совпадений нет", txt)
txt, err = call("fs_search", pattern="NEEDLE-2", path="second"); check("fs_search с относительным path = имя второго корня", "more.txt:1" in txt, txt)
txt, err = call("fs_read", path="second/more.txt"); check("fs_read по имени второго корня", "NEEDLE-2" in txt, txt)
txt, err = call("fs_search", pattern="x", path="nosuch"); check("fs_search по несуществующему path при нескольких корнях", "ОШИБКА" in txt, txt)
sch = {t.name: t.input_schema for t in m.all_tools()}
check("pattern теперь первым аргументом, path необязателен", list(sch["fs_search"]["properties"])[:2] == ["pattern", "path"] and sch["fs_search"]["required"] == ["pattern"], sch["fs_search"])
check("fs_list: path необязателен", "required" not in sch["fs_list"] or sch["fs_list"]["required"] == [], sch["fs_list"])
(second / "deep").mkdir(); (second / "deep" / "inner.txt").write_text("глубоко", encoding="utf-8")
txt, err = call("fs_read", path="inner.txt")
check("относительный путь без подпапки при нескольких корнях: «не найден», а не «вне папок»", "Путь не найден" in txt and "вне разрешённых" not in txt, txt)
check("... с подсказкой, где файл на самом деле лежит", "inner.txt" in txt and "deep" in txt and "Возможно, имелось в виду" in txt, txt)
txt, err = call("fs_read", path="deep/inner.txt"); check("а с правильным относительным путём находится", "глубоко" in txt, txt)
txt, err = call("fs_read", path="totally_missing.txt")
check("файла нет нигде: подсказка про fs_search / fs_list", "Путь не найден" in txt and "fs_search" in txt and "Возможно" not in txt, txt)
txt, err = call("fs_list", path="inner.txt"); check("fs_list с именем файла из подпапки: тоже подсказка", "Путь не найден" in txt and "deep" in txt, txt)
txt, err = call("fs_replace", path="inner.txt", old="глубоко", new="x"); check("fs_replace с именем без пути: подсказка, файл цел", "Путь не найден" in txt and (second / "deep" / "inner.txt").read_text(encoding="utf-8") == "глубоко", txt)
txt, err = call("fs_read", path=str(tmp / "outside.txt")); check("абсолютный путь вне папок по-прежнему «вне разрешённых»", "вне разрешённых" in txt, txt)
db.remove_root(str(second))

# отказоустойчивость конфига
bad = {"mcpServers": {"nocmd": {"args": []}, "off": {"command": "x", "disabled": True},
                      "broken": {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]},
                      "missing": {"command": "no-such-binary-xyz"}, "toolbox": cfg["mcpServers"]["toolbox"]}}
t0 = time.time(); states = m.connect_all(bad)
print(f"      переподключение с плохими серверами: {time.time()-t0:.2f} с")
check("нет command", states["nocmd"].status == "error" and "command" in states["nocmd"].error)
check("disabled", states["off"].status == "disabled")
check("сервер, который сразу падает", states["broken"].status == "error" and states["broken"].error, states["broken"])
check("несуществующая программа", states["missing"].status == "error" and states["missing"].error, states["missing"])
check("исправный сервер работает рядом с плохими", states["toolbox"].status == "ready" and m.call("toolbox", "calc", {"expression": "2+2"}) == ("4", False))
check("all_tools только из готовых", {t.server for t in m.all_tools()} == {"toolbox"})

# закрытие завершает процессы
n_before = python_procs()
m.close_servers(); time.sleep(0.5)
check("close_servers: процесс сервера завершён (относительно исходных)", n_before > baseline and python_procs() == baseline, (baseline, n_before, python_procs()))
check("после закрытия вызов даёт понятную ошибку", m.call("toolbox", "calc", {"expression": "1"})[1])
m.connect_all(cfg); check("повторное подключение после закрытия", m.states["toolbox"].status == "ready")
m.shutdown(); time.sleep(0.5)
check("shutdown: своих процессов не осталось", python_procs() == baseline, (baseline, python_procs()))
print("ALL OK" if ok_all else "SOME FAILED")


