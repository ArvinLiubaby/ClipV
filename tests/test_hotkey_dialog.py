"""快捷键设置对话框测试：按键事件解析、捕获交互与保存流程。"""
from __future__ import annotations

import os
import unittest

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk

    from clipv import hotkey_dialog as hd_mod

    _HAS_GTK = True
except (ImportError, ValueError):
    _HAS_GTK = False

_HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


def _key_event(keyval, state):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    event.state = state
    return event


@unittest.skipUnless(_HAS_GTK, "需要 GTK3")
class HotkeyFromEventTest(unittest.TestCase):
    """纯逻辑：不依赖显示服务器（Gdk.keyval_name 无需连接 X）。"""

    def test_with_modifier(self):
        self.assertEqual(
            hd_mod.hotkey_from_event(Gdk.KEY_v, Gdk.ModifierType.SUPER_MASK),
            "Super+V")

    def test_multiple_modifiers(self):
        state = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        self.assertEqual(hd_mod.hotkey_from_event(Gdk.KEY_v, state),
                         "Ctrl+Alt+V")

    def test_requires_strong_modifier(self):
        # 无修饰、或仅 Shift（等价大写输入）均不可用
        self.assertIsNone(hd_mod.hotkey_from_event(Gdk.KEY_v, 0))
        self.assertIsNone(hd_mod.hotkey_from_event(
            Gdk.KEY_v, Gdk.ModifierType.SHIFT_MASK))

    def test_shift_with_super(self):
        # 按住 Shift 时 keyval 为大写 V
        state = Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.SHIFT_MASK
        self.assertEqual(hd_mod.hotkey_from_event(Gdk.KEY_V, state),
                         "Shift+Super+V")

    def test_function_key(self):
        self.assertEqual(
            hd_mod.hotkey_from_event(Gdk.KEY_F5, Gdk.ModifierType.CONTROL_MASK),
            "Ctrl+F5")

    def test_unsupported_keys(self):
        for keyval in (Gdk.KEY_space, Gdk.KEY_Left, Gdk.KEY_F35):
            self.assertIsNone(
                hd_mod.hotkey_from_event(keyval, Gdk.ModifierType.SUPER_MASK))


@unittest.skipUnless(_HAS_GTK and _HAS_DISPLAY, "需要 GTK3 与图形会话（DISPLAY）")
class HotkeyDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, _argv = Gtk.init_check()
        if not ok:
            raise unittest.SkipTest("GTK 初始化失败：无法连接显示服务器")

    def _make(self, on_save=None):
        return hd_mod.HotkeyDialog(
            "Super+V", on_save=on_save or (lambda hotkey: (True, hotkey)))

    def test_capture_updates_value(self):
        dlg = self._make()
        dlg._on_key_press(dlg, _key_event(Gdk.KEY_c,
                                          Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(dlg._value, "Ctrl+C")
        dlg.destroy()

    def test_modifier_only_keeps_value(self):
        dlg = self._make()
        dlg._on_key_press(dlg, _key_event(Gdk.KEY_Control_L, 0))
        self.assertEqual(dlg._value, "Super+V")
        dlg.destroy()

    def test_invalid_key_keeps_value(self):
        dlg = self._make()
        dlg._on_key_press(dlg, _key_event(Gdk.KEY_space,
                                          Gdk.ModifierType.SUPER_MASK))
        self.assertEqual(dlg._value, "Super+V")
        dlg.destroy()

    def test_escape_destroys(self):
        dlg = self._make()
        destroyed = []
        dlg.connect("destroy", lambda *_: destroyed.append(1))
        dlg._on_key_press(dlg, _key_event(Gdk.KEY_Escape, 0))
        self.assertEqual(destroyed, [1])

    def test_save_success_destroys(self):
        saved = []
        dlg = self._make(on_save=lambda hotkey: (saved.append(hotkey),
                                                 (True, hotkey))[1])
        destroyed = []
        dlg.connect("destroy", lambda *_: destroyed.append(1))
        dlg._on_key_press(dlg, _key_event(Gdk.KEY_v,
                                          Gdk.ModifierType.SUPER_MASK
                                          | Gdk.ModifierType.SHIFT_MASK))
        dlg.response(Gtk.ResponseType.OK)
        self.assertEqual(saved, ["Shift+Super+V"])
        self.assertEqual(destroyed, [1])

    def test_save_failure_keeps_open(self):
        dlg = self._make(on_save=lambda hotkey: (False, "写入失败"))
        destroyed = []
        dlg.connect("destroy", lambda *_: destroyed.append(1))
        dlg.response(Gtk.ResponseType.OK)
        self.assertEqual(destroyed, [])
        self.assertEqual(dlg._status.get_text(), "写入失败")
        dlg.destroy()

    def test_restore_default(self):
        dlg = self._make()
        dlg._value = "Ctrl+Alt+P"
        dlg.response(hd_mod._RESPONSE_RESET)
        self.assertEqual(dlg._value, "Super+V")
        dlg.destroy()


if __name__ == "__main__":
    unittest.main()
