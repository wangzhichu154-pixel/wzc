"""Launch the native application. Does not start a broker or a browser."""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Vector native research desktop")
    parser.add_argument("--server", default="http://127.0.0.1:8080", help="Local backend URL")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    from PySide6.QtWidgets import QApplication, QMessageBox
    from vector_desktop.client import ApiError, VectorClient
    from vector_desktop.window import MainWindow, apply_palette

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Vector")
    app.setOrganizationName("Vector Research")
    app.setApplicationVersion("1.0.0-rc.5")
    apply_palette(app)
    try:
        client = VectorClient(args.server)
    except (ApiError, ValueError):
        QMessageBox.critical(None, "服务地址无效", "请使用本机服务地址，例如 http://127.0.0.1:8080。")
        return 2
    from vector_desktop.houmen_window import HoumenWindow
    window = HoumenWindow(lambda: MainWindow(client, auto_check=not args.smoke_test))
    window.show()
    if args.smoke_test:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
