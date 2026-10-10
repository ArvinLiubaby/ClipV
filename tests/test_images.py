"""图片存储模块单元测试（仅需 GdkPixbuf，无显示环境要求）。"""
import atexit
import os
import shutil
import tempfile
import unittest

# 必须在导入 clipv 之前重定向环境（图片仓库指向隔离的临时目录）
_TMP = tempfile.mkdtemp(prefix="clipv-images-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")

from clipv.images import (gc_orphan_images, images_dir, is_image_path,  # noqa: E402
                          png_size, save_image_file, save_pixbuf_as_png,
                          uri_to_path)
from clipv.store import Store  # noqa: E402

try:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    _HAS_PIXBUF = True
except (ImportError, ValueError):
    _HAS_PIXBUF = False


def _make_pixbuf(width=8, height=6, rgba=0xFF0000FF):
    pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    pixbuf.fill(rgba)
    return pixbuf


class UriTest(unittest.TestCase):
    def test_file_uri_and_plain_path(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as fh:
            self.assertEqual(uri_to_path("file://" + fh.name), fh.name)
            self.assertEqual(uri_to_path(fh.name), fh.name)

    def test_rejects_non_local(self):
        self.assertIsNone(uri_to_path("https://example.com/a.png"))
        self.assertIsNone(uri_to_path("relative/path.png"))
        self.assertIsNone(uri_to_path(""))
        self.assertIsNone(uri_to_path("line1\nline2"))

    def test_missing_file(self):
        self.assertIsNone(uri_to_path("file:///nonexistent/x.png"))

    def test_percent_encoded_uri(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "a b.png")
        with open(path, "wb") as fh:
            fh.write(b"x")
        uri = "file://" + path.replace(" ", "%20")
        self.assertEqual(uri_to_path(uri), path)


class ImagePathTest(unittest.TestCase):
    def test_extensions(self):
        self.assertTrue(is_image_path("/a/b.PNG"))
        self.assertTrue(is_image_path("/a/b.jpeg"))
        self.assertTrue(is_image_path("/a/b.webp"))
        self.assertFalse(is_image_path("/a/b.txt"))
        self.assertFalse(is_image_path(""))


class PngSizeTest(unittest.TestCase):
    def test_invalid_or_missing(self):
        bad = os.path.join(_TMP, "not-a.png")
        with open(bad, "wb") as fh:
            fh.write(b"hello world" * 4)
        self.assertIsNone(png_size(bad))
        self.assertIsNone(png_size(os.path.join(_TMP, "missing.png")))


@unittest.skipUnless(_HAS_PIXBUF, "需要 GdkPixbuf")
class ImageStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(path=os.path.join(self.tmp.name, "history.db"),
                           max_items=50)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_save_dedupe_and_size(self):
        pixbuf = _make_pixbuf(16, 12)
        path1 = save_pixbuf_as_png(pixbuf)
        self.assertTrue(path1)
        self.assertTrue(path1.startswith(images_dir()))
        self.assertEqual(png_size(path1), (16, 12))
        path2 = save_pixbuf_as_png(pixbuf)  # 相同内容 → 同一文件
        self.assertEqual(path1, path2)

    def test_save_image_file(self):
        pixbuf = _make_pixbuf(10, 8, 0x0080FFFF)
        src = os.path.join(self.tmp.name, "source.png")
        pixbuf.savev(src, "png", [], [])
        saved = save_image_file(src)
        self.assertTrue(saved)
        self.assertNotEqual(saved, src)  # 转存到图片仓库
        self.assertEqual(png_size(saved), (10, 8))

    def test_save_image_file_rejects_broken(self):
        broken = os.path.join(self.tmp.name, "broken.png")
        with open(broken, "wb") as fh:
            fh.write(b"not an image")
        self.assertIsNone(save_image_file(broken))
        self.assertIsNone(save_image_file(os.path.join(self.tmp.name, "no.png")))

    def test_gc_orphan_images(self):
        referenced = save_pixbuf_as_png(_make_pixbuf(8, 8, 0xFF0000FF))
        orphan = save_pixbuf_as_png(_make_pixbuf(8, 8, 0x0000FFFF))
        self.assertNotEqual(referenced, orphan)
        self.store.add(referenced, kind="image")

        removed = gc_orphan_images(self.store)
        self.assertGreaterEqual(removed, 1)
        self.assertTrue(os.path.exists(referenced))
        self.assertFalse(os.path.exists(orphan))

        # 清空历史后原引用文件也成孤儿
        self.store.clear_all()
        gc_orphan_images(self.store)
        self.assertFalse(os.path.exists(referenced))


if __name__ == "__main__":
    unittest.main()
