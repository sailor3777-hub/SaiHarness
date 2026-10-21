"""Text markers: the error prefix, the image_generate result, the new chat title (with earlier wordings)."""
import sys

from core import db, imagegen, markers

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


check("error() gives text with the prefix", markers.error("no file") == markers.ERROR_PREFIX + "no file")
check("is_error recognizes its own prefix", markers.is_error(markers.error("x")))
check("plain text is not an error", not markers.is_error("all good") and not markers.is_error(""))
check("a word without the colon is not an error (a log line starting with ERROR)", not markers.is_error("ERROR in module x"))
check("is_error recognizes the Russian version's prefix (old history)", markers.is_error(markers.LEGACY_ERROR_WORDS[0] + ": x"))
saved_words = markers.LEGACY_ERROR_WORDS
markers.LEGACY_ERROR_WORDS = ("OLD-ERR",)
check("is_error recognizes an earlier prefix", markers.is_error("OLD-ERR: x"))
markers.LEGACY_ERROR_WORDS = saved_words

check("result_path: the current prefix", imagegen.result_path(imagegen.RESULT_PREFIX + r"C:\a.png") == r"C:\a.png")
check("result_path: someone else's text", imagegen.result_path(r"C:\a.png") == "")
check("result_path: the Russian version's prefix", imagegen.result_path(imagegen.LEGACY_RESULT_PREFIXES[0] + r"C:\b.png ") == r"C:\b.png")
saved_prefixes = imagegen.LEGACY_RESULT_PREFIXES
imagegen.LEGACY_RESULT_PREFIXES = ("Old image: ",)
check("result_path: an earlier prefix", imagegen.result_path(r"Old image: C:\b.png ") == r"C:\b.png")
imagegen.LEGACY_RESULT_PREFIXES = saved_prefixes

check("the default title", db.is_default_title(db.DEFAULT_CHAT_TITLE) and not db.is_default_title("hello"))
check("the Russian version's default title (old databases)", db.is_default_title(db.LEGACY_CHAT_TITLES[0]))
saved_titles = db.LEGACY_CHAT_TITLES
db.LEGACY_CHAT_TITLES = ("Old chat",)
check("an earlier default title", db.is_default_title("Old chat"))
db.LEGACY_CHAT_TITLES = saved_titles

print("\nALL OK" if ok_all else "\nSOME FAILED")
sys.exit(0 if ok_all else 1)
