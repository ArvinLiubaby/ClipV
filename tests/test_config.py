"""配置与快捷键解析单元测试。"""
import json
import os
import tempfile
import time
import unittest

from clipv.config import (Config, ConfigError, DEFAULTS, format_hotkey,
                          parse_hotkey, socket_path)


class HotkeyParseTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_hotkey("Super+V"), (["super"], "v"))
        self.assertEqual(parse_hotkey("ctrl+shift+f2"), (["ctrl", "shift"], "f2"))
        self.assertEqual(parse_hotkey("Alt+1"), (["alt"], "1"))

    def test_aliases(self):
        self.assertEqual(parse_hotkey("Meta+V"), (["super"], "v"))
        self.assertEqual(parse_hotkey("Win+V"), (["super"], "v"))
        self.assertEqual(parse_hotkey("Control+Alt+V"), (["ctrl", "alt"], "v"))

    def test_errors(self):
        for bad in ("V", "Super+", "Super+Bogus", "Ctrl+Ctrl+V", "+V", ""):
            with self.assertRaises(ValueError):
                parse_hotkey(bad)

    def test_format(self):
        self.assertEqual(format_hotkey(["super"], "v"), "Super+V")
        self.assertEqual(format_hotkey(["ctrl", "shift"], "f2"), "Ctrl+Shift+F2")


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_creates_default_file(self):
        cfg = Config.load(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(cfg.get("history.max_items"), 100)
        self.assertEqual(cfg.get("ui.theme"), "auto")

    def test_set_validates_and_persists(self):
        cfg = Config.load(self.path)
        cfg.set("history.max_items", "5")
        cfg.set("ignore.apps", "Foo, Bar baz")
        cfg.set("hotkey.toggle", "ctrl+shift+v")
        fresh = Config.load(self.path)
        self.assertEqual(fresh.get("history.max_items"), 5)
        self.assertEqual(fresh.get("ignore.apps"), ["foo", "bar", "baz"])
        self.assertEqual(fresh.get("hotkey.toggle"), "Ctrl+Shift+V")

    def test_set_invalid_raises(self):
        cfg = Config.load(self.path)
        with self.assertRaises(ConfigError):
            cfg.set("ui.theme", "neon")
        with self.assertRaises(ConfigError):
            cfg.set("hotkey.toggle", "V")
        with self.assertRaises(ConfigError):
            cfg.set("not.exist", 1)

    def test_reload_if_changed(self):
        cfg = Config.load(self.path)
        with open(self.path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        data["history"]["max_items"] = 42
        time.sleep(0.01)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.utime(self.path, (time.time() + 2, time.time() + 2))
        self.assertTrue(cfg.reload_if_changed())
        self.assertEqual(cfg.get("history.max_items"), 42)
        self.assertFalse(cfg.reload_if_changed())

    def test_broken_file_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        cfg = Config.load(self.path)
        self.assertTrue(cfg.load_error)
        self.assertEqual(cfg.get("history.max_items"), DEFAULTS["history"]["max_items"])

    def test_db_path_follows_storage_setting(self):
        cfg = Config.load(self.path)
        custom = os.path.join(self.tmp.name, "custom.db")
        cfg.set("storage.path", custom)
        self.assertEqual(cfg.db_path(), custom)

    def test_socket_path_sane(self):
        path = socket_path()
        self.assertTrue(path.endswith(".sock"))
        self.assertLess(len(path), 100)  # Unix socket 路径长度限制


if __name__ == "__main__":
    unittest.main()
