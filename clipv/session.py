"""会话环境探测：登录会话类型、桌面环境、外部工具可用性、Wayland 能力探测。"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Optional

log = logging.getLogger("clipv.session")

_TOOLS = ("xdotool", "wmctrl", "wl-paste", "wl-copy", "ydotool", "wtype",
          "notify-send", "gsettings")


@dataclass
class SessionInfo:
    session_type: str            # 'x11' | 'wayland' | 'none'
    desktop: str                 # XDG_CURRENT_DESKTOP，如 X-Cinnamon / GNOME
    display: Optional[str]       # DISPLAY
    wayland_display: Optional[str]
    tools: Dict[str, Optional[str]] = field(default_factory=dict)
    wl_data_control: Optional[bool] = None   # Wayland 剪贴板协议能力（None=未探测/不适用）

    @property
    def is_x11(self) -> bool:
        return self.session_type == "x11"

    @property
    def is_wayland(self) -> bool:
        return self.session_type == "wayland"

    def has_tool(self, name: str) -> bool:
        return bool(self.tools.get(name))

    def tool_path(self, name: str) -> Optional[str]:
        return self.tools.get(name)


def detect() -> SessionInfo:
    display_env = os.environ.get("DISPLAY") or None
    wayland_env = os.environ.get("WAYLAND_DISPLAY") or None
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()

    if session_type not in ("x11", "wayland"):
        if wayland_env:
            session_type = "wayland"
        elif display_env:
            session_type = "x11"
        else:
            session_type = "none"

    tools = {name: shutil.which(name) for name in _TOOLS}
    info = SessionInfo(
        session_type=session_type,
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", ""),
        display=display_env,
        wayland_display=wayland_env,
        tools=tools,
    )
    if info.is_wayland:
        info.wl_data_control = probe_wl_data_control(tools.get("wl-paste"))
    return info


def probe_wl_data_control(wl_paste: Optional[str]) -> bool:
    """探测 Wayland 合成器是否支持 data-control（后台应用读取剪贴板的前提）。

    探测方法：后台（无焦点）执行 `wl-paste --list-types`。
    - 支持 data-control：立即返回（有/无内容都算支持）；
    - 仅支持 wl_data_device（如旧版 GNOME）：无焦点会阻塞等待 → 超时判为不支持。
    """
    if not wl_paste:
        return False
    try:
        proc = subprocess.run(
            [wl_paste, "--list-types"],
            capture_output=True, text=True, timeout=2.0)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    stderr = (proc.stderr or "").lower()
    if "not support" in stderr or "data-control" in stderr:
        return False
    return True
