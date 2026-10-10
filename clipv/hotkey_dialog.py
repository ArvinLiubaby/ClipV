"""快捷键设置对话框：按键捕获（Super / Ctrl / Alt 组合）并交由调用方保存应用。

托盘菜单「设置快捷键…」的界面实现。on_save 回调由守护进程提供，
负责写入配置、更新桌面绑定（Cinnamon）并让新键立即生效。

交互约定：
- 打开时显示当前快捷键；按下的有效组合即时更新显示，按「保存」应用；
- 单独按修饰键仅提示继续按住；Esc 取消；Enter / Tab 放行给对话框本身。
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import config as cfg_mod

# “恢复默认”按钮的自定义响应码（Gtk.ResponseType 无对应值）
_RESPONSE_RESET = 1

# 无主键的修饰键 / 锁定键（单独按下时忽略）
_MODIFIER_KEYS = frozenset({
    Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
    Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
    Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock,
})

_FN_KEY_RE = re.compile(r"f([1-9]|1[0-9]|2[0-4])")

_TIP_IDLE = "按下新的快捷键组合（需包含 Super / Ctrl / Alt 之一）"
_TIP_HOLDING = "继续按住修饰键，再按一个主键（字母 / 数字 / F1–F24）"
_TIP_CAPTURED = "已捕获新快捷键，按「保存」应用"
_TIP_INVALID = "该组合不可用：需含 Super / Ctrl / Alt；主键限 字母 / 数字 / F1–F24"


def hotkey_from_event(keyval: int, state: int) -> Optional[str]:
    """将按键事件的 (keyval, state) 解析为规范化快捷键字符串；不支持返回 None。

    要求至少包含 Super / Ctrl / Alt 之一（避免无修饰单键成为全局热键），
    主键仅支持 字母 / 数字 / F1-F24（与 config.parse_hotkey 的能力一致）。
    """
    mods = []
    if state & Gdk.ModifierType.CONTROL_MASK:
        mods.append("ctrl")
    if state & Gdk.ModifierType.MOD1_MASK:
        mods.append("alt")
    if state & Gdk.ModifierType.SHIFT_MASK:
        mods.append("shift")
    if state & Gdk.ModifierType.SUPER_MASK:
        mods.append("super")
    if not ({"ctrl", "alt", "super"} & set(mods)):
        return None
    name = Gdk.keyval_name(keyval)
    if not name:
        return None
    low = name.lower()
    if len(low) == 1 and low.isalnum():
        key = low
    elif _FN_KEY_RE.fullmatch(low):
        key = low
    else:
        return None
    try:
        norm_mods, norm_key = cfg_mod.parse_hotkey(
            cfg_mod.format_hotkey(mods, key))
    except ValueError:
        return None
    return cfg_mod.format_hotkey(norm_mods, norm_key)


class HotkeyDialog(Gtk.Dialog):
    """捕获组合键的对话框；on_save(hotkey) 返回 (成功, 反馈信息)。"""

    def __init__(self, current: str,
                 on_save: Callable[[str], Tuple[bool, str]]) -> None:
        super().__init__(title="ClipV 快捷键设置")
        self._on_save = on_save
        self._value = current

        self.set_icon_name("clipv")
        self.set_default_size(440, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_modal(True)

        area = self.get_content_area()
        area.set_spacing(10)
        area.set_margin_top(14)
        area.set_margin_bottom(2)
        area.set_margin_start(20)
        area.set_margin_end(20)

        lead = Gtk.Label(label="面板呼出快捷键")
        lead.set_line_wrap(True)
        area.add(lead)

        self._value_label = Gtk.Label()
        self._value_label.set_margin_top(6)
        self._value_label.set_margin_bottom(6)
        self._value_label.set_selectable(True)
        area.add(self._value_label)

        note = Gtk.Label()
        note.set_markup("<small>支持：字母 / 数字 / F1–F24　·　Esc 取消</small>")
        area.add(note)

        self._status = Gtk.Label(label=_TIP_IDLE)
        self._status.set_line_wrap(True)
        self._status.set_xalign(0)
        area.add(self._status)

        self.add_button("恢复默认", _RESPONSE_RESET)
        self.add_button("取消", Gtk.ResponseType.CANCEL)
        self.add_button("保存", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        self.connect("key-press-event", self._on_key_press)
        self.connect("response", self._on_response)
        self._update_display()

    # ------------------------------------------------------------ 内部
    def _update_display(self) -> None:
        esc = GLib.markup_escape_text(self._value)
        self._value_label.set_markup(
            f"<span size='x-large' weight='bold'>{esc}</span>")

    def _on_key_press(self, _widget, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.response(Gtk.ResponseType.CANCEL)
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter,
                            Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            return False   # 放行：Enter 触发默认按钮（保存），Tab 切换焦点
        if event.keyval in _MODIFIER_KEYS:
            self._status.set_text(_TIP_HOLDING)
            return True
        combo = hotkey_from_event(event.keyval, event.state)
        if combo is None:
            self._status.set_text(_TIP_INVALID)
            return True
        self._value = combo
        self._status.set_text(_TIP_CAPTURED)
        self._update_display()
        return True

    def _on_response(self, _dialog, response: int) -> None:
        if response == _RESPONSE_RESET:             # 恢复默认：仅填入，待保存
            self._value = str(cfg_mod.DEFAULTS["hotkey"]["toggle"])
            self._status.set_text(_TIP_CAPTURED)
            self._update_display()
            self.stop_emission_by_name("response")
            return
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        ok, message = False, "未知错误"
        try:
            ok, message = self._on_save(self._value)
        except Exception as exc:                     # on_save 不应抛异常
            message = f"应用失败：{exc}"
        if ok:
            self.destroy()
        else:
            self._status.set_text(message)
            self.stop_emission_by_name("response")   # 应用失败时保持打开
