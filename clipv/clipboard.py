"""剪贴板监听与写回（GTK3）。

- X11 等原生会话：Gtk.Clipboard 的 "owner-change" 信号 + 防抖定时器，
  读取时同步记录来源应用（活动窗口）；
- Wayland：优先 `wl-paste --watch`（需合成器支持 data-control），
  由子进程 `clipv _ingest` 经 IPC 回传内容；不可用时降级并上报。
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import config as cfg_mod
from .images import (IMAGE_KIND, is_image_path, load_pixbuf,
                     save_image_file, save_pixbuf_as_png, uri_to_path)
from .session import SessionInfo
from .store import content_hash
from .util import truncate_text

log = logging.getLogger("clipv.clipboard")

_SELF_WRITE_WINDOW = 3.0   # 自身写回剪贴板的抑制窗口（秒）
_WATCH_RETRY_MIN = 5.0     # wl-paste 崩溃后的最小重试间隔


class ClipboardMonitor:
    """剪贴板变化监听器。on_captured(content, source_app, source_class)。"""

    def __init__(self, session: SessionInfo, config: cfg_mod.Config,
                 x11=None, on_captured=None, on_degraded=None):
        self.session = session
        self.config = config
        self.x11 = x11
        self._on_captured = on_captured
        self._on_degraded = on_degraded
        self._suppress_hash: str = ""
        self._suppress_at: float = 0.0
        self._clipboard: Optional[Gtk.Clipboard] = None
        self._owner_handler = 0
        self._debounce_source = 0
        self._watch_pid = 0
        self._watch_retry = _WATCH_RETRY_MIN
        self._watch_fail_count = 0
        self._watch_restart_source = 0
        self._stopping = False
        self._degraded_emitted = False

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self.session.is_wayland:
            self._start_wayland_watch()
        else:
            self._clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            self._owner_handler = self._clipboard.connect(
                "owner-change", self._on_owner_change)
            log.info("剪贴板监听已启动（GTK owner-change）")

    def stop(self) -> None:
        self._stopping = True
        self._remove_debounce()
        if self._watch_restart_source:
            GLib.source_remove(self._watch_restart_source)
            self._watch_restart_source = 0
        if self._watch_pid:
            try:
                os.kill(self._watch_pid, signal.SIGTERM)
            except OSError:
                pass
            self._watch_pid = 0
        if self._clipboard is not None:
            try:
                if self._owner_handler:
                    self._clipboard.disconnect(self._owner_handler)
            except Exception:
                pass
            self._owner_handler = 0
            self._clipboard = None

    # ------------------------------------------------------------ 自身写回
    def mark_self_write(self, content: str) -> None:
        """在写回剪贴板前调用，避免把自身的写入记录为一次复制。"""
        self._suppress_hash = content_hash(content)
        self._suppress_at = time.monotonic()

    def is_self_write(self, content: str) -> bool:
        if not self._suppress_hash:
            return False
        if time.monotonic() - self._suppress_at > _SELF_WRITE_WINDOW:
            self._suppress_hash = ""
            return False
        if content_hash(content) == self._suppress_hash:
            self._suppress_hash = ""
            return True
        return False

    # ------------------------------------------------------------ X11 / GTK
    def _on_owner_change(self, _clipboard, _event) -> None:
        self._remove_debounce()
        ms = max(60, int(self.config.get("daemon.poll_interval_ms")))
        self._debounce_source = GLib.timeout_add(ms, self._read_clipboard)

    def _remove_debounce(self) -> None:
        if self._debounce_source:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = 0

    def _read_clipboard(self) -> bool:
        self._debounce_source = 0
        clipboard = self._clipboard
        if clipboard is None:
            return False
        try:
            # 图像数据优先（截图工具/浏览器复制图片时常同时携带文本）
            if clipboard.wait_is_image_available():
                pixbuf = clipboard.wait_for_image()
                if pixbuf is not None:
                    path = save_pixbuf_as_png(pixbuf)
                    if path:
                        self._consider_capture(path, query_source=True,
                                               kind=IMAGE_KIND)
                        return False
            content = clipboard.wait_for_text()
        except Exception as exc:  # 剪贴板读取偶发失败（源应用退出等）
            log.debug("读取剪贴板失败：%s", exc)
            return False
        if content:
            if self._capture_image_file(content):
                return False
            self._consider_capture(content, query_source=True)
        return False

    def _capture_image_file(self, content: str) -> bool:
        """剪贴板文本为单个图片文件路径/URI 时，转存为图片条目。"""
        path = uri_to_path(content)
        if not path or not is_image_path(path):
            return False
        saved = save_image_file(path)
        if not saved:
            return False
        self._consider_capture(saved, query_source=True, kind=IMAGE_KIND)
        return True

    # ------------------------------------------------------------ Wayland
    def _start_wayland_watch(self) -> None:
        wl_paste = self.session.tool_path("wl-paste")
        if not wl_paste or not self.session.wl_data_control:
            reason = ("未安装 wl-clipboard（wl-paste）" if not wl_paste
                      else "当前合成器不支持 data-control 协议，后台无法读取剪贴板")
            self._report_degraded(reason)
            return
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        existing = os.environ.get("PYTHONPATH", "")
        envp = [f"{k}={v}" for k, v in os.environ.items() if k != "PYTHONPATH"]
        envp.append("PYTHONPATH="
                    + pkg_root + (os.pathsep + existing if existing else ""))
        argv = [wl_paste, "--watch", sys.executable, "-m", "clipv", "_ingest"]
        try:
            pid = GLib.spawn_async(
                argv, envp=envp,
                flags=GLib.SpawnFlags.DO_NOT_REAP_CHILD
                | GLib.SpawnFlags.SEARCH_PATH)[0]
        except GLib.Error as exc:
            self._report_degraded(f"启动 wl-paste 监听失败：{exc.message}")
            return
        self._watch_pid = pid
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, self._on_watch_exit)
        log.info("Wayland 剪贴板监听已启动（wl-paste --watch，pid=%d）", pid)

    def _on_watch_exit(self, pid: int, status: int) -> None:
        if self._stopping or pid != self._watch_pid:
            return
        self._watch_pid = 0
        code = os.waitstatus_to_exitcode(status)
        log.warning("wl-paste 监听进程退出（code=%s），%s 秒后重试",
                    code, int(self._watch_retry))
        self._watch_fail_count += 1
        if self._watch_fail_count <= 3 and code != 0:
            self._report_degraded("wl-paste 监听异常退出，剪贴板监听可能不可用")
        self._watch_restart_source = GLib.timeout_add_seconds(
            int(self._watch_retry), self._restart_watch)
        self._watch_retry = min(self._watch_retry * 2, 60.0)

    def _restart_watch(self) -> bool:
        self._watch_restart_source = 0
        if not self._stopping:
            self._start_wayland_watch()
        return False

    def _report_degraded(self, reason: str) -> None:
        message = f"剪贴板监听已降级：{reason}"
        log.warning("%s", message)
        if not self._degraded_emitted:
            self._degraded_emitted = True
            if self._on_degraded is not None:
                self._on_degraded(message)

    # ------------------------------------------------------------ 公共入口
    def _consider_capture(self, content: str, query_source: bool,
                          source_app: Optional[str] = None,
                          source_class: Optional[str] = None,
                          kind: str = "text") -> None:
        """外部内容（GTK 信号或 _ingest）统一去噪后上报。"""
        if content is None:
            return
        if not content.strip():
            return  # 纯空白无记录价值
        if self.is_self_write(content):
            log.debug("忽略自身写回内容（len=%d）", len(content))
            return
        if kind != IMAGE_KIND:
            max_bytes = int(self.config.get("history.max_item_kb")) * 1024
            content = truncate_text(content, max_bytes)

        if query_source and source_app is None and source_class is None \
                and self.x11 is not None:
            try:
                wid = self.x11.active_window()
                source_app, source_class = self.x11.window_info(wid)
            except Exception as exc:
                log.debug("获取来源窗口失败：%s", exc)

        ignore = self.config.get("ignore.apps")
        if source_class or source_app:
            hay = f"{source_app or ''} {source_class or ''}".lower()
            for pattern in ignore:
                if pattern and pattern in hay:
                    log.info("命中忽略列表，跳过该条复制（来源=%s）",
                             source_class or source_app)
                    return
        if self._on_captured is not None:
            self._on_captured(content, source_app, source_class, kind)

    def ingest_external(self, content: str, source_app: Optional[str] = None,
                        source_class: Optional[str] = None) -> None:
        """由 `clipv _ingest` / `clipv add` 等外部入口触发。"""
        self._consider_capture(content, query_source=False,
                               source_app=source_app, source_class=source_class)


def set_clipboard(content: str) -> None:
    """写回文本到系统剪贴板（调用前需先 mark_self_write）。"""
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(content, -1)
    clipboard.store()


def set_clipboard_image(path: str) -> bool:
    """写回图片到系统剪贴板（调用前需先 mark_self_write）。

    返回是否成功；图片文件缺失/损坏时返回 False。
    """
    pixbuf = load_pixbuf(path)
    if pixbuf is None:
        return False
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_image(pixbuf)
    clipboard.store()
    return True
