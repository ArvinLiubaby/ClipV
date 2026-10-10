"""冒烟测试：面板交互（进程内）+ 守护进程全链路（真实子进程）。

运行环境要求：系统已安装 GTK3/PyGObject（python3-gi、gir1.2-gtk-3.0）
且存在可用的图形会话（DISPLAY），否则测试自动跳过。
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# 必须在导入 clipv 之前重定向环境（测试隔离，不污染用户目录）
_TMP = tempfile.mkdtemp(prefix="clipv-smoke-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")
os.environ["XDG_STATE_HOME"] = os.path.join(_TMP, "state")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(_TMP, "run")
os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk

    from clipv import ipc
    from clipv.config import Config, socket_path
    from clipv.panel import ClipvPanel
    from clipv.store import Store

    _HAS_GTK = True
except (ImportError, ValueError):  # 未安装 GTK3/PyGObject 时跳过
    _HAS_GTK = False

_PKG_ROOT = str(Path(__file__).resolve().parents[1])
_HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


def _pump() -> None:
    """抽干 GTK 主循环待处理事件（代替 Qt processEvents）。"""
    while Gtk.events_pending():
        Gtk.main_iteration()


@unittest.skipUnless(_HAS_GTK and _HAS_DISPLAY, "需要 GTK3 与图形会话（DISPLAY）")
class PanelSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, _argv = Gtk.init_check()
        if not ok:
            raise unittest.SkipTest("GTK 初始化失败：无法连接显示服务器")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config.load(os.path.join(self.tmp.name, "config.json"))
        self.store = Store(path=os.path.join(self.tmp.name, "history.db"),
                           max_items=50)
        self.got = []
        self.panel = ClipvPanel(
            self.store, self.config,
            on_activate=lambda e, wp: self.got.append((e.content, wp)))

    def tearDown(self):
        self.panel.destroy()
        self.store.close()
        self.tmp.cleanup()

    def _press(self, keyval, state=0):
        handled = self.panel._handle_key(keyval, state)
        _pump()
        return handled

    def _show(self):
        self.panel.show_at_cursor()
        _pump()

    def _row_count(self):
        return len(self.panel.listbox.get_children())

    def test_show_filter_activate(self):
        self.store.add("hello world")
        self.store.add("第二条：测试搜索")
        self._show()
        self.assertTrue(self.panel.is_visible())
        self.assertEqual(self._row_count(), 2)

        # 实时过滤（F-41）
        self.panel.search.set_text("hello")
        _pump()
        self.assertEqual(self._row_count(), 1)
        self.panel.search.set_text("")
        _pump()
        self.assertEqual(self._row_count(), 2)

        # Enter 确认 → 隐藏面板并回调 on_activate（F-23/F-26）
        self.assertTrue(self._press(Gdk.KEY_Return))
        self.assertEqual(len(self.got), 1)
        self.assertEqual(self.got[0][0], "第二条：测试搜索")  # 最新条目默认选中
        self.assertTrue(self.got[0][1])
        self.assertFalse(self.panel.is_visible())

    def test_navigation_and_pin_delete(self):
        self.store.add("A")
        self.store.add("B")   # 最新
        self._show()

        self._press(Gdk.KEY_Down)
        self.assertEqual(self.panel.listbox.get_selected_row().get_index(), 1)
        self._press(Gdk.KEY_Up)
        self.assertEqual(self.panel.listbox.get_selected_row().get_index(), 0)

        # Ctrl+P 固定（F-13）
        eid = self.panel._current_entry_id()
        self._press(Gdk.KEY_p, Gdk.ModifierType.CONTROL_MASK)
        self.assertTrue(self.store.get(eid).pinned)
        # Delete 删除（F-15）
        self._press(Gdk.KEY_Delete)
        self.assertIsNone(self.store.get(eid))
        self.assertEqual(self._row_count(), 1)

    def test_clear_requires_second_press(self):
        self.store.add("x")
        self.store.add("y")
        self._show()
        mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        self._press(Gdk.KEY_Delete, mods)      # 第一次：进入确认态
        self.assertEqual(self._row_count(), 2)
        self._press(Gdk.KEY_Delete, mods)      # 第二次：执行清空（F-16）
        self.assertEqual(self._row_count(), 0)

    def test_escape_closes(self):
        self._show()
        self._press(Gdk.KEY_Escape)
        self.assertFalse(self.panel.is_visible())

    def test_image_entry_rendering_and_search(self):
        try:
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
        except (ImportError, ValueError):
            self.skipTest("需要 GdkPixbuf")
        from clipv.images import save_pixbuf_as_png

        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 12, 9)
        pixbuf.fill(0xFF0000FF)
        path = save_pixbuf_as_png(pixbuf)
        self.assertTrue(path)
        self.store.add("普通文本")
        self.store.add(path, kind="image")  # 最新置顶
        self._show()
        self.assertEqual(self._row_count(), 2)

        # 图片行：预览为 [图片] 宽×高，且行内渲染缩略图
        row = self.panel.listbox.get_row_at_index(0)
        data = row.clipv_data
        self.assertEqual(data.entry.kind, "image")
        self.assertIn("[图片]", data.preview)
        self.assertIn("12×9", data.preview)

        def has_image(widget):
            if isinstance(widget, Gtk.Image):
                return True
            if isinstance(widget, Gtk.Container):
                return any(has_image(c) for c in widget.get_children())
            return False

        self.assertTrue(has_image(row))

        # 搜索"图片"命中图片条目；搜索文本命中普通条目
        self.panel.search.set_text("图片")
        _pump()
        self.assertEqual(self._row_count(), 1)
        self.panel.search.set_text("普通")
        _pump()
        self.assertEqual(self._row_count(), 1)
        self.assertEqual(self.panel.listbox.get_row_at_index(0).clipv_data.entry.kind,
                         "text")


@unittest.skipUnless(_HAS_GTK and _HAS_DISPLAY, "需要 GTK3 与图形会话（DISPLAY）")
class DaemonE2ETest(unittest.TestCase):
    """通过真实子进程验证：ping/add/list/toggle/quit 全链路。"""

    def _daemon_env(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = _PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def test_full_lifecycle(self):
        log_path = os.path.join(_TMP, "daemon-e2e.log")
        log_file = open(log_path, "wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "clipv", "daemon"],
            env=self._daemon_env(), stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=log_file)
        try:
            sock = socket_path()
            ready = False
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    if ipc.call({"cmd": "ping"}, path=sock, timeout=1).get("ok"):
                        ready = True
                        break
                except ipc.IpcError:
                    time.sleep(0.15)
            self.assertTrue(ready, self._dump_log(log_path))

            # add → list
            self.assertTrue(ipc.call(
                {"cmd": "add", "content": "冒烟测试-条目"}, path=sock)["ok"])
            data = ipc.call({"cmd": "list"}, path=sock)["data"]
            self.assertTrue(any(e["content"] == "冒烟测试-条目" for e in data))

            # toggle → 面板可见；再 toggle → 隐藏
            ipc.call({"cmd": "toggle"}, path=sock)
            status = ipc.call({"cmd": "status"}, path=sock)["data"]
            self.assertTrue(status["panel_visible"])
            ipc.call({"cmd": "toggle"}, path=sock)
            status = ipc.call({"cmd": "status"}, path=sock)["data"]
            self.assertFalse(status["panel_visible"])

            # clear
            removed = ipc.call({"cmd": "clear"}, path=sock)["data"]["removed"]
            self.assertGreaterEqual(removed, 1)

            # quit → 进程退出码 0
            ipc.call({"cmd": "quit"}, path=sock)
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0, self._dump_log(log_path))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            log_file.close()

    def _dump_log(self, path: str) -> str:
        try:
            with open(path, "rb") as fh:
                return "\n守护进程日志：\n" + fh.read().decode("utf-8", "replace")[-3000:]
        except OSError:
            return "(无日志)"


if __name__ == "__main__":
    unittest.main()
