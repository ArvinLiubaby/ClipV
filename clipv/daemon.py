"""ClipV 守护进程：剪贴板监听 + 全局热键 + 面板 + IPC 的编排层（GTK3/GLib）。"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import __app_name__, __version__, config as cfg_mod, ipc, session as session_mod
from . import tray as tray_mod
from .clipboard import ClipboardMonitor, set_clipboard, set_clipboard_image
from .config import Config
from .images import IMAGE_KIND, gc_orphan_images
from .panel import ClipvPanel
from .paste import paste_ctrl_v
from .store import Store, open_store
from .util import send_notification, setup_logging

log = logging.getLogger("clipv.daemon")

_QUIT_GRACE_MS = 200        # 先回响应再退出
_PASTE_FOCUS_DELAY_MS = 90  # 激活目标窗口后等待焦点切换
_FOCUS_CHECK_DELAY_MS = 120  # 面板显示后校验输入焦点的延迟
_IMAGE_GC_INTERVAL_S = 60    # 孤儿图片文件清理周期


class Daemon:
    def __init__(self, detached: bool = False):
        self.detached = detached
        self.loop = GLib.MainLoop()
        self.config: Config = Config.load()
        self.session = session_mod.detect()
        self.store: Store = open_store(self.config)
        self.x11 = None
        self._x11_source = 0
        self._config_source = 0
        self._gc_source = 0
        self._signal_sources: List[int] = []
        self._shutting_down = False
        self._target_wid: Optional[int] = None
        self._hotkey_warned = False
        self._hotkey_owner: Optional[str] = None  # de / self / conflict

        if self.session.is_x11:
            self._init_x11()

        self.monitor = ClipboardMonitor(
            self.session, self.config, x11=self.x11,
            on_captured=self._on_captured, on_degraded=self._on_degraded)
        self.panel = ClipvPanel(self.store, self.config,
                                on_activate=self._on_activated)
        self.ipc_server = ipc.IpcServer(cfg_mod.socket_path(),
                                        self._ipc_handler_thread)
        self.tray: Optional[tray_mod.TrayIcon] = None
        self._hotkey_dialog: Optional[Gtk.Dialog] = None
        self._about_dialog: Optional[Gtk.Dialog] = None

    # ------------------------------------------------------------ 初始化
    def _init_x11(self) -> None:
        from .x11util import X11, X11Unavailable
        try:
            self.x11 = X11()
        except X11Unavailable as exc:
            log.warning("X11 连接失败，热键/来源应用/自动粘贴降级：%s", exc)
            self.x11 = None

    def start(self) -> bool:
        if self.session.session_type == "none":
            log.error("未检测到图形会话（DISPLAY/WAYLAND_DISPLAY 均未设置）")
            print("错误：未检测到图形会话，无法启动。请运行 `clipv doctor` 查看诊断。",
                  file=sys.stderr)
            return False
        try:
            self.ipc_server.start()
        except ipc.IpcAlreadyRunning as exc:
            print(f"{exc}", file=sys.stderr)
            return False

        self._install_signals()
        self._grab_hotkey()
        if self.x11 is not None:
            self._x11_source = GLib.io_add_watch(
                self.x11.fileno(), GLib.IOCondition.IN, self._on_x11_events)
        self.monitor.start()
        self.tray = tray_mod.create_tray(
            on_show=self.show_panel, on_quit=self._schedule_quit,
            on_settings=self.show_hotkey_dialog,
            on_about=self.show_about_dialog)
        self._config_source = GLib.timeout_add_seconds(2, self._on_config_tick)
        self._gc_images_tick()  # 启动时立即清理孤儿图片
        self._gc_source = GLib.timeout_add_seconds(
            _IMAGE_GC_INTERVAL_S, self._gc_images_tick)
        log.info("%s %s 已启动（会话=%s，桌面=%s，自动粘贴=%s）",
                 __app_name__, __version__, self.session.session_type,
                 self.session.desktop or "未知",
                 self.config.get("paste.auto_paste"))
        return True

    def _grab_hotkey(self) -> None:
        if self.x11 is None:
            return
        from .x11util import HotkeyConflict, X11Unavailable
        hotkey = self.config.get("hotkey.toggle")
        # 桌面环境已把该热键绑定到 `clipv toggle`（如 shortcut install 写入
        # 的 Cinnamon 快捷键）时优先让 DE 处理；但需实测 DE 是否真正持有
        # 该热键——部分桌面在绑定变更后会注销却未能重注册（空窗），
        # 此时由守护进程接管注册，保证物理按键始终可用
        de_bound = False
        try:
            from .cli import de_binds_toggle
            de_bound = de_binds_toggle(hotkey)
        except Exception:
            log.debug("桌面环境快捷键检测失败", exc_info=True)
        if de_bound:
            available = False
            try:
                available = self.x11.hotkey_grab_available(hotkey)
            except X11Unavailable as exc:
                log.warning("热键占用探测失败：%s", exc)
            if available:
                log.info("桌面环境绑定 %s 未实际生效，由守护进程接管注册", hotkey)
            else:
                if self._hotkey_owner == "self":
                    self.x11.ungrab_hotkey()
                self._hotkey_owner = "de"
                log.info("桌面环境已绑定 %s → clipv toggle，跳过自主注册", hotkey)
                return
        try:
            self.x11.grab_hotkey(hotkey)
            self._hotkey_owner = "self"
        except HotkeyConflict:
            self._hotkey_warned = True
            self._hotkey_owner = "conflict"
            note = (f"全局快捷键 {hotkey} 已被其他程序占用。"
                    "可运行 `clipv shortcut install` 重绑，"
                    "或在桌面环境快捷键里绑定 `clipv toggle`。")
            log.warning("%s", note)
            send_notification("ClipV", note)
        except X11Unavailable as exc:
            self._hotkey_owner = None
            log.warning("注册全局快捷键失败：%s", exc)

    # ------------------------------------------------------------ 快捷键设置
    def show_hotkey_dialog(self) -> bool:
        """打开快捷键设置对话框（托盘菜单 / `clipv shortcut edit` 入口）。

        对话框已打开时置顶聚焦；返回是否已成功显示。
        """
        if self._hotkey_dialog is not None:
            self._hotkey_dialog.present()
            return True
        try:
            from .hotkey_dialog import HotkeyDialog
            dialog = HotkeyDialog(self.config.get("hotkey.toggle"),
                                  on_save=self._apply_hotkey_setting)
            dialog.connect("destroy",
                           lambda *_: setattr(self, "_hotkey_dialog", None))
            self._hotkey_dialog = dialog
            dialog.show_all()
            dialog.present()
            return True
        except Exception:
            self._hotkey_dialog = None
            log.exception("打开快捷键设置对话框失败")
            return False

    def show_about_dialog(self) -> bool:
        """打开关于对话框（托盘菜单「关于 ClipV」/ `clipv about` 入口）。

        对话框已打开时置顶聚焦；返回是否已成功显示。
        """
        if self._about_dialog is not None:
            self._about_dialog.present()
            return True
        try:
            from .about_dialog import AboutDialog
            dialog = AboutDialog()
            dialog.connect("destroy",
                           lambda *_: setattr(self, "_about_dialog", None))
            self._about_dialog = dialog
            dialog.show_all()
            dialog.present()
            return True
        except Exception:
            self._about_dialog = None
            log.exception("打开关于对话框失败")
            return False

    def _apply_hotkey_setting(self, hotkey: str) -> Tuple[bool, str]:
        """应用新快捷键：写配置 → (Cinnamon) 更新桌面绑定 → 立即重新注册。

        返回 (是否成功, 反馈信息)，失败信息由对话框展示。
        """
        try:
            normalized = self.config.set("hotkey.toggle", hotkey)
        except Exception as exc:
            return False, f"保存配置失败：{exc}"

        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        de_msg = None
        if "cinnamon" in desktop and shutil.which("gsettings"):
            try:
                from .cli import _clipv_command, _shortcut_install_cinnamon
                rc = _shortcut_install_cinnamon(normalized, _clipv_command())
            except Exception:
                log.exception("写入桌面快捷键失败")
                rc = 1
            if rc != 0:
                de_msg = ("已保存配置，但写入桌面快捷键失败"
                          "（可重试，或运行 `clipv shortcut install`）")
        elif not self.session.is_x11:
            # Wayland 非 Cinnamon：无法程序化绑定桌面热键
            de_msg = (f"已保存快捷键 {normalized}，但当前桌面无法自动绑定；"
                      "请在系统设置中把 `clipv toggle` 绑定到该组合")

        self._apply_config(force=True)   # 立即按新键重新注册/接管（不等轮询）
        if de_msg is not None:
            return False, de_msg
        send_notification("ClipV", f"快捷键已更新：{normalized}")
        return True, normalized

    def _install_signals(self) -> None:
        def on_signal():
            # 信号在主循环中直接回调（GLib unix signal source），退出安全
            self._schedule_quit()
            return GLib.SOURCE_REMOVE
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._signal_sources.append(
                    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, on_signal))
            except Exception:
                log.debug("注册信号处理失败：%s", sig)

    # ------------------------------------------------------------ 热键
    def _on_x11_events(self, _fd, _condition) -> bool:
        assert self.x11 is not None
        self.x11.process_events(self.toggle_panel)
        return True

    def toggle_panel(self) -> None:
        if self.panel.is_visible():
            self.panel.hide_panel()
            return
        self.show_panel()

    def show_panel(self) -> None:
        # 记录目标窗口必须在面板取得焦点之前（供自动粘贴时恢复）
        if self.x11 is not None:
            try:
                self._target_wid = self.x11.active_window()
            except Exception:
                self._target_wid = None
        self.panel.show_at_cursor()
        # 兜底：present() 被 WM 焦点窃取防护拒绝时，X11 层强制聚焦
        GLib.timeout_add(_FOCUS_CHECK_DELAY_MS, self._ensure_panel_focus)

    def _ensure_panel_focus(self) -> bool:
        """面板显示后校验输入焦点；仍留在原窗口时强制聚焦。"""
        if self.x11 is None or not self.panel.is_visible():
            return False
        try:
            wid = self.panel.xid()
            if not wid:
                return False
            if self.x11.active_window() != wid:
                if self.x11.force_focus(wid):
                    log.info("面板未获得焦点，已强制聚焦（wid=%s）", hex(wid))
        except Exception:
            log.debug("面板焦点校验失败", exc_info=True)
        return False

    # ------------------------------------------------------------ 剪贴板
    def _on_captured(self, content: str, source_app, source_class,
                     kind: str = "text") -> None:
        entry, action = self.store.add(content, source_app, source_class,
                                       kind=kind)
        log.debug("记录剪贴板（%s，kind=%s，len=%d，id=%d）",
                  action, kind, len(content), entry.id)
        if self.panel.is_visible():
            self.panel.reload()

    def _on_degraded(self, message: str) -> None:
        send_notification("ClipV", message)

    # ------------------------------------------------------------ 面板确认
    def _on_activated(self, entry, want_paste: bool) -> None:
        # 写回剪贴板（先抑制自身写入，避免被监听回环）
        self.monitor.mark_self_write(entry.content)
        if entry.kind == IMAGE_KIND:
            if not set_clipboard_image(entry.content):
                log.warning("图片写回失败（文件缺失/损坏）：%s", entry.content)
                send_notification("ClipV", "图片文件已不存在，无法复制")
                return
        else:
            set_clipboard(entry.content)
        # 置顶（kind 必须随条目保留，避免图片条目退回文本）
        self.store.add(entry.content, entry.source_app, entry.source_class,
                       kind=entry.kind)

        if not want_paste or not self.config.get("paste.auto_paste"):
            log.debug("已写回剪贴板（自动粘贴关闭或仅复制）")
            return
        delay = int(self.config.get("paste.delay_ms"))
        GLib.timeout_add(delay, self._do_paste_oneshot)

    def _do_paste_oneshot(self) -> bool:
        self._do_paste()
        return False

    def _do_paste(self) -> None:
        """X11：先激活原窗口再注入；Wayland：直接注入（合成器决定焦点）。"""
        if self.session.is_x11 and self.x11 is not None and self._target_wid:
            if self.x11.window_exists(self._target_wid):
                self.x11.activate_window(self._target_wid)
                GLib.timeout_add(_PASTE_FOCUS_DELAY_MS, self._inject_oneshot)
                return
        self._inject_ctrl_v()

    def _inject_oneshot(self) -> bool:
        self._inject_ctrl_v()
        return False

    def _inject_ctrl_v(self) -> None:
        result = paste_ctrl_v(
            self.session, self.x11 if self.session.is_x11 else None)
        if result.ok:
            log.debug("自动粘贴完成（%s）", result.method)
        else:
            log.warning("自动粘贴不可用：%s", result.detail)
            send_notification(
                "ClipV", "已复制到剪贴板；自动粘贴不可用，请手动 Ctrl+V")

    # ------------------------------------------------------------ IPC
    def _ipc_handler_thread(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """IPC 服务线程的请求经 idle 回调切到主线程执行。"""
        box: Dict[str, Any] = {}
        event = threading.Event()
        GLib.idle_add(self._process_ipc_request, request, box, event)
        if not event.wait(timeout=8.0):
            return {"ok": False, "error": "请求处理超时"}
        return box.get("response", {"ok": False, "error": "无响应"})

    def _process_ipc_request(self, request, box, event) -> bool:
        try:
            box["response"] = self._dispatch(request)
        except Exception as exc:
            log.exception("处理请求失败：%s", request.get("cmd"))
            box["response"] = {"ok": False, "error": str(exc)}
        finally:
            event.set()
        return False

    def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        cmd = request.get("cmd", "")
        if cmd == "ping":
            return {"ok": True, "data": {"pid": os.getpid(),
                                         "version": __version__}}
        if cmd == "toggle":
            self.toggle_panel()
            return {"ok": True}
        if cmd == "show":
            self.show_panel()
            return {"ok": True}
        if cmd == "hide":
            self.panel.hide_panel()
            return {"ok": True}
        if cmd == "hotkey-dialog":
            return ({"ok": True} if self.show_hotkey_dialog()
                    else {"ok": False, "error": "打开快捷键设置对话框失败"})
        if cmd == "about-dialog":
            return ({"ok": True} if self.show_about_dialog()
                    else {"ok": False, "error": "打开关于对话框失败"})
        if cmd == "status":
            total, pinned = self.store.stats()
            return {"ok": True, "data": {
                "pid": os.getpid(),
                "session": self.session.session_type,
                "desktop": self.session.desktop,
                "panel_visible": self.panel.is_visible(),
                "total": total, "pinned": pinned,
                "hotkey": self.config.get("hotkey.toggle"),
                "hotkey_conflict": self._hotkey_warned,
                "hotkey_owner": self._hotkey_owner,
                "auto_paste": self.config.get("paste.auto_paste"),
                "tray": self.tray.backend if self.tray is not None else "none",
            }}
        if cmd == "add":
            content = request.get("content", "")
            self.monitor.ingest_external(
                content, source_app=request.get("source_app"),
                source_class=request.get("source_class"))
            return {"ok": True}
        if cmd == "clear":
            if request.get("all"):
                removed = self.store.clear_all()
            else:
                removed = self.store.clear_unpinned()
            if self.panel.is_visible():
                self.panel.reload()
            self._gc_images_tick()  # 清理被清空条目的图片文件
            return {"ok": True, "data": {"removed": removed}}
        if cmd == "list":
            entries = self.store.list(query=request.get("query"))
            return {"ok": True, "data": [
                {"id": e.id, "kind": e.kind, "content": e.content,
                 "pinned": e.pinned, "captured_at": e.captured_at,
                 "source_app": e.source_app}
                for e in entries]}
        if cmd == "reload":
            self._apply_config(force=True)
            return {"ok": True}
        if cmd == "quit":
            GLib.timeout_add(_QUIT_GRACE_MS, self._quit_oneshot)
            return {"ok": True}
        return {"ok": False, "error": f"未知命令：{cmd}"}

    def _quit_oneshot(self) -> bool:
        self._schedule_quit()
        return False

    def _on_config_tick(self) -> bool:
        self._apply_config()
        return True

    def _gc_images_tick(self) -> bool:
        """周期清理不再被历史条目引用的图片文件。"""
        try:
            gc_orphan_images(self.store)
        except Exception:
            log.debug("图片清理失败", exc_info=True)
        return True

    def _apply_config(self, force: bool = False) -> None:
        changed = self.config.reload_if_changed() or force
        if not changed:
            return
        if self.config.load_error:
            log.warning("%s", self.config.load_error)
        self.store.max_items = self.config.get("history.max_items")
        self.panel.refresh_theme()
        if self.panel.is_visible():
            self.panel.reload()
        if self.x11 is not None:
            self._grab_hotkey()
        log.info("配置已重新加载")

    # ------------------------------------------------------------ 退出
    def _schedule_quit(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("正在退出…")
        try:
            if self._config_source:
                GLib.source_remove(self._config_source)
                self._config_source = 0
            if self._x11_source:
                GLib.source_remove(self._x11_source)
                self._x11_source = 0
            if self._gc_source:
                GLib.source_remove(self._gc_source)
                self._gc_source = 0
            self.monitor.stop()
            if self.tray is not None:
                self.tray.destroy()
                self.tray = None
            self.ipc_server.stop()
            if self.x11 is not None:
                self.x11.close()
            self.store.close()
        finally:
            self.loop.quit()


def run_daemon(detached: bool = False, verbose: bool = False) -> int:
    """守护进程入口（`clipv daemon`）。

    后台化（--detached）由 cli.cmd_daemon 以“重新 exec 一个全新解释器
    进程”完成（fork 之后继续初始化 GTK 在部分环境会死锁），
    进入本函数时进程已是独立后台进程。
    """
    setup_logging(log_file=cfg_mod.log_path(), verbose=verbose,
                  to_stderr=not detached)
    ok, _argv = Gtk.init_check()
    if not ok:
        print("错误：无法初始化 GTK（无图形会话？）。"
              "请运行 `clipv doctor` 查看诊断。", file=sys.stderr)
        return 1
    daemon = Daemon(detached=detached)
    if not daemon.start():
        return 1
    try:
        daemon.loop.run()
    except KeyboardInterrupt:
        pass
    return 0
