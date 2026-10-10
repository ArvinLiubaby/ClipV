"""托盘图标测试：菜单结构与回调、后端降级、异常安全。"""
from __future__ import annotations

import os
import unittest
from unittest import mock

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from clipv import tray as tray_mod

    _HAS_GTK = True
except (ImportError, ValueError):
    _HAS_GTK = False

_HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


@unittest.skipUnless(_HAS_GTK and _HAS_DISPLAY, "需要 GTK3 与图形会话（DISPLAY）")
class TrayIconTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, _argv = Gtk.init_check()
        if not ok:
            raise unittest.SkipTest("GTK 初始化失败：无法连接显示服务器")

    def test_menu_items_and_callbacks(self):
        shown = []
        quit_calls = []
        settings_calls = []
        about_calls = []
        tray = tray_mod.TrayIcon(on_show=lambda: shown.append(1),
                                 on_quit=lambda: quit_calls.append(1),
                                 on_settings=lambda: settings_calls.append(1),
                                 on_about=lambda: about_calls.append(1))
        menu = tray._build_menu()
        items = [w for w in menu.get_children()
                 if isinstance(w, Gtk.MenuItem)
                 and not isinstance(w, Gtk.SeparatorMenuItem)]
        self.assertEqual([i.get_label() for i in items],
                         ["显示面板", "设置快捷键…", "关于 ClipV", "退出 ClipV"])
        items[0].activate()
        self.assertEqual(len(shown), 1)
        items[1].activate()
        self.assertEqual(len(settings_calls), 1)
        items[2].activate()
        self.assertEqual(len(about_calls), 1)
        items[3].activate()
        self.assertEqual(len(quit_calls), 1)

    def test_menu_without_settings(self):
        # 未传 on_settings / on_about 时不出现对应项
        tray = tray_mod.TrayIcon(on_show=lambda: None, on_quit=lambda: None)
        menu = tray._build_menu()   # 保持引用，避免临时菜单被回收后子项失效
        items = [w.get_label() for w in menu.get_children()
                 if isinstance(w, Gtk.MenuItem)
                 and not isinstance(w, Gtk.SeparatorMenuItem)]
        self.assertEqual(items, ["显示面板", "退出 ClipV"])

    def test_menu_without_about(self):
        # 仅传 on_settings 时菜单不含“关于 ClipV”
        tray = tray_mod.TrayIcon(on_show=lambda: None, on_quit=lambda: None,
                                 on_settings=lambda: None)
        menu = tray._build_menu()
        items = [w.get_label() for w in menu.get_children()
                 if isinstance(w, Gtk.MenuItem)
                 and not isinstance(w, Gtk.SeparatorMenuItem)]
        self.assertEqual(items, ["显示面板", "设置快捷键…", "退出 ClipV"])

    def test_build_and_destroy(self):
        tray = tray_mod.TrayIcon(on_show=lambda: None, on_quit=lambda: None)
        tray.build()
        if not tray.available:
            self.skipTest("当前环境无可用托盘后端")
        self.assertIn(tray.backend, ("appindicator", "status-icon"))
        tray.destroy()
        self.assertFalse(tray.available)
        self.assertEqual(tray.backend, "none")

    def test_fallback_without_appindicator(self):
        with mock.patch.object(tray_mod, "_load_ayatana", return_value=None):
            tray = tray_mod.TrayIcon(on_show=lambda: None, on_quit=lambda: None)
            tray.build()
            # 无 Ayatana 时不得使用 appindicator 后端（StatusIcon 成功或均失败）
            self.assertNotEqual(tray.backend, "appindicator")
            tray.destroy()

    def test_create_tray_never_raises(self):
        with mock.patch.object(tray_mod.TrayIcon, "build",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(
                tray_mod.create_tray(lambda: None, lambda: None))

    def test_resolve_icon_name(self):
        # 主题含品牌图标时用 clipv；缺失时回退标准图标
        with mock.patch.object(tray_mod, "_icon_theme_has_icon",
                               return_value=True):
            self.assertEqual(tray_mod._resolve_icon_name(),
                             tray_mod._ICON_NAME)
        with mock.patch.object(tray_mod, "_icon_theme_has_icon",
                               return_value=False):
            self.assertEqual(tray_mod._resolve_icon_name(),
                             tray_mod._FALLBACK_ICON_NAME)


if __name__ == "__main__":
    unittest.main()
