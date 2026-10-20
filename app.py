import sys

from PySide6.QtWidgets import QApplication

from core import db, diag
from ui import themes
from ui.main_window import MainWindow


def main():
    diag.install(db.DB_PATH.parent / "crash.log")
    app = QApplication(sys.argv)
    themes.apply(themes.saved_key(), save=False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
