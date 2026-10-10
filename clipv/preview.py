"""图片悬停预览：鼠标停留在缩略图上时弹出稍大的浮动预览卡。

交互约定：
- 悬停 250ms 后弹出（快速扫过不打扰），移开立即隐藏；
- override-redirect 窗口（tooltip 语义）：WM 不管理、不抢焦点、不进任务栏，
  也不会触发面板的 focus-out 自动隐藏；
- 视觉与面板一致：深色圆角卡片 + 微阴影（RGBA 合成器下），
  图片按 fit 框等比缩放，不放大超过原始尺寸；最近 8 张做 Pixbuf 缓存。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import cairo

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

log = logging.getLogger("clipv.preview")

HOVER_DELAY_MS = 250      # 悬停多久后弹出
PREVIEW_MAX_W = 480       # 预览最大宽（等比缩放）
PREVIEW_MAX_H = 360       # 预览最大高
_POS_OFFSET = 16          # 预览与鼠标之间的间距
_SHADOW = 6               # 阴影留白
_PAD = 8                  # 图片与卡片边缘的间距
_RADIUS = 10              # 卡片圆角
_CACHE_LIMIT = 8          # 预览图缓存数量上限
_CARD_BG = "#1e2027"      # 与面板深色卡片一致
_CARD_BORDER = "#33363f"


def _hex_rgb(value: str) -> Tuple[float, float, float]:
    v = value.lstrip("#")[:6]
    return (int(v[0:2], 16) / 255.0, int(v[2:4], 16) / 255.0,
            int(v[4:6], 16) / 255.0)


def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5707963267948966, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5707963267948966)
    cr.arc(x + r, y + h - r, r, 1.5707963267948966, 3.141592653589793)
    cr.arc(x + r, y + r, r, 3.141592653589793, 4.71238898038469)
    cr.close_path()


def compute_position(mx: int, my: int, w: int, h: int,
                     workarea: Tuple[int, int, int, int],
                     offset: int = _POS_OFFSET) -> Tuple[int, int]:
    """预览定位：鼠标右下方偏移；越界翻转到左上；最终夹在 workarea 内。"""
    wa_x, wa_y, wa_w, wa_h = workarea
    x = mx + offset
    if x + w > wa_x + wa_w:
        x = mx - w - offset
    x = max(wa_x, min(x, wa_x + wa_w - w))
    y = my + offset
    if y + h > wa_y + wa_h:
        y = my - h - offset
    y = max(wa_y, min(y, wa_y + wa_h - h))
    return x, y


class ImagePreview:
    """图片悬停预览的浮动窗口（面板持有一个实例，服务所有缩略图）。"""

    def __init__(self) -> None:
        self._win: Optional[Gtk.Window] = None
        self._image: Optional[Gtk.Image] = None
        self._timer_id = 0
        self._pending: Optional[str] = None
        self._cache: Dict[str, GdkPixbuf.Pixbuf] = {}
        self._cache_order = []

    # ------------------------------------------------------ 触发接口
    def schedule(self, path: str) -> None:
        """悬停进入缩略图：延迟弹出（期间移开会被 cancel）。"""
        self.cancel()
        self._pending = path
        self._timer_id = GLib.timeout_add(HOVER_DELAY_MS, self._on_delay_done)

    def cancel(self) -> None:
        """取消待弹出的延迟计时（不影响已显示的预览）。"""
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        self._pending = None

    def hide(self) -> None:
        """取消待弹出并立即隐藏（移开 / 面板隐藏 / 列表重建 / 滚动）。"""
        self.cancel()
        if self._win is not None and self._win.get_visible():
            self._win.hide()

    def _on_delay_done(self) -> bool:
        self._timer_id = 0
        path, self._pending = self._pending, None
        if path:
            self.show_for(path)
        return False

    # ------------------------------------------------------ 显示
    def show_for(self, path: str) -> bool:
        """立即显示指定图片的预览；加载失败返回 False（不弹窗）。"""
        pixbuf = self._load(path)
        if pixbuf is None:
            return False
        win = self._ensure_window()
        self._image.set_from_pixbuf(pixbuf)
        iw, ih = pixbuf.get_width(), pixbuf.get_height()
        win.resize(iw + 2 * (_SHADOW + _PAD), ih + 2 * (_SHADOW + _PAD))
        win.show_all()                      # 置为最上层显示
        x, y = self._position(iw, ih)
        win.move(x, y)
        return True

    def _load(self, path: str) -> Optional[GdkPixbuf.Pixbuf]:
        """加载并按 fit 框缩放（不放大）；带 FIFO 缓存。"""
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        try:
            info = GdkPixbuf.Pixbuf.get_file_info(path)
            if info is None or info[1] <= 0 or info[2] <= 0:
                return None
            _fmt, width, height = info
            scale = min(PREVIEW_MAX_W / width, PREVIEW_MAX_H / height, 1.0)
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, max(1, round(width * scale)),
                max(1, round(height * scale)), True)
        except Exception as exc:            # 文件缺失 / 解码失败
            log.debug("预览加载失败 %s：%s", path, exc)
            return None
        self._cache[path] = pixbuf
        self._cache_order.append(path)
        while len(self._cache_order) > _CACHE_LIMIT:
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)
        return pixbuf

    def _position(self, iw: int, ih: int) -> Tuple[int, int]:
        w = iw + 2 * (_SHADOW + _PAD)
        h = ih + 2 * (_SHADOW + _PAD)
        try:
            display = Gdk.Display.get_default()
            seat = display.get_default_seat()
            pointer = seat.get_pointer() if seat is not None else None
            if pointer is None:
                return (0, 0)
            _screen, mx, my = pointer.get_position()
            monitor = display.get_monitor_at_point(mx, my)
            if monitor is None:
                return (0, 0)
            geo = monitor.get_workarea()
            return compute_position(
                mx, my, w, h, (geo.x, geo.y, geo.width, geo.height))
        except Exception:
            log.debug("预览定位失败", exc_info=True)
            return (0, 0)

    # ------------------------------------------------------ 窗口
    def _ensure_window(self) -> Gtk.Window:
        if self._win is not None:
            return self._win
        # POPUP 类型即 override-redirect：WM 完全不介入（tooltip 语义）
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_title("ClipV 预览")
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
        win.set_app_paintable(True)
        screen = win.get_screen()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None:
                win.set_visual(visual)
        win.connect("draw", self._on_draw)
        image = Gtk.Image()
        pad = _SHADOW + _PAD
        image.set_margin_top(pad)
        image.set_margin_bottom(pad)
        image.set_margin_start(pad)
        image.set_margin_end(pad)
        win.add(image)
        self._win = win
        self._image = image
        return win

    def _on_draw(self, widget, cr) -> bool:
        """手绘透明底 + 阴影 + 圆角深色卡片（与面板同一视觉语言）。"""
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if w <= 2 * _SHADOW or h <= 2 * _SHADOW:
            return False
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.restore()
        for d in range(1, _SHADOW + 1):
            alpha = 0.12 * (1.0 - (d - 1) / _SHADOW) ** 2.0
            if alpha <= 0.004:
                continue
            cr.set_source_rgba(0, 0, 0, alpha)
            _rounded_rect(cr, _SHADOW - d, _SHADOW - d,
                          w - 2 * (_SHADOW - d), h - 2 * (_SHADOW - d),
                          _RADIUS + d * 0.6)
            cr.fill()
        r, g, b = _hex_rgb(_CARD_BG)
        cr.set_source_rgba(r, g, b, 1.0)
        _rounded_rect(cr, _SHADOW, _SHADOW,
                      w - 2 * _SHADOW, h - 2 * _SHADOW, _RADIUS)
        cr.fill_preserve()
        r, g, b = _hex_rgb(_CARD_BORDER)
        cr.set_source_rgba(r, g, b, 1.0)
        cr.set_line_width(1.0)
        cr.stroke()
        return False
