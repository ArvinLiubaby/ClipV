"""X11 能力封装（python-xlib）：全局热键注册、活动窗口信息、XTest 模拟粘贴。

本模块直接依赖 Xlib，仅在 X11 会话下使用。所有公开接口的异常均转为
X11Unavailable / HotkeyConflict，便于上层降级处理。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional, Tuple

from Xlib import X, XK, display as xdisplay
from Xlib import error as xerror
from Xlib import protocol
from Xlib.ext import xtest

from .config import parse_hotkey

log = logging.getLogger("clipv.x11")

_MOD_MASKS = {
    "shift": X.ShiftMask,
    "ctrl": X.ControlMask,
    "alt": X.Mod1Mask,
    "super": X.Mod4Mask,
}
#: 需要忽略的锁定修饰键（CapsLock / NumLock）组合
_IGNORE_COMBOS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
_ANY_PROP = X.AnyPropertyType
_ACTIVE_WINDOW = "_NET_ACTIVE_WINDOW"


class X11Unavailable(Exception):
    """X 连接不可用（无 DISPLAY 或连接失败）。"""


class HotkeyConflict(Exception):
    """快捷键已被其他客户端抢占（XGrabKey 返回 BadAccess）。"""


def mods_to_mask(mods: List[str]) -> int:
    mask = 0
    for mod in mods:
        mask |= _MOD_MASKS[mod]
    return mask


class X11:
    """持有独立 X 连接的轻量封装。"""

    def __init__(self):
        try:
            self.display = xdisplay.Display()
        except Exception as exc:  # DisplayNameError / ConnectionError 等
            raise X11Unavailable(f"无法连接 X 服务器：{exc}") from exc
        self.root = self.display.screen().root
        self._grabbed: Optional[Tuple[int, int]] = None  # (keycode, base_mask)

    # ------------------------------------------------------------ 事件循环
    def fileno(self) -> int:
        return self.display.fileno()

    def process_events(self, on_hotkey: Callable[[], None]) -> None:
        """处理挂起事件：命中已注册热键时回调（mapping 变化自动重注册）。"""
        while self.display.pending_events():
            event = self.display.next_event()
            if event.type == X.KeyPress and self._grabbed is not None:
                keycode, base_mask = self._grabbed
                clean = event.state & ~(X.LockMask | X.Mod2Mask)
                if event.detail == keycode and clean == base_mask:
                    try:
                        on_hotkey()
                    except Exception:
                        log.exception("热键回调执行失败")
            elif event.type == X.MappingNotify:
                self.display.refresh_keyboard_mapping(event)
                if self._grabbed is not None:
                    keycode, base_mask = self._grabbed
                    try:
                        self._grab(keycode, base_mask)
                    except HotkeyConflict as exc:
                        log.warning("键盘映射变化后重注册热键失败：%s", exc)

    # ------------------------------------------------------------ 热键
    def grab_hotkey(self, hotkey: str) -> None:
        """按快捷键字符串（如 'Super+V'）注册全局热键。"""
        mods, key = parse_hotkey(hotkey)
        # F1-F24 需要大写形式的 keysym 名（如 'F1'）；单字符键按原样
        keysym = XK.string_to_keysym(key.upper() if len(key) > 1 else key)
        if keysym == 0:
            raise X11Unavailable(f"无法解析按键：{key}")
        keycode = self.display.keysym_to_keycode(keysym)
        if keycode == 0:
            raise X11Unavailable(f"按键无对应键码：{key}")
        base_mask = mods_to_mask(mods)
        self.ungrab_hotkey()
        self._grab(keycode, base_mask)
        self._grabbed = (keycode, base_mask)
        log.info("全局热键已注册：%s", hotkey)

    def _grab(self, keycode: int, base_mask: int) -> None:
        conflicts: List[xerror.XError] = []

        def on_error(err, _request):
            conflicts.append(err)

        for extra in _IGNORE_COMBOS:
            self.root.grab_key(keycode, base_mask | extra, False,
                               X.GrabModeAsync, X.GrabModeAsync,
                               onerror=on_error)
        self.display.sync()
        if conflicts:
            first = conflicts[0]
            if isinstance(first, xerror.BadAccess):
                raise HotkeyConflict("快捷键已被其他程序占用")
            raise X11Unavailable(f"注册热键失败：{first}")

    def ungrab_hotkey(self) -> None:
        if self._grabbed is None:
            return
        keycode, base_mask = self._grabbed
        try:
            for extra in _IGNORE_COMBOS:
                self.root.ungrab_key(keycode, base_mask | extra,
                                     onerror=lambda _e, _r: None)
            self.display.sync()
        except xerror.XError:
            pass
        self._grabbed = None

    def hotkey_grab_available(self, hotkey: str) -> bool:
        """探测热键当前是否能被本连接抢占注册（返回 True 表示无其他持有者）。

        用于判断桌面环境是否“真正”持有某绑定：部分桌面在配置变更后
        可能注销绑定且未能重注册，此时探测会给出可抢占的结果。
        探测产生的临时 grab 会立即释放，不影响后续正式注册。
        """
        mods, key = parse_hotkey(hotkey)
        keysym = XK.string_to_keysym(key.upper() if len(key) > 1 else key)
        if keysym == 0:
            raise X11Unavailable(f"无法解析按键：{key}")
        keycode = self.display.keysym_to_keycode(keysym)
        if keycode == 0:
            raise X11Unavailable(f"按键无对应键码：{key}")
        base_mask = mods_to_mask(mods)
        conflicts: List[xerror.XError] = []

        def on_error(err, _request):
            conflicts.append(err)

        for extra in _IGNORE_COMBOS:
            self.root.grab_key(keycode, base_mask | extra, False,
                               X.GrabModeAsync, X.GrabModeAsync,
                               onerror=on_error)
        self.display.sync()
        for extra in _IGNORE_COMBOS:
            self.root.ungrab_key(keycode, base_mask | extra,
                                 onerror=lambda _e, _r: None)
        self.display.sync()
        return not conflicts

    # ------------------------------------------------------------ 窗口信息
    def active_window(self) -> Optional[int]:
        """返回当前活动窗口 id（_NET_ACTIVE_WINDOW），失败返回 None。"""
        try:
            prop = self.root.get_full_property(
                self.display.intern_atom(_ACTIVE_WINDOW), _ANY_PROP)
            if prop and prop.value:
                wid = int(prop.value[0])
                return wid or None
        except xerror.XError:
            pass
        return None

    def window_info(self, wid: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
        """返回窗口的 (显示名, 归一化类名)，如 ('code', 'code.code')。"""
        if not wid:
            return None, None
        try:
            win = self.display.create_resource_object("window", wid)
            wm_class = win.get_wm_class()  # (instance, class)
        except xerror.XError:
            return None, None
        if not wm_class:
            return None, None
        instance = wm_class[0] if len(wm_class) > 0 else None
        cls = wm_class[1] if len(wm_class) > 1 else instance
        display_name = (instance or cls or "").strip() or None
        norm = ".".join(x for x in (instance, cls) if x).lower() or None
        return display_name, norm

    def window_exists(self, wid: Optional[int]) -> bool:
        if not wid:
            return False
        try:
            win = self.display.create_resource_object("window", wid)
            win.get_attributes()
            return True
        except xerror.XError:
            return False

    def activate_window(self, wid: int) -> bool:
        """请求窗口管理器激活目标窗口（EWMH _NET_ACTIVE_WINDOW）。"""
        if not self.window_exists(wid):
            return False
        try:
            win = self.display.create_resource_object("window", wid)
            data = [2, X.CurrentTime, 0, 0, 0]
            event = protocol.event.ClientMessage(
                window=win,
                client_type=self.display.intern_atom(_ACTIVE_WINDOW),
                data=(32, data))
            self.root.send_event(
                event,
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            self.display.sync()
            # 兜底：部分 WM 不响应 client message，直接设置输入焦点
            self.display.set_input_focus(win, X.RevertToParent, X.CurrentTime)
            self.display.sync()
            return True
        except xerror.XError as exc:
            log.debug("激活窗口失败：%s", exc)
            return False

    def force_focus(self, wid: int) -> bool:
        """强制把输入焦点切到目标窗口（面板显示后的焦点兜底）。

        Gtk.Window.present() 依赖 WM 策略转移焦点，部分时序/配置下会被
        焦点窃取防护（FSP）拒绝，表现为面板可见但键盘事件仍发往原窗口。
        XSetInputFocus 是对 X 服务器的直接请求，不受 WM 策略限制。
        """
        if not self.window_exists(wid):
            return False
        try:
            win = self.display.create_resource_object("window", wid)
            data = [1, X.CurrentTime, 0, 0, 0]  # source=application
            event = protocol.event.ClientMessage(
                window=win,
                client_type=self.display.intern_atom(_ACTIVE_WINDOW),
                data=(32, data))
            self.root.send_event(
                event,
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            self.display.sync()
            self.display.set_input_focus(win, X.RevertToParent, X.CurrentTime)
            self.display.sync()
            return True
        except xerror.XError as exc:
            log.debug("强制聚焦失败：%s", exc)
            return False

    # ------------------------------------------------------------ 模拟按键
    def fake_ctrl_v(self) -> bool:
        """XTest 注入 Ctrl+V（无需 xdotool）。"""
        try:
            ctrl = self.display.keysym_to_keycode(XK.string_to_keysym("Control_L"))
            v = self.display.keysym_to_keycode(XK.string_to_keysym("v"))
            if not ctrl or not v:
                return False
            xtest.fake_input(self.display, X.KeyPress, ctrl)
            self.display.sync()
            time.sleep(0.01)
            xtest.fake_input(self.display, X.KeyPress, v)
            xtest.fake_input(self.display, X.KeyRelease, v)
            self.display.sync()
            time.sleep(0.01)
            xtest.fake_input(self.display, X.KeyRelease, ctrl)
            self.display.sync()
            return True
        except xerror.XError as exc:
            log.warning("XTest 注入 Ctrl+V 失败：%s", exc)
            return False

    def close(self) -> None:
        try:
            self.ungrab_hotkey()
        except Exception:
            pass
        try:
            self.display.close()
        except Exception:
            pass
