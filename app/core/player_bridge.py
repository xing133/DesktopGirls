from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.ui.dancer_window import DancerWindow


class PlayerBridge:
    def __init__(self, project_root: Path, dancer_dir: str = "dancer"):
        self._project_root = project_root
        self._dancer_dir = project_root / dancer_dir
        self._window: DancerWindow | None = None

    def start_default_animation(self) -> None:
        if self._window is not None:
            return

        dancer_dir = self._dancer_dir
        if not dancer_dir.is_dir():
            print(f"[player] 找不到角色目录: {dancer_dir}")
            return

        subdirs = sorted(
            d for d in dancer_dir.iterdir()
            if d.is_dir() and (d / "metadata.json").exists()
        )
        if not subdirs:
            print(f"[player] {dancer_dir} 下没有有效角色，跳过启动")
            return

        last_file = dancer_dir / ".last"
        initial_name = None
        if last_file.exists():
            name = last_file.read_text(encoding="utf-8").strip()
            if (dancer_dir / name).is_dir():
                initial_name = name
        if not initial_name:
            initial_name = subdirs[0].name

        try:
            with open(dancer_dir / initial_name / "metadata.json", encoding="utf-8") as f:
                meta = json.load(f)
            w, h = int(meta["width"]), int(meta["height"])
        except Exception:
            w, h = 200, 400

        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry()
        MARGIN = 20
        start_x = avail.x() + avail.width() - w - MARGIN
        start_y = avail.y() + avail.height() - h - MARGIN

        self._window = DancerWindow(
            dancer_dir=dancer_dir,
            initial_name=initial_name,
            start_x=start_x,
            start_y=start_y,
        )
        self._window.show()
        print(f"[player] 启动 '{initial_name}' @ ({start_x}, {start_y})")

    def switch_to_dancer(self, name: str) -> None:
        try:
            (self._dancer_dir / ".last").write_text(name, encoding="utf-8")
        except OSError:
            pass
        if self._window:
            self._window.switch_to(name)
        else:
            self.start_default_animation()

    def stop(self) -> None:
        if self._window:
            w = self._window
            self._window = None
            w.close()
