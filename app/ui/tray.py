from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint
from PySide6.QtGui import QAction, QCursor, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon


class DesktopDancerTray:
    def __init__(
        self,
        on_add_wife: Callable[[], None],
        on_quit: Callable[[], None],
    ):
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("QApplication 尚未初始化")

        icon = QIcon.fromTheme("user-available")
        if icon.isNull():
            icon = QIcon.fromTheme("applications-multimedia")
        if icon.isNull():
            icon = app.style().standardIcon(QStyle.SP_ComputerIcon)

        self._on_add_wife = on_add_wife

        self._tray = QSystemTrayIcon(icon)
        self._tray.setToolTip("Desktop Dancer")

        self._menu = QMenu()

        add_wife_action = QAction("添加一个老婆")
        add_wife_action.triggered.connect(on_add_wife)
        self._menu.addAction(add_wife_action)

        self._menu.addSeparator()

        quit_action = QAction("退出")
        quit_action.triggered.connect(on_quit)
        self._menu.addAction(quit_action)

        self._tray.activated.connect(self._on_tray_activated)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Context,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._menu.popup(QCursor.pos())
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_add_wife()

    def show(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError("系统托盘不可用")
        self._tray.show()
