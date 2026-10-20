"""fs_replace на реальном MCP-сервере: безопасность, кодировки, переводы строк, копии, атомарность."""
import os, stat, sys, time, pathlib, tempfile
from core import db
from core.files import decode_for_edit
from core.mcp_manager import McpManager, load_config

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t34.db"
root = tmp / "proj"; (root / "sub").mkdir(parents=True); (root / ".git").mkdir()
db.add_root(str(root)); outside = tmp / "outside.txt"; outside.write_bytes("секрет".encode())
m = McpManager(); m.connect_all(load_config(tmp / "mcp.json", db.DB_PATH))
def rep(path, old, new): return m.call("toolbox", "fs_replace", {"path": str(path), "old": old, "new": new})[0]
def put(name, data): p = root / name; p.parent.mkdir(exist_ok=True, parents=True); p.write_bytes(data); return p
def baks(p): return sorted(x.name for x in p.parent.iterdir() if x.name.startswith(p.name + ".bak"))
def leftovers(): return [x.name for x in root.rglob("*") if x.name.endswith(".sai-tmp")]

# ---- основной сценарий (как у пользователя) ----
req = put("requirements.txt", b"pygame-ce>=2.5\n\nyellow sausage\n")
out = rep(req, "yellow sausage", "brown bear")
check("замена выполнена", out.startswith("Готово: заменено 1 вхождение") and "строка 3" in out, out)
check("файл изменён ровно в одном месте", req.read_bytes() == b"pygame-ce>=2.5\n\nbrown bear\n")
check("копия .bak с исходными байтами", baks(req) == ["requirements.txt.bak"] and (root / "requirements.txt.bak").read_bytes() == b"pygame-ce>=2.5\n\nyellow sausage\n")
check("временных файлов не осталось", leftovers() == [])
out = rep(req, "brown bear", "red fox")
check("вторая правка: копии не затираются (.bak.1)", baks(req) == ["requirements.txt.bak", "requirements.txt.bak.1"]
      and (root / "requirements.txt.bak").read_bytes().endswith(b"yellow sausage\n") and (root / "requirements.txt.bak.1").read_bytes().endswith(b"brown bear\n"), baks(req))

# ---- отказы: файл не меняется, копий нет ----
f = put("dup.txt", b"a b a\n"); snap = f.read_bytes()
out = rep(f, "a", "x"); check("несколько вхождений: ошибка с числом", out.startswith("ОШИБКА") and "2 раз" in out and f.read_bytes() == snap and baks(f) == [], out)
out = rep(f, "zzz", "x"); check("нет вхождений: ошибка", "не найден" in out and f.read_bytes() == snap and baks(f) == [], out)
out = rep(f, "", "x"); check("пустой old", "old пуст" in out and f.read_bytes() == snap)
out = rep(f, "a b", "a b"); check("old == new", "совпадают" in out and baks(f) == [])
out = rep(outside, "секрет", "x"); check("вне разрешённых папок", "вне разрешённых" in out and outside.read_bytes() == "секрет".encode(), out)
out = rep(root / ".." / "outside.txt", "секрет", "x"); check("обход через .. запрещён", "вне разрешённых" in out and outside.read_bytes() == "секрет".encode())
out = rep(root / "nope.txt", "a", "b"); check("несуществующий файл", "файл не найден" in out, out)
out = rep(root / "sub", "a", "b"); check("папка вместо файла", "это папка" in out, out)
g = put(".git/config", b"[core]\n"); out = rep(g, "core", "x"); check("служебная папка .git править нельзя", "нельзя" in out and g.read_bytes() == b"[core]\n", out)
png = put("img.png", b"\x89PNG abc"); out = rep(png, "abc", "x"); check("бинарное расширение", "нельзя" in out and png.read_bytes() == b"\x89PNG abc")
nul = put("data.txt", b"abc\x00def"); out = rep(nul, "abc", "x"); check("бинарное содержимое (NUL)", "бинарный" in out and nul.read_bytes() == b"abc\x00def", out)
big = put("big.txt", b"x" * 1_100_000); out = rep(big, "x", "y"); check("слишком большой файл", "слишком большой" in out and big.stat().st_size == 1_100_000, out)
u16 = put("u16.txt", "привет".encode("utf-16")); out = rep(u16, "привет", "пока"); check("UTF-16 не правится, файл цел", "UTF-16" in out and u16.read_bytes() == "привет".encode("utf-16"), out)

