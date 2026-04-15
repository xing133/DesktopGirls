#!/home/gary/test/desktop-dancer/.venv/bin/python
"""
桌面宠物舞者展示

目录结构：
  dancer/
    anna01/   ← 一个角色，内含 frame_*.png + metadata.json
    alyx2/
    ...

用法：
  .venv/bin/python dancer.py
  .venv/bin/python dancer.py --scale 0.8 --sticky
  .venv/bin/python dancer.py --x 100 --y 100

控制：
  左键拖动    移动窗口
  右键        切换角色 / 退出
"""

import argparse
import json
import random
import sys
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, Gdk, GLib

import cairo


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="桌面宠物舞者")
    p.add_argument("--dancer-dir", default="dancer", help="角色根目录（含各子目录）")
    p.add_argument("--scale", default=1.0, type=float, help="显示缩放比例（默认 1.0）")
    p.add_argument("--monitor", default=-1, type=int, help="显示器索引（-1 = 主显示器）")
    p.add_argument("--x", default=None, type=int, help="窗口 X 坐标")
    p.add_argument("--y", default=None, type=int, help="窗口 Y 坐标")
    p.add_argument("--sticky", action="store_true", help="在所有工作区显示（默认仅当前工作区）")
    return p.parse_args()


def get_dancer_subdirs(dancer_dir: Path) -> list[Path]:
    """返回 dancer_dir 下所有有效子目录（含 metadata.json），按名称排序"""
    return sorted(
        d for d in dancer_dir.iterdir()
        if d.is_dir() and (d / "metadata.json").exists()
    )


def read_last(dancer_dir: Path) -> str | None:
    """读取上次使用的角色名，无效则返回 None"""
    last_file = dancer_dir / ".last"
    if last_file.exists():
        name = last_file.read_text().strip()
        if (dancer_dir / name).is_dir():
            return name
    return None


def write_last(dancer_dir: Path, name: str):
    (dancer_dir / ".last").write_text(name)


def load_surfaces_and_meta(subdir: Path) -> tuple:
    """
    加载子目录下所有 PNG 帧和 metadata，返回 (surfaces, fps, width, height)
    出错时抛 ValueError
    """
    png_files = sorted(subdir.glob("frame_*.png"))
    if not png_files:
        raise ValueError(f"'{subdir}' 中没有 frame_*.png 文件")

    meta_path = subdir / "metadata.json"
    if not meta_path.exists():
        raise ValueError(f"找不到 '{meta_path}'")

    with open(meta_path) as f:
        meta = json.load(f)

    total = len(png_files)
    print(f"加载 '{subdir.name}'：{total} 帧...")

    surfaces = []
    for i, path in enumerate(png_files):
        surf = cairo.ImageSurface.create_from_png(str(path))
        surfaces.append(surf)
        if (i + 1) % 50 == 0 or (i + 1) == total:
            print(f"  {i + 1}/{total} ({(i + 1) / total * 100:.0f}%)")

    fps = meta["fps"]
    w = int(meta["width"])
    h = int(meta["height"])
    print(f"完成，{w}×{h}px，{fps:.0f}fps")
    return surfaces, fps, w, h


def load_frames(subdir: Path) -> tuple:
    """
    同步加载子目录下所有 PNG 帧，返回 (surfaces, regions, fps, width, height)
    出错时抛 ValueError
    """
    surfaces, fps, w, h = load_surfaces_and_meta(subdir)
    regions = [Gdk.cairo_region_create_from_surface(surf) for surf in surfaces]
    return surfaces, regions, fps, w, h


def get_monitor_workarea(monitor_index: int):
    display = Gdk.Display.get_default()
    if monitor_index < 0:
        mon = display.get_primary_monitor() or display.get_monitor(0)
    else:
        mon = display.get_monitor(monitor_index)
    wa = mon.get_workarea()
    return wa.x, wa.y, wa.width, wa.height


# ── 主窗口 ────────────────────────────────────────────────────────────────────

