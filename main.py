#!/usr/bin/env python3
"""ELECMEAS — Electronic transport measurement GUI.

Entry point. Run with:
    python main.py
"""

import sys

from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("ELECMEAS")
    app.setOrganizationName("LabPhys")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
