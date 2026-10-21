"""Themes: QSS files, the selector above "New chat", applying and saving, recoloring the chat and panels."""
import sys, pathlib, tempfile
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from core import db

tmp = pathlib.Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "themes.db"
from ui import themes
from ui.main_window import MainWindow

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

warnings = []
qInstallMessageHandler(lambda mode, ctx, msg: warnings.append(msg))
app = QApplication([])

# ---- the set of themes ----
check("three themes: dark, light, Diablo", list(themes.THEMES) == ["dark", "light", "diablo"]
      and [t.title for t in themes.THEMES.values()] == ["Dark", "Light", "Diablo"])
fields = ("user assistant system reasoning tool tool_error dim normal warn over code_bg code_fg inline_code link table_head quote").split()
bad = [(t.key, f) for t in themes.THEMES.values() for f in fields if not QColor(getattr(t, f)).isValid()]
check("all theme colors are valid", not bad, bad)
check("the dark theme matches the old theme.qss", "background-color: #000000" in themes.DARK.qss() and themes.DARK.qss_file == "theme.qss")
check("light has a light background, Diablo has gold and a serif font",
      "#f6f6f4" in themes.LIGHT.qss() and "#d4a840" in themes.DIABLO.qss() and "Palatino" in themes.DIABLO.qss())
check("the richtext css has the theme colors", themes.DIABLO.code_bg in themes.DIABLO.richtext_css() and themes.LIGHT.link in themes.LIGHT.richtext_css())
check("dark by default", themes.current().key == "dark" and themes.saved_key() == "dark")

# ---- window: the selector above "New chat" ----
w = MainWindow(mcp_config=tmp / "mcp.json", connect_mcp=False)
cl = w.chat_list; lay = cl.layout()
idx = {lay.itemAt(i).widget(): i for i in range(lay.count()) if lay.itemAt(i).widget()}
check("the theme selector is above the \"New chat\" button", idx[cl.theme_box] + 1 == idx[cl.new_btn], idx)
check("the selector has three themes, the current one is selected", cl.theme_box.count() == 3 and cl.theme_box.currentData() == "dark")

# ---- changing the theme ----
w.chat_view._blocks = [
    {"role": "user", "content": "hello", "reasoning": "", "live": False, "images": [], "tool_calls": []},
    {"role": "assistant", "content": "answer `code`", "reasoning": "thinking", "live": False, "images": [], "tool_calls": []},
]
w.chat_view._render()
check("dark: the \"You\" label is blue", themes.DARK.user in w.chat_view._block_html(w.chat_view._blocks[0]))
for key in ("light", "diablo", "dark"):
    i = cl.theme_box.findData(key)
    cl.theme_box.setCurrentIndex(i); cl.theme_box.activated.emit(i)
    th = themes.THEMES[key]
    check(f"{key}: the theme is applied", themes.current().key == key)
    check(f"{key}: the app style changed", app.styleSheet() == th.qss())
    check(f"{key}: saved in the database", db.get_setting("theme") == key and themes.saved_key() == key)
    html = w.chat_view._block_html(w.chat_view._blocks[1])
    check(f"{key}: chat colors from the theme", th.assistant in html and th.reasoning in html and th.assistant != "" and
          th.code_bg in w.chat_view.history.document().defaultStyleSheet(), html[:120])
    check(f"{key}: the history is redrawn in the theme colors", th.user.lower() in w.chat_view.history.toHtml().lower(), w.chat_view.history.toHtml()[-300:])

# ---- panels ----
themes.apply("light", save=False)
fp = w.files_panel
fp.set_extra_tokens(100); fp.set_limit((10, True, None))
check("files panel: exceeding the limit uses the theme color", themes.LIGHT.over in fp.info.styleSheet(), fp.info.styleSheet())
themes.apply("diablo", save=False)
check("files panel: recolored after the theme change", themes.DIABLO.over in fp.info.styleSheet(), fp.info.styleSheet())
themes.apply("dark", save=False)

# ---- QSS parse errors ----
parse = [m for m in warnings if "parse" in m.lower() or "unknown property" in m.lower()]
check("Qt does not complain about the QSS", not parse, parse[:3])

# ---- an unknown theme from the database ----
db.set_setting("theme", "no-such-theme")
check("an unknown key from the database gives dark", themes.saved_key() == "dark")

print("ALL OK" if ok_all else "SOME FAILED")