class DancerWindow(Gtk.Window):

    def __init__(self, dancer_dir: Path, initial_name: str, scale: float,
                 start_x: int, start_y: int, sticky: bool,
                 preloaded=None):
        """
        preloaded: 可选的 (surfaces, fps, w, h) 元组。
                   若提供则跳过首次 load，节省一次磁盘读取。
        """
        super().__init__()

        self._dancer_dir = dancer_dir
        self._current_name = initial_name
        self._scale = scale
        self._timer_id = None

        # 加载初始角色
        if preloaded is not None:
            surfaces, fps, w, h = preloaded
        else:
            surfaces, fps, w, h = load_surfaces_and_meta(dancer_dir / initial_name)
        self._surfaces = surfaces
        self._regions = [None] * len(surfaces)
        self._frame_idx = 0
        self._n_frames = len(surfaces)
        self._win_w = int(w * scale)
        self._win_h = int(h * scale)
        self._interval_ms = max(16, int(1000 / fps))

        # --- 异步切换状态 ---
        self._is_loading = False
        self._loading_name = None
        self._wanted_name = initial_name
        self._switch_token = 0

        # --- 随机播放状态 ---
        self._random_enabled = False
        self._random_every_loops = 3
        self._loops_since_switch = 0

        # --- 窗口属性 ---
        self.set_title("desktop-dancer")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_app_paintable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)

        if sticky:
            self.stick()

        # --- RGBA 透明 visual ---
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        else:
            print("警告：RGBA visual 不可用，请确认 Cinnamon 合成器已启用")

        # --- 尺寸与位置 ---
        self.set_default_size(self._win_w, self._win_h)
        self.move(start_x, start_y)

        # --- 绘制区域 ---
        self._da = Gtk.DrawingArea()
        self._da.set_size_request(self._win_w, self._win_h)
        self.add(self._da)

        # --- 事件 ---
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self._da.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_button_press)
        self.connect("delete-event", Gtk.main_quit)

        # --- 动画定时器 ---
        self._timer_id = GLib.timeout_add(self._interval_ms, self._on_timer)

    # ── 绘制 ──────────────────────────────────────────────────────────────────

    def _on_draw(self, widget, ctx):
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()

        ctx.set_operator(cairo.OPERATOR_OVER)
        surf = self._surfaces[self._frame_idx]
        sw, sh = surf.get_width(), surf.get_height()
        if sw != self._win_w or sh != self._win_h:
            ctx.scale(self._win_w / sw, self._win_h / sh)
        ctx.set_source_surface(surf, 0, 0)
        ctx.paint()
        return False

    # ── 动画定时器 ────────────────────────────────────────────────────────────

    def _on_timer(self):
        prev_idx = self._frame_idx
        self._frame_idx = (self._frame_idx + 1) % self._n_frames

        # 完整播放一轮（末帧回到 0）
        if prev_idx == self._n_frames - 1 and self._frame_idx == 0:
            self._loops_since_switch += 1
            if (
                self._random_enabled
                and not self._is_loading
                and self._loops_since_switch >= self._random_every_loops
            ):
                next_name = self._pick_random_name()
                self._loops_since_switch = 0
                if next_name:
                    self._request_switch(next_name, source="auto")

        gdk_win = self.get_window()
        if gdk_win:
            gdk_win.input_shape_combine_region(
                self._region_for_frame(self._frame_idx), 0, 0
            )
        self._da.queue_draw()
        return True

    def _region_for_frame(self, idx: int):
        region = self._regions[idx]
        if region is None:
            region = Gdk.cairo_region_create_from_surface(self._surfaces[idx])
            self._regions[idx] = region
        return region


    def _on_button_press(self, widget, event):
        if event.button == 1:
            self.begin_move_drag(
                event.button, int(event.x_root), int(event.y_root), event.time
            )
        elif event.button == 3:
            # 每次重建菜单，反映当前角色状态
            menu = self._build_menu()
            menu.popup_at_pointer(event)
        return True

    # ── 右键菜单 ──────────────────────────────────────────────────────────────

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        # 角色列表
        subdirs = get_dancer_subdirs(self._dancer_dir)
        if not subdirs:
            item = Gtk.MenuItem(label="（无可用角色）")
            item.set_sensitive(False)
            menu.append(item)
        else:
            for d in subdirs:
                name = d.name
                item = Gtk.MenuItem(label=name)
                if name == self._current_name:
                    item.set_sensitive(False)   # 当前角色置灰
                else:
                    item.connect("activate", lambda _, n=name: self.switch_to(n))
                menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        random_item = Gtk.CheckMenuItem(label="随机播放")
        random_item.set_active(self._random_enabled)
        random_item.connect("toggled", self._on_toggle_random)
        menu.append(random_item)

        settings_item = Gtk.MenuItem(label=f"设置随机切换频率（当前：{self._random_every_loops}）")
        settings_item.connect("activate", self._open_random_settings_dialog)
        menu.append(settings_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="退出舞者")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    # ── 切换角色（异步） ───────────────────────────────────────────────────────

    def switch_to(self, name: str):
        self._request_switch(name, source="manual")

    def _request_switch(self, name: str, source: str):
        if name == self._current_name and not self._is_loading:
            return

        self._wanted_name = name
        if source == "manual":
            print(f"\n切换到：{name}")

        if not self._is_loading:
            self._start_async_load(name)

    def _start_async_load(self, name: str):
        self._is_loading = True
        self._loading_name = name
        self._switch_token += 1
        token = self._switch_token

        def worker():
            try:
                surfaces, fps, w, h = load_surfaces_and_meta(self._dancer_dir / name)
                ok = True
                payload = (surfaces, fps, w, h)
            except Exception as e:
                ok = False
                payload = e
            GLib.idle_add(self._on_async_load_done, token, name, ok, payload)

        threading.Thread(target=worker, daemon=True).start()

    def _on_async_load_done(self, token: int, name: str, ok: bool, payload):
        if token != self._switch_token:
            return False

        self._is_loading = False
        self._loading_name = None

        if not ok:
            print(f"切换失败：{payload}")
        elif name == self._wanted_name:
            surfaces, fps, w, h = payload
            self._apply_loaded_role(name, surfaces, fps, w, h)

        if self._wanted_name != self._current_name and not self._is_loading:
            self._start_async_load(self._wanted_name)

        return False

    def _apply_loaded_role(self, name: str, surfaces, fps: float, w: int, h: int):
        regions = [Gdk.cairo_region_create_from_surface(surf) for surf in surfaces]

        # 更新帧数据
        self._surfaces = surfaces
        self._regions = regions
        self._frame_idx = 0
        self._n_frames = len(surfaces)
        self._current_name = name
        self._loops_since_switch = 0

        # 若 fps 变化，重建定时器
        new_interval = max(16, int(1000 / fps))
        if new_interval != self._interval_ms:
            if self._timer_id is not None:
                GLib.source_remove(self._timer_id)
            self._interval_ms = new_interval
            self._timer_id = GLib.timeout_add(self._interval_ms, self._on_timer)

        # 若尺寸变化，调整窗口
        new_w = int(w * self._scale)
        new_h = int(h * self._scale)
        if new_w != self._win_w or new_h != self._win_h:
            self._win_w = new_w
            self._win_h = new_h
            self._da.set_size_request(new_w, new_h)
            self.resize(new_w, new_h)

        # 更新 input shape 并重绘
        gdk_win = self.get_window()
        if gdk_win and self._surfaces:
            gdk_win.input_shape_combine_region(
                self._region_for_frame(0), 0, 0
            )
        self._da.queue_draw()

        # 记住这次选择
        write_last(self._dancer_dir, name)

    def _on_toggle_random(self, item: Gtk.CheckMenuItem):
        self._random_enabled = item.get_active()
        self._loops_since_switch = 0

    def _open_random_settings_dialog(self, _item):
        dialog = Gtk.Dialog(
            title="随机播放设置",
            transient_for=self,
            flags=0,
        )
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        dialog.add_button("确定", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label="每播放多少次后随机切换：")
        spin = Gtk.SpinButton()
        spin.set_range(1, 999)
        spin.set_increments(1, 5)
        spin.set_value(self._random_every_loops)
        box.pack_start(label, False, False, 0)
        box.pack_start(spin, False, False, 0)
        content.add(box)
        dialog.show_all()

        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            self._random_every_loops = int(spin.get_value())
            self._loops_since_switch = 0
        dialog.destroy()

    def _pick_random_name(self) -> str | None:
        names = [d.name for d in get_dancer_subdirs(self._dancer_dir)]
        candidates = [n for n in names if n != self._current_name]
        if not candidates:
            return None
        return random.choice(candidates)

    # ── realize 后初始化 input shape ─────────────────────────────────────────

    def do_realize(self):
        Gtk.Window.do_realize(self)
        gdk_win = self.get_window()
        if gdk_win and self._surfaces:
            gdk_win.input_shape_combine_region(
                self._region_for_frame(0), 0, 0
            )


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    dancer_dir = Path(args.dancer_dir)

    if not dancer_dir.is_dir():
        sys.exit(f"错误：找不到角色根目录 '{dancer_dir}'，请先创建并放入角色子目录")

    subdirs = get_dancer_subdirs(dancer_dir)
    if not subdirs:
        sys.exit(f"错误：'{dancer_dir}' 下没有有效的角色子目录（需含 metadata.json）")

    # 决定初始角色
    last = read_last(dancer_dir)
    initial_name = last if last else subdirs[0].name
    print(f"初始角色：{initial_name}{'（上次）' if last else '（首个）'}")

    # 默认位置：主显示器右下角
    mon_x, mon_y, mon_w, mon_h = get_monitor_workarea(args.monitor)
    MARGIN = 20

    # 读取初始尺寸以计算位置（只读 metadata，不加载帧）
    meta_path = dancer_dir / initial_name / "metadata.json"
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        fw, fh = int(meta["width"]), int(meta["height"])
        fps_init = meta["fps"]
    except (FileNotFoundError, KeyError) as e:
        sys.exit(f"错误：读取 metadata 失败 ({meta_path}): {e}")

    win_w = int(fw * args.scale)
    win_h = int(fh * args.scale)
    start_x = args.x if args.x is not None else (mon_x + mon_w - win_w - MARGIN)
    start_y = args.y if args.y is not None else (mon_y + mon_h - win_h - MARGIN)

    print(f"位置：({start_x}, {start_y})，{win_w}×{win_h}px")

    # 加载初始角色帧（仅一次）
    try:
        preloaded = load_surfaces_and_meta(dancer_dir / initial_name)
    except ValueError as e:
        sys.exit(f"错误：{e}")

    win = DancerWindow(
        dancer_dir=dancer_dir,
        initial_name=initial_name,
        scale=args.scale,
        start_x=start_x,
        start_y=start_y,
        sticky=args.sticky,
        preloaded=preloaded,
    )
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
