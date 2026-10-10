"""图片悬停预览测试：定位算法、缩放规则、显示/隐藏与延迟行为。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, Gtk

    from clipv import preview as pv_mod

    _HAS_GTK = True
except (ImportError, ValueError):
    _HAS_GTK = False

_HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


def _pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)


@unittest.skipUnless(_HAS_GTK, "需要 GTK3")
class ComputePositionTest(unittest.TestCase):
    WA = (0, 0, 1920, 1080)

    def test_default_bottom_right_offset(self):
        x, y = pv_mod.compute_position(100, 100, 300, 200, self.WA, offset=16)
        self.assertEqual((x, y), (116, 116))

    def test_flip_left_and_up_near_edges(self):
        # 鼠标在右下角附近：预览翻转到左上
        x, y = pv_mod.compute_position(1900, 1070, 300, 200,
                                       self.WA, offset=16)
        self.assertEqual(x, 1900 - 300 - 16)
        self.assertEqual(y, 1070 - 200 - 16)

    def test_clamped_when_larger_than_workarea(self):
        x, y = pv_mod.compute_position(500, 500, 2000, 1100,
                                       self.WA, offset=16)
        self.assertEqual((x, y), (0, 0))

    def test_respects_workarea_origin(self):
        wa = (100, 200, 800, 600)    # 副显示器（原点非零）
        x, y = pv_mod.compute_position(150, 250, 300, 200, wa, offset=16)
        self.assertEqual((x, y), (166, 266))


@unittest.skipUnless(_HAS_GTK and _HAS_DISPLAY, "需要 GTK3 与图形会话（DISPLAY）")
class ImagePreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, _argv = Gtk.init_check()
        if not ok:
            raise unittest.SkipTest("GTK 初始化失败：无法连接显示服务器")
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.big = cls._make_png("big.png", 800, 600)
        cls.wide = cls._make_png("wide.png", 1600, 200)
        cls.small = cls._make_png("small.png", 100, 60)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    @classmethod
    def _make_png(cls, name, w, h):
        path = os.path.join(cls.tmpdir.name, name)
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
        pb.fill(0x3a6ea5ff)
        pb.savev(path, "png", [], [])
        return path

    def _make(self):
        preview = pv_mod.ImagePreview()
        self.addCleanup(preview.hide)
        return preview

    def test_fit_without_upscale(self):
        preview = self._make()
        self.assertTrue(preview.show_for(self.big))
        pb = preview._image.get_pixbuf()
        self.assertEqual((pb.get_width(), pb.get_height()), (480, 360))

    def test_wide_and_small_images(self):
        preview = self._make()
        self.assertTrue(preview.show_for(self.wide))
        pb = preview._image.get_pixbuf()
        self.assertEqual((pb.get_width(), pb.get_height()), (480, 60))
        self.assertTrue(preview.show_for(self.small))
        pb = preview._image.get_pixbuf()
        self.assertEqual((pb.get_width(), pb.get_height()), (100, 60))

    def test_missing_file_returns_false(self):
        preview = self._make()
        self.assertFalse(preview.show_for("/nonexistent/nope.png"))

    def test_visible_and_hide(self):
        preview = self._make()
        preview.show_for(self.small)
        self.assertTrue(preview._win.get_visible())
        preview.hide()
        self.assertFalse(preview._win.get_visible())

    def test_schedule_cancelled_by_leave(self):
        preview = self._make()
        preview.schedule(self.small)
        preview.hide()          # 模拟鼠标移开：取消未触发的弹出
        _pump(0.6)
        self.assertFalse(preview._win is not None
                         and preview._win.get_visible())

    def test_schedule_fires_after_delay(self):
        preview = self._make()
        preview.schedule(self.small)
        _pump(0.8)              # 超过 HOVER_DELAY_MS
        self.assertTrue(preview._win.get_visible())
        preview.hide()


if __name__ == "__main__":
    unittest.main()
