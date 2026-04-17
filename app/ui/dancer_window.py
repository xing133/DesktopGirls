from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QMenu, QWidget


class _LoadThread(QThread):
    done = Signal(int, str, list, float, int, int)  # token, name, images, fps, w, h
    err = Signal(int, str, str)                      # token, name, msg

    def __init__(self, token: int, subdir: Path, name: str, parent=None):
        super().__init__(parent)
        self._token = token
        self._subdir = subdir
        self._name = name

    def run(self) -> None:
        try:
            png_files = sorted(self._subdir.glob("frame_*.png"))
            if not png_files:
                raise ValueError(f"no frame_*.png in {self._subdir}")
            with open(self._subdir / "metadata.json", encoding="utf-8") as f:
                meta = json.load(f)
            images = [QImage(str(p)) for p in png_files]
            self.done.emit(
                self._token, self._name, images,
                float(meta["fps"]), int(meta["width"]), int(meta["height"]),
            )
        except Exception as e:
            self.err.emit(self._token, self._name, str(e))


class DancerWindow(QWidget):
    def __init__(
        self,
        dancer_dir: Path,
        initial_name: str,
        scale: float = 1.0,
        start_x: int = 100,
        start_y: int = 100,
    ):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._dancer_dir = dancer_dir
        self._scale = scale

        self._pixmaps: list[QPixmap] = []
        self._regions: list[QRegion | None] = []
        self._frame_idx = 0
        self._n_frames = 0

        self._current_name = ""
        self._wanted_name = initial_name
        self._is_loading = False
        self._switch_token = 0
        self._load_thread: _LoadThread | None = None

        self._random_enabled = False
        self._random_every_loops = 3
        self._loops_since_switch = 0

        self._drag_pos = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self.move(start_x, start_y)
        self._begin_load(initial_name)

    # ── window events ─────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "win32":
            self._remove_win32_shadow()

    def _remove_win32_shadow(self) -> None:
        import ctypes
        DWMWA_NCRENDERING_POLICY = 2
        DWMNCRP_DISABLED = 1
        hwnd = int(self.winId())
        val = ctypes.c_int(DWMNCRP_DISABLED)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_NCRENDERING_POLICY,
            ctypes.byref(val), ctypes.sizeof(val),
        )

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        if not self._pixmaps:
            return
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawPixmap(0, 0, self._pixmaps[self._frame_idx])

    # ── animation timer ───────────────────────────────────────────────────────

    def _tick(self) -> None:
        prev = self._frame_idx
        self._frame_idx = (self._frame_idx + 1) % self._n_frames

        if prev == self._n_frames - 1 and self._frame_idx == 0:
            self._loops_since_switch += 1
            if (
                self._random_enabled
                and not self._is_loading
                and self._loops_since_switch >= self._random_every_loops
            ):
                nxt = self._pick_random_name()
                if nxt:
                    self._loops_since_switch = 0
                    self._request_switch(nxt)

        self.setMask(self._mask_for(self._frame_idx))
        self.update()

    def _mask_for(self, idx: int) -> QRegion:
        r = self._regions[idx]
        if r is None:
            bmp = self._pixmaps[idx].mask()
            r = QRegion(bmp) if not bmp.isNull() else QRegion(self.rect())
            self._regions[idx] = r
        return r

    # ── mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None

    # ── context menu ──────────────────────────────────────────────────────────

    def _show_menu(self, pos) -> None:
        menu = QMenu()
        names = self._dancer_names()
        if not names:
            menu.addAction("（无可用角色）").setEnabled(False)
        else:
            for name in names:
                act = menu.addAction(name)
                if name == self._current_name:
                    act.setEnabled(False)
                else:
                    act.triggered.connect(lambda _checked, n=name: self._request_switch(n))
        menu.addSeparator()
        random_act = menu.addAction("随机播放")
        random_act.setCheckable(True)
        random_act.setChecked(self._random_enabled)
        random_act.triggered.connect(lambda checked: setattr(self, "_random_enabled", checked))
        menu.addSeparator()
        menu.addAction("退出舞者").triggered.connect(QApplication.quit)
        menu.exec(pos)

    # ── public API ────────────────────────────────────────────────────────────

    def switch_to(self, name: str) -> None:
        self._request_switch(name)

    # ── async load ────────────────────────────────────────────────────────────

    def _request_switch(self, name: str) -> None:
        self._wanted_name = name
        if not self._is_loading:
            self._begin_load(name)

    def _begin_load(self, name: str) -> None:
        self._is_loading = True
        self._switch_token += 1
        token = self._switch_token

        thread = _LoadThread(token, self._dancer_dir / name, name, parent=self)
        thread.done.connect(self._on_load_done)
        thread.err.connect(self._on_load_err)
        thread.finished.connect(thread.deleteLater)
        self._load_thread = thread
        thread.start()

    def _on_load_done(self, token: int, name: str, images: list, fps: float, w: int, h: int) -> None:
        if token != self._switch_token:
            return
        self._is_loading = False

        pw, ph = int(w * self._scale), int(h * self._scale)
        pixmaps = [
            QPixmap.fromImage(img).scaled(
                pw, ph,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            for img in images
        ]

        self._pixmaps = pixmaps
        self._regions = [None] * len(pixmaps)
        self._frame_idx = 0
        self._n_frames = len(pixmaps)
        self._current_name = name
        self._loops_since_switch = 0

        self.setFixedSize(pw, ph)
        self.setMask(self._mask_for(0))

        interval = max(16, int(1000 / fps))
        self._timer.setInterval(interval)
        if not self._timer.isActive():
            self._timer.start()

        self.update()
        self._write_last(name)

        if self._wanted_name != name:
            self._begin_load(self._wanted_name)

    def _on_load_err(self, token: int, name: str, msg: str) -> None:
        if token != self._switch_token:
            return
        self._is_loading = False
        print(f"[dancer] 加载失败 '{name}': {msg}")
        if self._wanted_name != name:
            self._begin_load(self._wanted_name)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _dancer_names(self) -> list[str]:
        if not self._dancer_dir.is_dir():
            return []
        return sorted(
            d.name for d in self._dancer_dir.iterdir()
            if d.is_dir() and (d / "metadata.json").exists()
        )

    def _pick_random_name(self) -> str | None:
        candidates = [n for n in self._dancer_names() if n != self._current_name]
        return random.choice(candidates) if candidates else None

    def _write_last(self, name: str) -> None:
        try:
            (self._dancer_dir / ".last").write_text(name, encoding="utf-8")
        except OSError:
            pass
