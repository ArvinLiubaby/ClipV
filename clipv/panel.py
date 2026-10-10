"""历史面板：GTK3 无边框置顶悬浮窗 + 搜索 + 列表（键盘/鼠标/高亮/主题）。

实现要点：
- Gtk.Window（UTILITY 类型、无装饰、置顶、跳过任务栏）+ RGBA 视觉，
  阴影与圆角卡片由窗口 draw 信号手绘（cairo）；
- 搜索框为 Gtk.SearchEntry，列表为 Gtk.ListBox（行内为普通 GTK 部件）；
- 主题深/浅色由 CSS Provider 实现，色板与旧版保持一致；
- 每次显示前显式 set 位置，规避 muffin 窗口重映射上移缺陷。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cairo

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

from . import config as cfg_mod
from . import preview as preview_mod
from .images import load_pixbuf, png_size
from .store import Entry, Store, search_text
from .util import preview_line, relative_time

log = logging.getLogger("clipv.panel")

PANEL_WIDTH = 690   # 460 × 1.5（可视面积放大）
PANEL_HEIGHT = 780  # 520 × 1.5
ITEM_HEIGHT = 64
THUMB_SIZE = 176     # 图片行缩略图长边（约为文本行高的 3 倍，一眼可辨内容）
IMAGE_ROW_HEIGHT = THUMB_SIZE + 14   # 缩略图 + 行内上下留白（7+7）
MARGIN = 14          # 窗口四周阴影留白
RADIUS = 12          # 卡片圆角

_FOOTER_HINT = "↑↓ 选择 · Enter 粘贴 · Ctrl+P 固定 · Del 删除 · Esc 关闭"
_CONFIRM_HINT = "再次按 Ctrl+Shift+Delete 确认清空非固定记录…"
_HIGHLIGHT_BG = "#ffc400"   # 搜索命中高亮底色（与原实现一致）


# ================================================================ 主题

@dataclass(frozen=True)
class Palette:
    card_bg: str
    border: str
    text: str
    muted: str
    hover: str
    selected_bg: str
    accent: str
    accent_soft: str
    input_bg: str
    divider: str
    danger: str

_DARK = Palette(
    card_bg="#1e2027", border="#33363f", text="#e9eaee", muted="#8f939e",
    hover="#282b34", selected_bg="#2d3547", accent="#4c8dff",
    accent_soft="rgba(76, 141, 255, 0.24)", input_bg="#26282f",
    divider="#2c2f38", danger="#ff6b6b")
_LIGHT = Palette(
    card_bg="#f8f8fa", border="#d9dae0", text="#1d1e22", muted="#70747e",
    hover="#edeef2", selected_bg="#dde8fd", accent="#2563eb",
    accent_soft="rgba(37, 99, 235, 0.17)", input_bg="#ffffff",
    divider="#e6e7ec", danger="#d64545")


def resolve_theme(setting: str) -> str:
    """auto 时跟随系统（GTK prefer-dark 标志 / 主题名含 dark）。"""
    if setting in ("dark", "light"):
        return setting
    try:
        settings = Gtk.Settings.get_default()
        if settings is not None:
            if settings.get_property("gtk-application-prefer-dark-theme"):
                return "dark"
            name = str(settings.get_property("gtk-theme-name") or "")
            if "dark" in name.lower():
                return "dark"
            if name:
                return "light"
    except Exception:  # 无显示环境等
        pass
    return "light"


def palette_for(theme: str) -> Palette:
    return _DARK if theme == "dark" else _LIGHT


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


# ================================================================ 行数据

@dataclass
class RowData:
    entry: Entry
    preview: str
    last_pinned: bool = False


# ================================================================ 面板

class ClipvPanel(Gtk.Window):
    """历史面板。on_activate(entry, want_paste) 由守护进程接手后续写回/粘贴。"""

    def __init__(self, store: Store, config: cfg_mod.Config, on_activate=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.store = store
        self.config = config
        self._on_activate = on_activate
        self.theme = resolve_theme(config.get("ui.theme"))
        self.palette_ = palette_for(self.theme)
        self._shown_at = 0.0
        self._menu_open = False
        self._menu: Optional[Gtk.Menu] = None
        self._clear_confirm_until = 0.0
        self._footer_source = 0
        self._entries: List[Entry] = []
        self._provider: Optional[Gtk.CssProvider] = None
        self._thumb_cache: dict = {}
        self._preview = preview_mod.ImagePreview()

        self.set_title("ClipV")
        self.get_style_context().add_class("clipv-window")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(PANEL_WIDTH, PANEL_HEIGHT)
        self.set_app_paintable(True)
        screen = self.get_screen()
        if screen is not None:
            visual = screen.get_rgba_visual()
            if visual is not None:
                self.set_visual(visual)
        self.connect("draw", self._on_draw)
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key_press)
        self.connect("delete-event", self._on_delete_event)

        self._build_ui()
        self._apply_theme()

    # ------------------------------------------------------------ UI 搭建
    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        self._outer = outer

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.set_margin_top(MARGIN + 12)
        card.set_margin_bottom(MARGIN + 8)
        card.set_margin_start(MARGIN + 12)
        card.set_margin_end(MARGIN + 12)
        outer.pack_start(card, True, True, 0)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("搜索剪贴板历史…")
        self.search.get_style_context().add_class("clipv-search")
        self.search.connect("changed", self._on_search_changed)
        self.search.connect("key-press-event", self._on_entry_key)
        card.pack_start(self.search, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        card.pack_start(self.stack, True, True, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_name("clipv-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.connect("button-press-event", self._on_list_button)
        self.listbox.connect("row-activated", self._on_row_activated)
        self.scroll.add(self.listbox)
        # 滚动时隐藏悬停预览（缩略图位置变化，避免预览滞留）
        self.scroll.get_vadjustment().connect("value-changed",
                                              self._on_list_scroll)
        self.stack.add_named(self.scroll, "list")

        self.empty_label = Gtk.Label(label="")
        self.empty_label.get_style_context().add_class("clipv-empty")
        self.empty_label.set_valign(Gtk.Align.CENTER)
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.stack.add_named(self.empty_label, "empty")

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        footer.set_margin_start(2)
        footer.set_margin_end(2)
        self.hint_label = Gtk.Label(label=_FOOTER_HINT)
        self.hint_label.set_xalign(0)
        self.hint_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.hint_label.get_style_context().add_class("clipv-hint")
        self.count_label = Gtk.Label(label="")
        self.count_label.get_style_context().add_class("clipv-count")
        footer.pack_start(self.hint_label, True, True, 0)
        footer.pack_end(self.count_label, False, False, 0)
        card.pack_start(footer, False, False, 0)

        # 只显示子部件（窗口本身保持隐藏，show_at_cursor 时才显示）
        outer.show_all()

    # ------------------------------------------------------------ 主题
    def _css(self) -> str:
        p = self.palette_
        return f"""
        window.clipv-window {{ background: transparent; }}
        entry.clipv-search {{
            background: {p.input_bg};
            color: {p.text};
            border: 1px solid {p.border};
            border-radius: 8px;
            padding: 7px 10px;
            box-shadow: none;
        }}
        entry.clipv-search:focus {{ border-color: {p.accent}; }}
        #clipv-list {{ background: transparent; }}
        #clipv-list row {{
            margin: 1px 6px;
            padding: 0;
            border-radius: 8px;
            border-left-width: 3px;
            border-left-style: solid;
            border-left-color: transparent;
        }}
        #clipv-list row:hover {{ background: {p.hover}; }}
        #clipv-list row:selected {{
            background: {p.selected_bg};
            border-left-color: {p.accent};
        }}
        #clipv-list row.clipv-divider {{
            border-bottom-style: solid;
            border-bottom-width: 1px;
            border-bottom-color: {p.divider};
        }}
        .clipv-thumb {{ background: transparent; }}
        label.clipv-preview {{ color: {p.text}; }}
        label.clipv-meta {{ color: {p.muted}; font-size: 11px; }}
        label.clipv-hint {{ color: {p.muted}; font-size: 18px; }}
        label.clipv-hint.clipv-toast {{ color: {p.accent}; }}
        label.clipv-hint.clipv-danger {{ color: {p.danger}; }}
        label.clipv-count {{ color: {p.muted}; font-size: 18px; }}
        label.clipv-empty {{ color: {p.muted}; font-size: 13px; }}
        button.clipv-pin, button.clipv-del {{
            background: transparent;
            border: none;
            box-shadow: none;
            min-width: 22px;
            min-height: 22px;
            padding: 0;
            margin: 0;
            color: {p.muted};
        }}
        button.clipv-pin:hover {{ color: {p.text}; background: {p.hover}; }}
        button.clipv-pin.clipv-pinned {{ color: {p.accent}; background: {p.accent_soft}; }}
        button.clipv-del:hover {{ color: {p.danger}; background: {p.hover}; }}
        """

    def _apply_theme(self) -> None:
        screen = self.get_screen()
        if screen is None:
            return
        if self._provider is None:
            self._provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_screen(
                screen, self._provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._provider.load_from_data(self._css().encode("utf-8"))
        self.queue_draw()

    def refresh_theme(self) -> None:
        self.theme = resolve_theme(self.config.get("ui.theme"))
        self.palette_ = palette_for(self.theme)
        self._apply_theme()

    # ------------------------------------------------------------ 手绘背景
    def _on_draw(self, widget, cr) -> bool:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if w <= 2 * MARGIN or h <= 2 * MARGIN:
            return False
        # 透明底（RGBA 窗口）
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.restore()
        # 阴影：由外向内逐圈加深
        for d in range(1, MARGIN + 1):
            alpha = 0.10 * (1.0 - (d - 1) / MARGIN) ** 2.2
            if alpha <= 0.002:
                continue
            cr.set_source_rgba(0, 0, 0, alpha)
            _rounded_rect(cr, MARGIN - d, MARGIN - d,
                          w - 2 * (MARGIN - d), h - 2 * (MARGIN - d),
                          RADIUS + d * 0.8)
            cr.fill()
        # 卡片底与边框
        r, g, b = _hex_rgb(self.palette_.card_bg)
        cr.set_source_rgba(r, g, b, 1.0)
        _rounded_rect(cr, MARGIN, MARGIN, w - 2 * MARGIN, h - 2 * MARGIN, RADIUS)
        cr.fill_preserve()
        r, g, b = _hex_rgb(self.palette_.border)
        cr.set_source_rgba(r, g, b, 1.0)
        cr.set_line_width(1.0)
        cr.stroke()
        return False

    def _on_delete_event(self, *_args) -> bool:
        return True  # 拦截 WM 关闭请求，仅隐藏

    # ------------------------------------------------------------ 数据
    def reload(self, preserve_selection: bool = True) -> None:
        """从存储重新加载并按当前搜索过滤。"""
        current_id = self._current_entry_id() if preserve_selection else None
        self._entries = self.store.list()
        self._rebuild_filtered(current_id)

    def _current_entry_id(self) -> Optional[int]:
        row = self.listbox.get_selected_row()
        data = getattr(row, "clipv_data", None) if row is not None else None
        return data.entry.id if data is not None else None

    def _rebuild_filtered(self, preserve_id: Optional[int] = None) -> None:
        query = self.search.get_text().strip()
        folded = query.casefold()
        filtered = [e for e in self._entries
                    if not folded or folded in search_text(e)]
        # 修剪缩略图缓存：只保留仍在列表中的图片
        if self._thumb_cache:
            valid = {e.content for e in filtered if e.kind == "image"}
            for key in list(self._thumb_cache):
                if key not in valid:
                    del self._thumb_cache[key]
        pinned_count = sum(1 for e in filtered if e.pinned)
        self._preview.hide()    # 重建行会销毁缩略图，预览同步隐藏
        for row in self.listbox.get_children():
            self.listbox.remove(row)
        target_row: Optional[Gtk.ListBoxRow] = None
        for i, entry in enumerate(filtered):
            preview = (self._image_preview(entry) if entry.kind == "image"
                       else preview_line(entry.content))
            data = RowData(
                entry=entry,
                preview=preview,
                last_pinned=(i == pinned_count - 1 and i < len(filtered) - 1))
            row = self._build_row(data, query)
            self.listbox.add(row)
            row.show_all()
            row.clipv_delete.hide()  # 删除按钮默认隐藏，悬停时显示
            if entry.id == preserve_id:
                target_row = row
        if filtered:
            if target_row is None:
                target_row = self.listbox.get_row_at_index(0)
            self.listbox.select_row(target_row)
        self.stack.set_visible_child_name("list" if filtered else "empty")
        self.empty_label.set_text(
            "没有匹配的结果" if self._entries
            else "还没有历史记录，复制点什么吧")
        self.count_label.set_text(f"{len(filtered)} 条")

    @staticmethod
    def _image_preview(entry: Entry) -> str:
        size = png_size(entry.content)
        return f"[图片] {size[0]}×{size[1]}" if size else "[图片]"

    def _thumb_pixbuf(self, path: str):
        """行内缩略图（带缓存；加载失败缓存 None 避免反复尝试）。"""
        if path not in self._thumb_cache:
            self._thumb_cache[path] = load_pixbuf(path, max_edge=THUMB_SIZE)
        return self._thumb_cache[path]

    def _build_row(self, data: RowData, query: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        # 图片行更高：缩略图放大后文本区垂直居中
        row.set_size_request(
            -1, IMAGE_ROW_HEIGHT if data.entry.kind == "image" else ITEM_HEIGHT)
        row.clipv_data = data
        tip = data.entry.content
        row.set_tooltip_text(tip if len(tip) <= 800 else tip[:800] + "\n…")
        if data.last_pinned and not data.entry.pinned:
            row.get_style_context().add_class("clipv-divider")

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.set_margin_start(13)   # 16 - 3（选中左条占位）
        hbox.set_margin_end(10)
        hbox.set_margin_top(7)
        hbox.set_margin_bottom(7)
        row.add(hbox)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_valign(Gtk.Align.CENTER)
        preview = Gtk.Label()
        preview.set_markup(self._preview_markup(data.preview, query))
        preview.set_xalign(0)
        preview.set_ellipsize(Pango.EllipsizeMode.END)
        preview.get_style_context().add_class("clipv-preview")
        meta = Gtk.Label(label=self._meta_text(data.entry))
        meta.set_xalign(0)
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        meta.get_style_context().add_class("clipv-meta")
        vbox.pack_start(preview, False, False, 0)
        vbox.pack_start(meta, False, False, 0)
        hbox.pack_start(vbox, True, True, 0)
        if data.entry.kind == "image":
            thumb = self._thumb_pixbuf(data.entry.content)
            if thumb is not None:
                img = Gtk.Image.new_from_pixbuf(thumb)
                img.set_valign(Gtk.Align.CENTER)
                # EventBox 提供事件窗口：悬停时弹出稍大的预览（preview.py）
                thumb_box = Gtk.EventBox()
                thumb_box.get_style_context().add_class("clipv-thumb")
                thumb_box.add(img)
                path = data.entry.content
                thumb_box.connect(
                    "enter-notify-event",
                    lambda *_: self._preview.schedule(path))
                thumb_box.connect(
                    "leave-notify-event",
                    lambda *_: self._preview.hide())
                hbox.pack_start(thumb_box, False, False, 0)
                hbox.reorder_child(thumb_box, 0)

        pin = Gtk.Button(label="★" if data.entry.pinned else "☆")
        pin.get_style_context().add_class("clipv-pin")
        if data.entry.pinned:
            pin.get_style_context().add_class("clipv-pinned")
        pin.set_valign(Gtk.Align.CENTER)
        pin.set_relief(Gtk.ReliefStyle.NONE)
        pin.set_can_focus(False)
        pin.connect("clicked", lambda *_: self._toggle_pin(data.entry.id))
        hbox.pack_end(pin, False, False, 0)

        delete = Gtk.Button(label="✕")
        delete.get_style_context().add_class("clipv-del")
        delete.set_valign(Gtk.Align.CENTER)
        delete.set_relief(Gtk.ReliefStyle.NONE)
        delete.set_can_focus(False)
        delete.connect("clicked", lambda *_: self._delete_entry(data.entry.id))
        hbox.pack_end(delete, False, False, 0)
        row.clipv_delete = delete

        def on_state(_row, flags):
            if flags & Gtk.StateFlags.PRELIGHT:
                delete.show()
            else:
                delete.hide()
        row.connect("state-flags-changed", on_state)
        return row

    @staticmethod
    def _meta_text(entry: Entry) -> str:
        meta = relative_time(entry.captured_at)
        if entry.source_app:
            meta += f" · {entry.source_app}"
        return meta

    @staticmethod
    def _preview_markup(text: str, query: str) -> str:
        """预览行 Pango markup：命中搜索关键词的片段加高亮底色。"""
        qf = query.strip().casefold()
        if not qf or qf not in text.casefold():
            return GLib.markup_escape_text(text)
        out: List[str] = []
        pos = 0
        qlen = len(qf)
        while True:
            idx = text.casefold().find(qf, pos)
            if idx < 0:
                out.append(GLib.markup_escape_text(text[pos:]))
                break
            if idx > pos:
                out.append(GLib.markup_escape_text(text[pos:idx]))
            hit = GLib.markup_escape_text(text[idx:idx + qlen])
            out.append(f'<span bgcolor="{_HIGHLIGHT_BG}" bgalpha="28%">'
                       f"{hit}</span>")
            pos = idx + qlen
        return "".join(out)

    def _on_search_changed(self, _entry) -> None:
        if not self._entries and not self.search.get_text():
            return
        self._rebuild_filtered()

    # ------------------------------------------------------------ 显示/隐藏
    def is_visible(self) -> bool:
        return self.get_visible()

    def xid(self) -> Optional[int]:
        """底层 X11 窗口 id（非 X11 会话或窗口句柄未创建时返回 None）。"""
        win = self.get_window()
        if win is None:
            return None
        try:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11
            return int(GdkX11.X11Window.get_xid(win))
        except Exception:
            return None

    def show_at_cursor(self) -> None:
        """显示在鼠标所在显示器（X11 精确居中；Wayland 由合成器决定）。"""
        self.reload(preserve_selection=False)
        self.search.set_text("")
        self._position_at_cursor()
        self.show()
        self.present()
        self.search.grab_focus()
        self._shown_at = time.monotonic()
        if self.listbox.get_children():
            self.listbox.select_row(self.listbox.get_row_at_index(0))

    def _position_at_cursor(self) -> None:
        try:
            display = Gdk.Display.get_default()
            if display is None:
                return
            seat = display.get_default_seat()
            pointer = seat.get_pointer() if seat is not None else None
            if pointer is None:
                return
            _screen, mx, my = pointer.get_position()
            monitor = display.get_monitor_at_point(mx, my)
            if monitor is None:
                return
            geo = monitor.get_workarea()
            x = geo.x + max(0, (geo.width - PANEL_WIDTH) // 2)
            y = geo.y + max(0, int(geo.height * 0.24))
            # 小屏幕防护：面板放大后可能超出工作区底部
            y = min(y, geo.y + max(0, geo.height - PANEL_HEIGHT))
            # 每次显示前显式设置位置：
            # 规避 muffin（Cinnamon WM）窗口重映射上移缺陷，同时保证定位稳定
            self.move(x, y)
        except Exception:
            log.debug("面板定位失败", exc_info=True)

    def hide_panel(self) -> None:
        self._preview.hide()
        self.hide()

    # ------------------------------------------------------------ 键盘
    def _on_entry_key(self, _widget, event) -> bool:
        return self._handle_key(event.keyval, event.state)

    def _on_key_press(self, _widget, event) -> bool:
        # 焦点不在搜索框时（点击行/按钮后）的兜底
        return self._handle_key(event.keyval, event.state)

    def _handle_key(self, keyval: int, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move_selection(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move_selection(-1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            self._activate_current(want_paste=True)
            return True
        if keyval == Gdk.KEY_Escape:
            self.hide_panel()
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            if ctrl and shift:
                self._request_clear()
            elif ctrl:
                pass  # Ctrl+Delete 未定义，忽略
            else:
                self._delete_current()
            return True
        if keyval in (Gdk.KEY_p, Gdk.KEY_P) and ctrl:
            eid = self._current_entry_id()
            if eid is not None:
                self._toggle_pin(eid)
            return True
        return False

    def _move_selection(self, delta: int) -> None:
        count = len(self.listbox.get_children())
        if count == 0:
            return
        row = self.listbox.get_selected_row()
        if row is None:
            index = 0 if delta > 0 else count - 1
        else:
            index = max(0, min(count - 1, row.get_index() + delta))
        new_row = self.listbox.get_row_at_index(index)
        self.listbox.select_row(new_row)
        self._scroll_to_row(new_row)

    def _scroll_to_row(self, row: Gtk.ListBoxRow) -> None:
        def do_scroll() -> bool:
            adj = self.scroll.get_vadjustment()
            alloc = row.get_allocation()
            top = adj.get_value()
            bottom = top + adj.get_page_size()
            if alloc.y < top:
                adj.set_value(alloc.y)
            elif alloc.y + alloc.height > bottom:
                adj.set_value(alloc.y + alloc.height - adj.get_page_size())
            return False
        GLib.idle_add(do_scroll)

    # ------------------------------------------------------------ 鼠标
    def _on_list_scroll(self, _adj) -> None:
        self._preview.hide()

    def _on_row_activated(self, _listbox, row) -> None:
        data = getattr(row, "clipv_data", None)
        if data is not None:
            self._activate_entry(data.entry, want_paste=True)

    def _on_list_button(self, _widget, event) -> bool:
        if event.button == 3:   # 右键菜单
            row = self.listbox.get_row_at_y(int(event.y))
            data = getattr(row, "clipv_data", None) if row is not None else None
            if data is None:
                return False
            self.listbox.select_row(row)
            self._popup_menu(row, event)
            return True
        return False

    def _popup_menu(self, row, event) -> None:
        data = row.clipv_data
        menu = Gtk.Menu()
        item_pin = Gtk.MenuItem(
            label="取消固定" if data.entry.pinned else "固定")
        item_pin.connect("activate", lambda *_: self._toggle_pin(data.entry.id))
        item_copy = Gtk.MenuItem(label="仅复制到剪贴板")
        item_copy.connect(
            "activate",
            lambda *_: self._activate_entry(data.entry, want_paste=False))
        item_del = Gtk.MenuItem(label="删除")
        item_del.connect("activate", lambda *_: self._delete_entry(data.entry.id))
        menu.append(item_pin)
        menu.append(item_copy)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(item_del)
        menu.show_all()
        self._menu_open = True
        self._menu = menu
        menu.connect("hide", self._on_menu_hide)
        menu.popup_at_pointer(event)

    def _on_menu_hide(self, *_args) -> None:
        self._menu_open = False
        self._menu = None

    # ------------------------------------------------------------ 操作
    def _activate_current(self, want_paste: bool) -> None:
        eid = self._current_entry_id()
        if eid is None:
            return
        entry = self.store.get(eid)
        if entry is not None:
            self._activate_entry(entry, want_paste)

    def _activate_entry(self, entry: Entry, want_paste: bool) -> None:
        if not self.is_visible():
            return  # 防双击/竞态导致的二次触发
        # 先隐藏面板，再交给守护进程写回/粘贴（F-27/F-29 时序）
        self.hide_panel()
        if self._on_activate is not None:
            self._on_activate(entry, want_paste)

    def _toggle_pin(self, entry_id: int) -> None:
        entry = self.store.get(entry_id)
        if entry is None:
            return
        self.store.set_pinned(entry_id, not entry.pinned)
        self.reload(preserve_selection=True)
        self._show_toast("已固定" if not entry.pinned else "已取消固定")
        self.search.grab_focus()

    def _delete_current(self) -> None:
        eid = self._current_entry_id()
        if eid is not None:
            self._delete_entry(eid)

    def _delete_entry(self, entry_id: int) -> None:
        row = self.listbox.get_selected_row()
        index = row.get_index() if row is not None else 0
        if self.store.delete(entry_id):
            self.reload(preserve_selection=False)
            count = len(self.listbox.get_children())
            if count:
                target = self.listbox.get_row_at_index(min(index, count - 1))
                self.listbox.select_row(target)
            self._show_toast("已删除")
        self.search.grab_focus()

    def _request_clear(self) -> None:
        now = time.monotonic()
        if now < self._clear_confirm_until:
            removed = self.store.clear_unpinned()
            self._clear_confirm_until = 0.0
            self.reload(preserve_selection=False)
            self._show_toast(f"已清空 {removed} 条非固定记录")
            return
        self._clear_confirm_until = now + 2.5
        ctx = self.hint_label.get_style_context()
        ctx.remove_class("clipv-toast")
        ctx.add_class("clipv-danger")
        self.hint_label.set_text(_CONFIRM_HINT)
        self._schedule_footer_reset(2500)

    # ------------------------------------------------------------ 反馈
    def _show_toast(self, text: str) -> None:
        ctx = self.hint_label.get_style_context()
        ctx.remove_class("clipv-danger")
        ctx.add_class("clipv-toast")
        self.hint_label.set_text(text)
        self._schedule_footer_reset(1800)

    def _schedule_footer_reset(self, ms: int) -> None:
        self._remove_footer_source()
        self._footer_source = GLib.timeout_add(ms, self._reset_footer)

    def _remove_footer_source(self) -> None:
        if self._footer_source:
            GLib.source_remove(self._footer_source)
            self._footer_source = 0

    def _reset_footer(self) -> bool:
        self._footer_source = 0
        self._clear_confirm_until = 0.0
        ctx = self.hint_label.get_style_context()
        ctx.remove_class("clipv-toast")
        ctx.remove_class("clipv-danger")
        self.hint_label.set_text(_FOOTER_HINT)
        return False

    # ------------------------------------------------------------ 窗口事件
    def _on_focus_out(self, _widget, _event) -> bool:
        if not self.get_visible():
            return False
        # 刚显示时的短暂失活（WM 交互）不算用户切换窗口
        if time.monotonic() - self._shown_at < 0.3 or self._menu_open:
            return False
        if Gtk.grab_get_current() is not None:
            return False  # 右键菜单等内部 grab 期间不隐藏
        self.hide_panel()  # F-29：失去焦点自动隐藏
        return False
