"""fs_replace on the real MCP server: safety, encodings, line breaks, backups, atomicity."""
import os, stat, sys, time, pathlib, tempfile
from core import db
from core.files import decode_for_edit
from core.mcp_manager import load_config
from model_view import McpManager  # errors with the prefix, as the model sees them

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t34.db"
root = tmp / "proj"; (root / "sub").mkdir(parents=True); (root / ".git").mkdir()
db.add_root(str(root)); outside = tmp / "outside.txt"; outside.write_bytes("secret".encode())
m = McpManager(); m.connect_all(load_config(tmp / "mcp.json", db.DB_PATH))
def rep(path, old, new): return m.call("toolbox", "fs_replace", {"path": str(path), "old": old, "new": new})[0]
def put(name, data): p = root / name; p.parent.mkdir(exist_ok=True, parents=True); p.write_bytes(data); return p
def baks(p): return sorted(x.name for x in p.parent.iterdir() if x.name.startswith(p.name + ".bak"))
def leftovers(): return [x.name for x in root.rglob("*") if x.name.endswith(".sai-tmp")]

# ---- the main scenario (as the user does it) ----
req = put("requirements.txt", b"pygame-ce>=2.5\n\nyellow sausage\n")
out = rep(req, "yellow sausage", "brown bear")
check("the replacement is done", out.startswith("Done: replaced 1 occurrence") and "line 3" in out, out)
check("the file is changed in exactly one place", req.read_bytes() == b"pygame-ce>=2.5\n\nbrown bear\n")
check("a .bak copy with the original bytes", baks(req) == ["requirements.txt.bak"] and (root / "requirements.txt.bak").read_bytes() == b"pygame-ce>=2.5\n\nyellow sausage\n")
check("no temporary files left", leftovers() == [])
out = rep(req, "brown bear", "red fox")
check("the second edit: backups are not overwritten (.bak.1)", baks(req) == ["requirements.txt.bak", "requirements.txt.bak.1"]
      and (root / "requirements.txt.bak").read_bytes().endswith(b"yellow sausage\n") and (root / "requirements.txt.bak.1").read_bytes().endswith(b"brown bear\n"), baks(req))

# ---- refusals: the file does not change, no backups ----
f = put("dup.txt", b"a b a\n"); snap = f.read_bytes()
out = rep(f, "a", "x"); check("several occurrences: an error with the count", out.startswith("ERROR") and "2 times" in out and f.read_bytes() == snap and baks(f) == [], out)
out = rep(f, "zzz", "x"); check("no occurrences: an error", "not found" in out and f.read_bytes() == snap and baks(f) == [], out)
out = rep(f, "", "x"); check("empty old", "old is empty" in out and f.read_bytes() == snap)
out = rep(f, "a b", "a b"); check("old == new", "are the same" in out and baks(f) == [])
out = rep(outside, "secret", "x"); check("outside the allowed folders", "outside the allowed" in out and outside.read_bytes() == "secret".encode(), out)
out = rep(root / ".." / "outside.txt", "secret", "x"); check("escaping with .. is refused", "outside the allowed" in out and outside.read_bytes() == "secret".encode())
out = rep(root / "nope.txt", "a", "b"); check("a missing file", "file not found" in out, out)
out = rep(root / "sub", "a", "b"); check("a folder instead of a file", "this is a folder" in out, out)
g = put(".git/config", b"[core]\n"); out = rep(g, "core", "x"); check("the service folder .git cannot be edited", "cannot be edited" in out and g.read_bytes() == b"[core]\n", out)
png = put("img.png", b"\x89PNG abc"); out = rep(png, "abc", "x"); check("a binary extension", "cannot be edited" in out and png.read_bytes() == b"\x89PNG abc")
nul = put("data.txt", b"abc\x00def"); out = rep(nul, "abc", "x"); check("binary content (NUL)", "binary" in out and nul.read_bytes() == b"abc\x00def", out)
big = put("big.txt", b"x" * 1_100_000); out = rep(big, "x", "y"); check("too large a file", "too large" in out and big.stat().st_size == 1_100_000, out)
u16 = put("u16.txt", "привет".encode("utf-16")); out = rep(u16, "привет", "пока"); check("UTF-16 is not edited, the file is intact", "UTF-16" in out and u16.read_bytes() == "привет".encode("utf-16"), out)

