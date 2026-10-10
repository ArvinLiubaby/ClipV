"""系统托盘图标：常驻状态区，提供“显示面板 / 设置快捷键 / 退出 ClipV”入口。

后端优先级：
- AyatanaAppIndicator3（SNI 状态通知区，Cinnamon/KDE/Ubuntu 原生支持）；
- Gtk.StatusIcon（XEmbed 老式托盘，X11 会话兜底）。

托盘不可用（库缺失/无托盘宿主）时静默降级：图标不显示，
不影响守护进程主功能；create_tray() 保证不抛异常。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

log = logging.getLogger("clipv.tray")

# 品牌图标（deb 安装到 hicolor/scalable/apps/clipv.svg）；
# 源码运行未安装图标时回退到 freedesktop 标准图标。
_ICON_NAME = "clipv"
_FALLBACK_ICON_NAME = "edit-paste"


def _icon_theme_has_icon(name: str) -> bool:
    """查询当前图标主题中是否存在指定图标（异常按不存在处理）。"""
    try:
        theme = Gtk.IconTheme.get_default()
        return theme is not None and theme.has_icon(name)
    except Exception:
        return False


def _resolve_icon_name() -> str:
    """优先使用品牌图标；主题中不存在时回退标准图标。"""
    return _ICON_NAME if _icon_theme_has_icon(_ICON_NAME) else _FALLBACK_ICON_NAME


def _load_ayatana():
    """加载 AyatanaAppIndicator3；不可用时返回 None。"""
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3
        return AyatanaAppIndicator3
    except (ImportError, ValueError):
        return None


class TrayIcon:
    """托盘图标 + 右键菜单（显示面板 / 设置快捷键 / 退出 ClipV）。

    on_show：左键激活或菜单“显示面板”时调用；
    on_settings：菜单“设置快捷键…”时调用（可选，None 时隐藏该项）；
    on_quit：菜单“退出 ClipV”时调用（由守护进程优雅退出）。
    """

    def __init__(self, on_show: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_settings: Optional[Callable[[], None]] = None) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._on_settings = on_settings
        self._icon_name = _ICON_NAME
        self._indicator = None
        self._status_icon: Optional[Gtk.StatusIcon] = None
        self._menu: Optional[Gtk.Menu] = None

    # ------------------------------------------------------------ 属性
    @property
    def available(self) -> bool:
        return self._indicator is not None or self._status_icon is not None

    @property
    def backend(self) -> str:
        if self._indicator is not None:
            return "appindicator"
        if self._status_icon is not None:
            return "status-icon"
        return "none"

    # ------------------------------------------------------------ 构建
    def build(self) -> "TrayIcon":
        self._icon_name = _resolve_icon_name()
        self._menu = self._build_menu()
        if self._try_appindicator():
            return self
        self._try_status_icon()
        return self

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        item_show = Gtk.MenuItem(label="显示面板")
        item_show.connect("activate", lambda *_: self._on_show())
        menu.append(item_show)
        if self._on_settings is not None:
            item_settings = Gtk.MenuItem(label="设置快捷键…")
            item_settings.connect("activate", lambda *_: self._on_settings())
            menu.append(item_settings)
        item_quit = Gtk.MenuItem(label="退出 ClipV")
        item_quit.connect("activate", lambda *_: self._on_quit())
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(item_quit)
        menu.show_all()
        return menu

    def _try_appindicator(self) -> bool:
        mod = _load_ayatana()
        if mod is None:
            return False
        try:
            indicator = mod.Indicator.new(
                "clipv", self._icon_name,
                mod.IndicatorCategory.APPLICATION_STATUS)
            indicator.set_status(mod.IndicatorStatus.ACTIVE)
            indicator.set_title("ClipV 剪贴板历史")
            indicator.set_menu(self._menu)
            # 左键单击直接显示面板（宿主支持时生效；老版本库无此方法）
            try:
                item_show = self._menu.get_children()[0]
                indicator.set_secondary_activate_target(item_show)
            except (AttributeError, TypeError, IndexError):
                pass
            self._indicator = indicator
            log.info("系统托盘图标已创建（AppIndicator）")
            return True
        except Exception as exc:
            log.debug("AppIndicator 初始化失败：%s", exc)
            return False

    def _try_status_icon(self) -> bool:
        if not hasattr(Gtk, "StatusIcon"):
            return False
        try:
            icon = Gtk.StatusIcon.new_from_icon_name(self._icon_name)
            icon.set_tooltip_text("ClipV 剪贴板历史")
            icon.connect("activate", lambda *_: self._on_show())
            icon.connect("popup-menu", self._on_popup_menu)
            icon.set_visible(True)
            self._status_icon = icon
            log.info("系统托盘图标已创建（Gtk.StatusIcon）")
            return True
        except Exception as exc:
            log.warning("系统托盘图标创建失败（不影响使用）：%s", exc)
            return False

    def _on_popup_menu(self, icon: Gtk.StatusIcon, button: int,
                       activate_time: int) -> None:
        if self._menu is not None:
            self._menu.popup(None, None, Gtk.StatusIcon.position_menu,
                             icon, button, activate_time)

    # ------------------------------------------------------------ 清理
    def destroy(self) -> None:
        if self._status_icon is not None:
            try:
                self._status_icon.set_visible(False)
            except Exception:
                pass
            self._status_icon = None
        if self._indicator is not None:
            mod = _load_ayatana()
            if mod is not None:
                try:
                    self._indicator.set_status(mod.IndicatorStatus.PASSIVE)
                except Exception:
                    pass
            self._indicator = None
        self._menu = None


def create_tray(on_show: Callable[[], None],
                on_quit: Callable[[], None],
                on_settings: Optional[Callable[[], None]] = None
                ) -> Optional[TrayIcon]:
    """创建托盘图标；任何失败返回 None（调用方应继续正常工作）。"""
    try:
        tray = TrayIcon(on_show, on_quit, on_settings).build()
        return tray if tray.available else None
    except Exception as exc:
        log.debug("托盘图标不可用：%s", exc)
        return None
