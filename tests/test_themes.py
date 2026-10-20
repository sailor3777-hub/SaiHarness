"""Темы оформления: файлы QSS, селектор над «Новый чат», применение и сохранение, перекраска чата и панелей."""
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

# ---- набор тем ----
check("три темы: тёмная, светлая, Diablo", list(themes.THEMES) == ["dark", "light", "diablo"]
      and [t.title for t in themes.THEMES.values()] == ["Тёмная", "Светлая", "Diablo"])
fields = ("user assistant system reasoning tool tool_error dim normal warn over code_bg code_fg inline_code link table_head quote").split()
bad = [(t.key, f) for t in themes.THEMES.values() for f in fields if not QColor(getattr(t, f)).isValid()]
check("все цвета тем корректны", not bad, bad)
check("тёмная тема совпадает с прежним theme.qss", "background-color: #000000" in themes.DARK.qss() and themes.DARK.qss_file == "theme.qss")
check("у светлой светлый фон, у Diablo золото и засечный шрифт",
      "#f6f6f4" in themes.LIGHT.qss() and "#d4a840" in themes.DIABLO.qss() and "Palatino" in themes.DIABLO.qss())
check("richtext css содержит цвета темы", themes.DIABLO.code_bg in themes.DIABLO.richtext_css() and themes.LIGHT.link in themes.LIGHT.richtext_css())
check("по умолчанию тёмная", themes.current().key == "dark" and themes.saved_key() == "dark")

# ---- окно: селектор над «Новый чат» ----
w = MainWindow(mcp_config=tmp / "mcp.json", connect_mcp=False)
cl = w.chat_list; lay = cl.layout()
idx = {lay.itemAt(i).widget(): i for i in range(lay.count()) if lay.itemAt(i).widget()}
check("селектор тем стоит над кнопкой «Новый чат»", idx[cl.theme_box] + 1 == idx[cl.new_btn], idx)
check("в селекторе три темы, выбрана текущая", cl.theme_box.count() == 3 and cl.theme_box.currentData() == "dark")

# ---- смена темы ----
w.chat_view._blocks = [
    {"role": "user", "content": "привет", "reasoning": "", "live": False, "images": [], "tool_calls": []},
    {"role": "assistant", "content": "ответ `код`", "reasoning": "думаю", "live": False, "images": [], "tool_calls": []},
]
w.chat_view._render()
check("тёмная: подпись «Вы» синяя", themes.DARK.user in w.chat_view._block_html(w.chat_view._blocks[0]))
for key in ("light", "diablo", "dark"):
    i = cl.theme_box.findData(key)
    cl.theme_box.setCurrentIndex(i); cl.theme_box.activated.emit(i)
    th = themes.THEMES[key]
    check(f"{key}: тема применена", themes.current().key == key)
    check(f"{key}: стиль приложения сменился", app.styleSheet() == th.qss())
    check(f"{key}: сохранена в базе", db.get_setting("theme") == key and themes.saved_key() == key)
    html = w.chat_view._block_html(w.chat_view._blocks[1])
    check(f"{key}: цвета чата из темы", th.assistant in html and th.reasoning in html and th.assistant != "" and
          th.code_bg in w.chat_view.history.document().defaultStyleSheet(), html[:120])
    check(f"{key}: история перерисована цветами темы", th.user.lower() in w.chat_view.history.toHtml().lower(), w.chat_view.history.toHtml()[-300:])

# ---- панели ----
themes.apply("light", save=False)
fp = w.files_panel
fp.set_extra_tokens(100); fp.set_limit((10, True, None))
check("панель файлов: превышение лимита красится цветом темы", themes.LIGHT.over in fp.info.styleSheet(), fp.info.styleSheet())
themes.apply("diablo", save=False)
check("панель файлов: после смены темы перекрашена", themes.DIABLO.over in fp.info.styleSheet(), fp.info.styleSheet())
themes.apply("dark", save=False)

# ---- ошибки разбора QSS ----
parse = [m for m in warnings if "parse" in m.lower() or "unknown property" in m.lower()]
check("Qt не ругается на QSS", not parse, parse[:3])

# ---- неизвестная тема из базы ----
db.set_setting("theme", "no-such-theme")
check("неизвестный ключ из базы даёт тёмную", themes.saved_key() == "dark")

print("ALL OK" if ok_all else "SOME FAILED")
