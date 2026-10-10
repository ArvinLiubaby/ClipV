"""自动粘贴策略：按平台/工具可用性选择注入 Ctrl+V 的方式。

优先级：
- X11：内置 XTest（无需外部工具）→ xdotool 兜底；
- Wayland：ydotool（uinput，支持 GNOME/KDE/wlroots）→ wtype（仅 wlroots）。
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .session import SessionInfo

log = logging.getLogger("clipv.paste")


@dataclass
class PasteResult:
    ok: bool
    method: str = ""
    detail: str = ""


def _run(cmd: List[str], timeout: float = 5.0) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("命令执行失败 %s：%s", cmd[0], exc)
        return False


def paste_ctrl_v(session: SessionInfo, x11=None) -> PasteResult:
    """注入 Ctrl+V。x11 为已连接的 X11 封装（X11 会话时由调用方传入）。"""
    if session.is_x11:
        if x11 is not None and x11.fake_ctrl_v():
            return PasteResult(True, "XTest", "内置 XTest 注入成功")
        if session.has_tool("xdotool"):
            if _run([session.tool_path("xdotool"), "key", "--clearmodifiers", "ctrl+v"]):  # type: ignore[list-item]
                return PasteResult(True, "xdotool", "xdotool 注入成功")
        return PasteResult(False, "", "X11 下无可用注入方式")

    # ---- Wayland ----
    ydotool = session.tool_path("ydotool")
    if ydotool:
        # ydotool 1.x 使用数字键码：29=Ctrl，47=V
        if _run([ydotool, "key", "29:1", "47:1", "47:0", "29:0"]):
            return PasteResult(True, "ydotool", "ydotool 注入成功")
        # ydotool 0.x 兼容写法
        if _run([ydotool, "key", "ctrl+v"]):
            return PasteResult(True, "ydotool", "ydotool（旧版语法）注入成功")
    wtype = session.tool_path("wtype")
    if wtype:
        if _run([wtype, "-M", "ctrl", "v", "-m", "ctrl"]):
            return PasteResult(True, "wtype", "wtype 注入成功")
    return PasteResult(False, "", "Wayland 下需安装 ydotool（推荐）或 wtype 且合成器支持")


def describe_paste_methods(session: SessionInfo) -> List[Tuple[str, bool, str]]:
    """供 doctor 展示：(方式, 是否可用, 说明)。"""
    items: List[Tuple[str, bool, str]] = []
    if session.is_x11:
        items.append(("XTest（内置）", True, "python-xlib XTest 扩展，无需额外安装"))
        items.append(("xdotool", session.has_tool("xdotool"),
                      "可选兜底，未安装不影响" if not session.has_tool("xdotool") else "已安装"))
    elif session.is_wayland:
        items.append(("ydotool", session.has_tool("ydotool"),
                      "需安装并运行 ydotoold；支持 GNOME/KDE/wlroots"))
        items.append(("wtype", session.has_tool("wtype"),
                      "仅支持 wlroots 系合成器（Sway/Hyprland 等）"))
    return items
