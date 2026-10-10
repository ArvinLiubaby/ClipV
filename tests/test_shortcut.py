"""桌面环境快捷键接管检测（de_binds_toggle）单元测试（不依赖 GUI 库）。"""
import argparse
import json
import os
import unittest
from unittest import mock

from clipv import cli

SCHEMA = "org.cinnamon.desktop.keybindings"
CUSTOM0 = ("org.cinnamon.desktop.keybindings.custom-keybinding:"
           "/org/cinnamon/desktop/keybindings/custom-keybindings/custom0/")


class DeBindsToggleTest(unittest.TestCase):
    def _detect(self, binding=("<Super>v",), command="clipv toggle",
                desktop="X-Cinnamon", custom_list=("custom0",),
                has_gsettings=True):
        table = {
            (SCHEMA, "custom-list"): list(custom_list),
            (CUSTOM0, "binding"): list(binding),
            (CUSTOM0, "command"): command,
        }
        with mock.patch.object(cli.shutil, "which",
                               return_value="/usr/bin/gsettings" if has_gsettings else None), \
                mock.patch.object(cli, "_gsettings_get",
                                  side_effect=lambda schema, key: table.get((schema, key))), \
                mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}):
            return cli.de_binds_toggle("Super+V")

    def test_bound_to_clipv(self):
        self.assertTrue(self._detect())

    def test_real_world_command_path(self):
        # shortcut install 写入的是 venv 绝对路径命令
        self.assertTrue(self._detect(
            command="/media/data/ClipV/.venv/bin/clipv toggle"))

    def test_other_command_not_detected(self):
        self.assertFalse(self._detect(command="/home/u/.local/bin/copyq-toggle"))

    def test_other_binding_not_detected(self):
        self.assertFalse(self._detect(binding=("<Super>c",)))

    def test_empty_custom_list(self):
        self.assertFalse(self._detect(custom_list=()))

    def test_non_cinnamon_desktop_skipped(self):
        self.assertFalse(self._detect(desktop="X-GNOME"))

    def test_no_gsettings(self):
        self.assertFalse(self._detect(has_gsettings=False))


class ReloadCustomsTest(unittest.TestCase):
    """_reload_cinnamon_customs：custom-list 置空再放回以触发 Cinnamon 重载。"""

    def test_toggles_custom_list(self):
        calls = []
        with mock.patch.object(cli, "_gsettings_get",
                               return_value=["custom0", "custom1"]), \
                mock.patch.object(cli, "_gsettings_set",
                                  side_effect=lambda s, k, v:
                                  (calls.append((k, v)) or True)):
            self.assertTrue(cli._reload_cinnamon_customs())
        self.assertEqual(calls, [("custom-list", "[]"),
                                 ("custom-list", "['custom0', 'custom1']")])

    def test_skips_when_empty(self):
        calls = []
        with mock.patch.object(cli, "_gsettings_get", return_value=[]), \
                mock.patch.object(cli, "_gsettings_set",
                                  side_effect=lambda s, k, v:
                                  (calls.append((k, v)) or True)):
            self.assertTrue(cli._reload_cinnamon_customs())
        self.assertEqual(calls, [])


class ShortcutInstallReloadTest(unittest.TestCase):
    """修改已有条目（custom-list 不变）后必须触发重载，否则 Cinnamon 沿用旧命令。"""

    def test_install_modified_entry_triggers_reload(self):
        table = {
            (SCHEMA, "custom-list"): ["custom0"],
            (CUSTOM0, "command"): "/x/.venv/bin/clipv toggle",
            (CUSTOM0, "binding"): ["<Super>v"],
            (CUSTOM0, "name"): "旧名称",
        }
        with mock.patch.object(cli, "_gsettings_get",
                               side_effect=lambda s, k: table.get((s, k))), \
                mock.patch.object(cli, "_gsettings_set", return_value=True), \
                mock.patch.object(cli, "_reload_cinnamon_customs",
                                  return_value=True) as reload_mock, \
                mock.patch.object(cli, "_STATE_KEYS_BACKUP", "/tmp/x.json"), \
                mock.patch("builtins.open", mock.mock_open()):
            rc = cli._shortcut_install_cinnamon("Super+V", "/x/.venv/bin/clipv")
        self.assertEqual(rc, 0)
        reload_mock.assert_called_once()


class ShortcutRemoveReloadTest(unittest.TestCase):
    """还原绑定（modified）后同样需要重载，否则内存中仍是 ClipV 的命令。"""

    def test_remove_modified_triggers_reload(self):
        backup = {"desktop": "cinnamon", "action": "modified", "idx": "custom0",
                  "name": "CopyQ", "binding": ["<Super>v"],
                  "command": "/home/u/.local/bin/copyq-toggle"}
        with mock.patch.object(cli, "_gsettings_set", return_value=True), \
                mock.patch.object(cli, "_reload_cinnamon_customs",
                                  return_value=True) as reload_mock, \
                mock.patch.object(cli, "_STATE_KEYS_BACKUP", "/tmp/x.json"), \
                mock.patch("builtins.open",
                           mock.mock_open(read_data=json.dumps(backup))), \
                mock.patch.object(cli.os, "unlink"):
            rc = cli._shortcut_remove_cinnamon()
        self.assertEqual(rc, 0)
        reload_mock.assert_called_once()


class ShortcutEditTest(unittest.TestCase):
    """`clipv shortcut edit`：经 IPC 请求守护进程弹出设置对话框。"""

    def test_edit_opens_dialog(self):
        with mock.patch.object(cli.ipc, "call",
                               return_value={"ok": True}) as call_mock:
            rc = cli.cmd_shortcut(argparse.Namespace(action="edit"))
        self.assertEqual(rc, 0)
        self.assertEqual(call_mock.call_args.args[0], {"cmd": "hotkey-dialog"})

    def test_edit_daemon_not_running(self):
        with mock.patch.object(cli.ipc, "call",
                               side_effect=cli.ipc.IpcNotRunning("x")), \
                mock.patch.object(cli, "_start_daemon_background",
                                  return_value=False):
            rc = cli.cmd_shortcut(argparse.Namespace(action="edit"))
        self.assertEqual(rc, 1)

    def test_edit_dialog_error(self):
        with mock.patch.object(cli.ipc, "call",
                               return_value={"ok": False,
                                             "error": "打开失败"}):
            rc = cli.cmd_shortcut(argparse.Namespace(action="edit"))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
