"""关于对话框测试：内容要素、仓库链接、关闭行为与 `clipv about` 入口。"""
from __future__ import annotations

import argparse
import os
import unittest
from unittest import mock

from clipv import cli

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from clipv import __version__
    from clipv import about_dialog as ad_mod

    _HAS_GTK = True
except (ImportError, ValueError):
    _HAS_GTK = False

_HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


def _walk(widget):
    """深度优先遍历 widget 树。"""
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _walk(child)


class AboutCommandTest(unittest.TestCase):
    """`clipv about`：经 IPC 请求守护进程弹窗（与托盘菜单同一入口）。"""

    def test_about_opens_dialog(self):
        with mock.patch.object(cli.ipc, "call",
                               return_value={"ok": True}) as call_mock:
            rc = cli.cmd_about(argparse.Namespace())
        self.assertEqual(rc, 0)
        self.assertEqual(call_mock.call_args.args[0], {"cmd": "about-dialog"})

    def test_about_daemon_not_running(self):
        with mock.patch.object(cli.ipc, "call",
                               side_effect=cli.ipc.IpcNotRunning("x")), \
                mock.patch.object(cli, "_start_daemon_background",
                                  return_value=False):
            rc = cli.cmd_about(argparse.Namespace())
        self.assertEqual(rc, 1)

    def test_about_dialog_error(self):
        with mock.patch.object(cli.ipc, "call",
                               return_value={"ok": False, "error": "打开失败"}):
            rc = cli.cmd_about(argparse.Namespace())
        self.assertEqual(rc, 1)


@unittest.skipUnless(_HAS_GTK and _HAS_DISPLAY, "需要 GTK3 与图形会话（DISPLAY）")
class AboutDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, _argv = Gtk.init_check()
        if not ok:
            raise unittest.SkipTest("GTK 初始化失败：无法连接显示服务器")

    def _make(self):
        dialog = ad_mod.AboutDialog()
        self.addCleanup(dialog.destroy)
        return dialog

    def test_title_version_and_name(self):
        dialog = self._make()
        self.assertEqual(dialog.get_title(), "关于 ClipV")
        texts = [w.get_text() for w in _walk(dialog.get_content_area())
                 if isinstance(w, Gtk.Label)]
        self.assertIn("ClipV", texts)
        self.assertIn(f"版本 {__version__}", texts)

    def test_repo_link(self):
        dialog = self._make()
        links = [w for w in _walk(dialog.get_content_area())
                 if isinstance(w, Gtk.LinkButton)]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].get_uri(), ad_mod.REPO_URL)
        self.assertEqual(links[0].get_label(), ad_mod.REPO_URL)

    def test_description_and_license(self):
        dialog = self._make()
        texts = [w.get_text() for w in _walk(dialog.get_content_area())
                 if isinstance(w, Gtk.Label)]
        self.assertTrue(any("剪贴板历史管理器" in t for t in texts))
        self.assertTrue(any("MIT License" in t for t in texts))

    def test_close_response_destroys(self):
        dialog = self._make()
        destroyed = []
        dialog.connect("destroy", lambda *_: destroyed.append(1))
        dialog.response(Gtk.ResponseType.CLOSE)
        self.assertEqual(destroyed, [1])

    def test_reopen_after_close(self):
        # 关闭后可重新构造（守护进程依赖 destroy 回调清理引用后重建）
        first = self._make()
        first.response(Gtk.ResponseType.CLOSE)
        second = self._make()
        self.assertIsNotNone(second.get_content_area())


if __name__ == "__main__":
    unittest.main()
