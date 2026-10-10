"""存储层单元测试（纯 stdlib，不依赖 GUI/显示环境）。"""
import os
import tempfile
import unittest

from clipv.store import Store, content_hash


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "history.db")
        self.store = Store(path=self.path, max_items=3)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_list_order(self):
        self.store.add("第一个")
        self.store.add("第二个")
        entries = self.store.list()
        self.assertEqual([e.content for e in entries], ["第二个", "第一个"])

    def test_dedupe_promotes_to_top(self):
        self.store.add("A")
        self.store.add("B")
        self.store.add("A")  # 去重并置顶（F-03）
        entries = self.store.list()
        self.assertEqual([e.content for e in entries], ["A", "B"])
        self.assertEqual(len(entries), 2)

    def test_prune_keeps_only_unpinned_limit(self):
        for i in range(5):
            self.store.add(f"item-{i}")
        entries = self.store.list()
        contents = [e.content for e in entries]
        self.assertEqual(len(entries), 3)
        self.assertEqual(contents, ["item-4", "item-3", "item-2"])

    def test_pinned_exempt_from_prune_and_clear(self):
        first, _ = self.store.add("固定条目")
        self.store.set_pinned(first.id, True)
        for i in range(5):
            self.store.add(f"item-{i}")
        entries = self.store.list()
        self.assertTrue(entries[0].pinned)
        self.assertEqual(entries[0].content, "固定条目")  # 固定组置顶
        removed = self.store.clear_unpinned()
        self.assertEqual(removed, 3)
        remaining = self.store.list()
        self.assertEqual([e.content for e in remaining], ["固定条目"])

    def test_dedupe_of_pinned_entry_keeps_pinned(self):
        entry, _ = self.store.add("密码片段")
        self.store.set_pinned(entry.id, True)
        again, action = self.store.add("密码片段")
        self.assertEqual(action, "promoted")
        self.assertTrue(again.pinned)
        total, pinned = self.store.stats()
        self.assertEqual((total, pinned), (1, 1))

    def test_delete_and_clear_all(self):
        e1, _ = self.store.add("x")
        self.store.add("y")
        self.assertTrue(self.store.delete(e1.id))
        self.store.add("z")
        total, _ = self.store.stats()
        self.assertEqual(total, 2)
        self.assertEqual(self.store.clear_all(), 2)

    def test_query_filter(self):
        self.store.add("Hello World")
        self.store.add("你好，世界")
        hits = self.store.list(query="hello")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].content, "Hello World")
        self.assertEqual(len(self.store.list(query="世界")), 1)

    def test_kind_preserved_on_promote(self):
        path = "/tmp/pic.png"
        entry, action = self.store.add(path, kind="image")
        self.assertEqual(action, "created")
        self.assertEqual(entry.kind, "image")
        again, action = self.store.add(path, kind="image")
        self.assertEqual(action, "promoted")
        self.assertEqual(again.kind, "image")  # 置顶后 kind 不丢失

    def test_image_search_alias(self):
        self.store.add("/tmp/pic.png", kind="image")
        self.store.add("hello world")
        self.assertEqual(len(self.store.list(query="图片")), 1)
        self.assertEqual(len(self.store.list(query="image")), 1)
        self.assertEqual(self.store.list(query="图片")[0].kind, "image")
        self.assertEqual(len(self.store.list(query="png")), 0)  # 路径不参与匹配
        self.assertEqual(len(self.store.list(query="hello")), 1)

    def test_content_hash_stable(self):
        self.assertEqual(content_hash("abc"), content_hash("abc"))
        self.assertNotEqual(content_hash("abc"), content_hash("abd"))

    def test_file_permissions(self):
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)  # F-73


if __name__ == "__main__":
    unittest.main()