# ---- encodings and line breaks are kept (Russian text on purpose: cp1251 and non-ASCII UTF-8) ----
bom = put("bom.txt", b"\xef\xbb\xbf" + "Привет, мир\nвторая строка\n".encode("utf-8"))
out = rep(bom, "мир", "свет"); check("UTF-8 with BOM: the BOM is kept", bom.read_bytes() == b"\xef\xbb\xbf" + "Привет, свет\nвторая строка\n".encode("utf-8"), out)
nobom = put("nobom.txt", "Привет, мир\n".encode("utf-8")); rep(nobom, "мир", "свет"); check("UTF-8 without BOM: no BOM added", nobom.read_bytes() == "Привет, свет\n".encode("utf-8"))
cp = put("cp.txt", "Русский текст в cp1251\nстрока два\n".encode("cp1251")); out = rep(cp, "строка два", "строка три")
check("cp1251: the bytes are written in the same encoding", cp.read_bytes() == "Русский текст в cp1251\nстрока три\n".encode("cp1251"), out)
snap = cp.read_bytes(); out = rep(cp, "строка три", "строка 😀")
check("cp1251: a character outside the encoding -> refusal, the file is intact", "cannot store" in out and cp.read_bytes() == snap, out)
crlf = put("crlf.txt", b"one\r\ntwo\r\nthree\r\n"); out = rep(crlf, "two\nthree", "2\n3")
check("CRLF: a multi-line old with \\n finds the match and CRLF is kept", crlf.read_bytes() == b"one\r\n2\r\n3\r\n", (out, crlf.read_bytes()))
out = rep(crlf, "one\r\n2", "ONE\r\n2"); check("CRLF: old already with \\r\\n works too", crlf.read_bytes() == b"ONE\r\n2\r\n3\r\n", (out, crlf.read_bytes()))
mixed = put("mixed.txt", b"a\r\nb\nc\r\n"); out = rep(mixed, "b", "B"); check("mixed line breaks are not normalized", mixed.read_bytes() == b"a\r\nB\nc\r\n", (out, mixed.read_bytes()))
noeol = put("noeol.txt", b"last line"); rep(noeol, "last", "LAST"); check("a file without a final line break stays that way", noeol.read_bytes() == b"LAST line")
multi = put("multi.py", b"def f():\n    return 1\n\n\ndef g():\n    return 2\n"); out = rep(multi, "    return 1\n", "    return 10\n    # changed\n")
check("a multi-line replacement with indentation", multi.read_bytes() == b"def f():\n    return 10\n    # changed\n\n\ndef g():\n    return 2\n", out)
specials = put("sp.txt", b"price: $5 (50%) [x] {y} \\n\n"); rep(specials, "$5 (50%) [x] {y} \\n", "$6"); check("special characters in old/new are literal, not a regular expression", specials.read_bytes() == b"price: $6\n")
check("still no temporary files", leftovers() == [])

# ---- paths ----
rel = put("sub/rel.txt", b"alpha\n"); out = rep("sub/rel.txt", "alpha", "beta"); check("a relative path", rel.read_bytes() == b"beta\n", out)
out = rep(f"{root.name}/sub/rel.txt", "beta", "gamma"); check("a path with the root name", rel.read_bytes() == b"gamma\n", out)

# ---- permissions ----
ro = put("ro.txt", b"keep\n"); os.chmod(ro, stat.S_IREAD)
out = rep(ro, "keep", "lost")
os.chmod(ro, stat.S_IWRITE | stat.S_IREAD)
check("a read-only file: the content is intact", ro.read_bytes() == b"keep\n", out)
print("      result for a read-only file:", out[:120])
check("after a failed write there are no temporary files and no extra backup", leftovers() == [] and baks(ro) == [], baks(ro))

# ---- decode_for_edit directly ----
check("decode_for_edit: UTF-8", decode_for_edit("ё".encode()) == ("ё", "utf-8"))
check("decode_for_edit: BOM", decode_for_edit(b"\xef\xbb\xbfabc") == ("abc", "utf-8-sig"))
check("decode_for_edit: cp1251 without loss", decode_for_edit("ё".encode("cp1251")) == ("ё", "cp1251"))
check("decode_for_edit: byte 0x98 (undefined in cp1251) -> refusal", decode_for_edit(b"abc\x98")[0] is None)
check("decode_for_edit: UTF-16 and NUL -> refusal", decode_for_edit(b"\xff\xfea\x00")[0] is None and decode_for_edit(b"a\x00b")[0] is None)

m.shutdown()
print("ALL OK" if ok_all else "SOME FAILED")
