"""控制台界面包（单实例保护）。"""
import sys


def main() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from netconsole.ui import single_instance
    from .main_window import MainWindow, make_icon

    first, mutex_handle, show_event = single_instance.acquire()
    if not first:
        app = QApplication(sys.argv)
        QMessageBox.information(None, "网络控制台", "控制台已在运行，已请求其显示主面板。")
        single_instance.release(mutex_handle)
        return

    app = QApplication(sys.argv)
    app.setApplicationName("网络控制台")
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.set_show_event(show_event)
    win.show()
    # mutex_handle 故意不释放：进程存活期保持单实例保护，退出由内核回收。
    sys.exit(app.exec())
