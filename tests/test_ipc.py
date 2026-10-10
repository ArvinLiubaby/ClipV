"""IPC 通信单元测试（线程级，不依赖 GUI 库）。"""
import os
import tempfile
import unittest

from clipv import ipc


class IpcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.sock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_running(self):
        with self.assertRaises(ipc.IpcNotRunning):
            ipc.call({"cmd": "ping"}, path=self.path, timeout=1)
        self.assertFalse(ipc.is_running(self.path))

    def test_echo_server(self):
        def handler(request):
            if request.get("cmd") == "echo":
                return {"ok": True, "data": request.get("value")}
            if request.get("cmd") == "ping":
                return {"ok": True}
            return {"ok": False, "error": "unknown"}

        server = ipc.IpcServer(self.path, handler)
        server.start()
        try:
            self.assertTrue(ipc.is_running(self.path))
            resp = ipc.call({"cmd": "echo", "value": "你好"}, path=self.path)
            self.assertEqual(resp, {"ok": True, "data": "你好"})
            resp = ipc.call({"cmd": "nope"}, path=self.path)
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"], "unknown")
        finally:
            server.stop()
        self.assertFalse(os.path.exists(self.path))

    def test_handler_exception_isolated(self):
        def handler(_request):
            raise RuntimeError("boom")

        server = ipc.IpcServer(self.path, handler)
        server.start()
        try:
            resp = ipc.call({"cmd": "x"}, path=self.path)
            self.assertFalse(resp["ok"])
            self.assertIn("boom", resp["error"])
            # 服务仍可继续处理请求
            def _ok(_request):
                return {"ok": True}
            server.handler = _ok
            self.assertTrue(ipc.call({"cmd": "y"}, path=self.path)["ok"])
        finally:
            server.stop()

    def test_stale_socket_reclaimed(self):
        # 预置一个残留 socket 文件（无监听者）
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        s.close()  # 关闭后文件残留，模拟异常退出
        server = ipc.IpcServer(self.path, lambda r: {"ok": True})
        server.start()
        try:
            self.assertTrue(ipc.is_running(self.path))
        finally:
            server.stop()

    def test_stop_keeps_socket_replaced_by_new_instance(self):
        """竞态防护：旧实例退出时不得删除已被新实例接管的 socket 文件。"""
        import socket

        old = ipc.IpcServer(self.path, lambda _r: {"ok": True})
        old.start()
        # 模拟新实例接管：删除旧文件并在同一路径重新 bind
        os.unlink(self.path)
        fresh = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        fresh.bind(self.path)
        try:
            old.stop()
            self.assertTrue(os.path.exists(self.path))  # 新实例的文件不受影响
        finally:
            fresh.close()


if __name__ == "__main__":
    unittest.main()