# ---- кодировки и переводы строк сохраняются ----
bom = put("bom.txt", b"\xef\xbb\xbf" + "Привет, мир\nвторая строка\n".encode("utf-8"))
out = rep(bom, "мир", "свет"); check("UTF-8 с BOM: BOM сохранён", bom.read_bytes() == b"\xef\xbb\xbf" + "Привет, свет\nвторая строка\n".encode("utf-8"), out)
nobom = put("nobom.txt", "Привет, мир\n".encode("utf-8")); rep(nobom, "мир", "свет"); check("UTF-8 без BOM: BOM не добавлен", nobom.read_bytes() == "Привет, свет\n".encode("utf-8"))
cp = put("cp.txt", "Русский текст в cp1251\nстрока два\n".encode("cp1251")); out = rep(cp, "строка два", "строка три")
check("cp1251: байты записаны в той же кодировке", cp.read_bytes() == "Русский текст в cp1251\nстрока три\n".encode("cp1251"), out)
snap = cp.read_bytes(); out = rep(cp, "строка три", "строка 😀")
check("cp1251: символ вне кодировки -> отказ, файл цел", "нет в кодировке" in out and cp.read_bytes() == snap, out)
crlf = put("crlf.txt", b"one\r\ntwo\r\nthree\r\n"); out = rep(crlf, "two\nthree", "2\n3")
check("CRLF: многострочный old с \\n находит совпадение и CRLF сохраняется", crlf.read_bytes() == b"one\r\n2\r\n3\r\n", (out, crlf.read_bytes()))
out = rep(crlf, "one\r\n2", "ONE\r\n2"); check("CRLF: old уже с \\r\\n тоже работает", crlf.read_bytes() == b"ONE\r\n2\r\n3\r\n", (out, crlf.read_bytes()))
mixed = put("mixed.txt", b"a\r\nb\nc\r\n"); out = rep(mixed, "b", "B"); check("смешанные переводы строк не нормализуются", mixed.read_bytes() == b"a\r\nB\nc\r\n", (out, mixed.read_bytes()))
noeol = put("noeol.txt", b"last line"); rep(noeol, "last", "LAST"); check("файл без перевода строки в конце остаётся таким", noeol.read_bytes() == b"LAST line")
multi = put("multi.py", b"def f():\n    return 1\n\n\ndef g():\n    return 2\n"); out = rep(multi, "    return 1\n", "    return 10\n    # changed\n")
check("многострочная замена с отступами", multi.read_bytes() == b"def f():\n    return 10\n    # changed\n\n\ndef g():\n    return 2\n", out)
specials = put("sp.txt", b"price: $5 (50%) [x] {y} \\n\n"); rep(specials, "$5 (50%) [x] {y} \\n", "$6"); check("спецсимволы в old/new трактуются буквально, не как регулярное выражение", specials.read_bytes() == b"price: $6\n")
check("временных файлов по-прежнему нет", leftovers() == [])

# ---- пути ----
rel = put("sub/rel.txt", b"alpha\n"); out = rep("sub/rel.txt", "alpha", "beta"); check("относительный путь", rel.read_bytes() == b"beta\n", out)
out = rep(f"{root.name}/sub/rel.txt", "beta", "gamma"); check("путь с именем корня", rel.read_bytes() == b"gamma\n", out)

# ---- права доступа ----
ro = put("ro.txt", b"keep\n"); os.chmod(ro, stat.S_IREAD)
out = rep(ro, "keep", "lost")
os.chmod(ro, stat.S_IWRITE | stat.S_IREAD)
check("файл только для чтения: содержимое цело", ro.read_bytes() == b"keep\n", out)
print("      результат для файла только для чтения:", out[:120])
check("после неудачной записи нет временных файлов и лишней копии", leftovers() == [] and baks(ro) == [], baks(ro))

# ---- decode_for_edit напрямую ----
check("decode_for_edit: UTF-8", decode_for_edit("ё".encode()) == ("ё", "utf-8"))
check("decode_for_edit: BOM", decode_for_edit(b"\xef\xbb\xbfabc") == ("abc", "utf-8-sig"))
check("decode_for_edit: cp1251 без потерь", decode_for_edit("ё".encode("cp1251")) == ("ё", "cp1251"))
check("decode_for_edit: байт 0x98 (не определён в cp1251) -> отказ", decode_for_edit(b"abc\x98")[0] is None)
check("decode_for_edit: UTF-16 и NUL -> отказ", decode_for_edit(b"\xff\xfea\x00")[0] is None and decode_for_edit(b"a\x00b")[0] is None)

m.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
