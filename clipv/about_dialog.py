"""关于对话框：展示应用信息、版本、GitHub 仓库地址与许可。

托盘菜单「关于 ClipV」与 `clipv about` 的界面实现；
仓库地址为可点击链接，由系统默认浏览器打开。
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from . import __app_name__, __version__

# 开源仓库地址（README 与本对话框展示的同一来源）
REPO_URL = "https://github.com/ArvinLiubaby/ClipV"

# 品牌图标（deb 安装到 hicolor/scalable/apps/clipv.svg）；
# 源码运行未安装图标时回退到 freedesktop 标准图标。
_ICON_NAME = "clipv"
_FALLBACK_ICON_NAME = "edit-paste"

_DESCRIPTION = "Linux 剪贴板历史管理器（类 Windows Win+V）"
_FEATURES = "GTK3 原生 · 文本 + 图片历史 · 纯本地存储 · 不联网"
_LICENSE = "MIT License · Copyright © 2026 arvin"


def _app_icon_name() -> str:
    """优先使用品牌图标；主题中不存在时回退标准图标。"""
    try:
        theme = Gtk.IconTheme.get_default()
        if theme is not None and theme.has_icon(_ICON_NAME):
            return _ICON_NAME
    except Exception:
        pass
    return _FALLBACK_ICON_NAME


class AboutDialog(Gtk.Dialog):
    """简洁的应用信息卡片：图标、名称、版本、简介、仓库链接与许可。"""

    def __init__(self) -> None:
        super().__init__(title=f"关于 {__app_name__}")
        self.set_icon_name(_app_icon_name())
        self.set_default_size(400, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_modal(True)

        area = self.get_content_area()
        area.set_spacing(6)
        area.set_margin_top(16)
        area.set_margin_bottom(2)
        area.set_margin_start(24)
        area.set_margin_end(24)

        icon = Gtk.Image.new_from_icon_name(_app_icon_name(),
                                            Gtk.IconSize.DIALOG)
        area.add(icon)

        name = Gtk.Label()
        name.set_markup(
            f"<span size='x-large' weight='bold'>{__app_name__}</span>")
        name.set_margin_top(2)
        area.add(name)

        version = Gtk.Label(label=f"版本 {__version__}")
        version.get_style_context().add_class("dim-label")
        area.add(version)

        description = Gtk.Label(label=_DESCRIPTION)
        description.set_margin_top(10)
        area.add(description)

        features = Gtk.Label()
        features.set_markup(f"<small>{_FEATURES}</small>")
        features.get_style_context().add_class("dim-label")
        area.add(features)

        link = Gtk.LinkButton.new_with_label(REPO_URL, REPO_URL)
        link.set_margin_top(12)
        link.set_tooltip_text("在浏览器中打开 GitHub 仓库")
        area.add(link)

        license_label = Gtk.Label()
        license_label.set_markup(f"<small>{_LICENSE}</small>")
        license_label.get_style_context().add_class("dim-label")
        license_label.set_margin_top(2)
        area.add(license_label)

        self.add_button("关闭", Gtk.ResponseType.CLOSE)
        self.set_default_response(Gtk.ResponseType.CLOSE)
        self.connect("response", lambda *_: self.destroy())
