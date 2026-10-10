"""进程间通信：Unix Domain Socket + 行式 JSON。

- 客户端（CLI）零依赖、轻量，不导入 GUI 库；
- 服务端（daemon）在线程中收发，由调用方桥接到主线程执行（见 daemon.py）。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("clipv.ipc")

_MAX_LINE = 4 * 1024 * 1024  # 单条消息上限


class IpcError(Exception):
    pass


class IpcNotRunning(IpcError):
    pass


class IpcAlreadyRunning(IpcError):
    pass


def _send_line(sock: socket.socket, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    sock.sendall(data)


def _recv_line(sock: socket.socket, deadline: Optional[float] = None) -> Dict[str, Any]:
    buf = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in buf:
            break
        if len(buf) > _MAX_LINE:
            raise IpcError("消息过大")
    line = bytes(buf).split(b"\n", 1)[0]
    if not line:
        raise IpcError("连接被关闭且无响应")
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise IpcError(f"非法响应：{exc}") from exc


# ---------------------------------------------------------------- 客户端

def call(request: Dict[str, Any], path: Optional[str] = None,
         timeout: float = 5.0) -> Dict[str, Any]:
    """向守护进程发送一条请求并等待响应。"""
    from .config import socket_path  # 延迟导入，避免循环依赖
    path = path or socket_path()
    if not os.path.exists(path):
        raise IpcNotRunning("守护进程未运行")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as exc:
        sock.close()
        raise IpcNotRunning("守护进程未运行") from exc
    try:
        _send_line(sock, request)
        return _recv_line(sock)
    except socket.timeout as exc:
        raise IpcError("等待响应超时") from exc
    finally:
        sock.close()


def is_running(path: Optional[str] = None) -> bool:
    try:
        resp = call({"cmd": "ping"}, path=path, timeout=1.5)
        return bool(resp.get("ok"))
    except IpcError:
        return False


# ---------------------------------------------------------------- 服务端

class IpcServer:
    """每连接一请求一响应。handler(request) -> response，在服务线程中执行。"""

    def __init__(self, path: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        self.path = path
        self.handler = handler
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._bound_ino: Optional[int] = None  # 自己 bind 的 socket 文件 inode

    def start(self) -> None:
        if os.path.exists(self.path):
            if is_running(self.path):
                raise IpcAlreadyRunning("已有 ClipV 守护进程在运行")
            os.unlink(self.path)  # 清理残留 socket
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        try:
            self._bound_ino = os.stat(self.path).st_ino
        except OSError:
            self._bound_ino = None
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(
            target=self._serve, name="clipv-ipc", daemon=True)
        self._thread.start()
        log.info("IPC 服务已启动：%s", self.path)

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10.0)
            request = _recv_line(conn)
            try:
                response = self.handler(request)
            except Exception as exc:  # 单个请求失败不影响服务
                log.exception("IPC 请求处理失败: %s", request.get("cmd"))
                response = {"ok": False, "error": str(exc)}
            _send_line(conn, response)
        except (IpcError, OSError) as exc:
            log.debug("IPC 连接异常：%s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            # 仅当路径仍指向自己 bind 的 socket 时才清理：
            # 旧实例退出期间可能已有新实例接管同一路径（竞态），
            # 误删会把新实例变成"活着但不可见"的孤儿进程。
            if os.path.exists(self.path):
                if self._bound_ino is None or \
                        os.stat(self.path).st_ino == self._bound_ino:
                    os.unlink(self.path)
        except OSError:
            pass
