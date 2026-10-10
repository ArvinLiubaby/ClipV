"""通用工具：日志、时间格式化、文本预览、系统通知。"""
from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import subprocess
import time
from typing import Optional

from . import __app_name__

log = logging.getLogger("clipv")

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_file: Optional[str] = None, verbose: bool = False,
                  to_stderr: bool = True) -> None:
    """初始化日志。日志中不记录剪贴板内容（F-74）。"""
    root = logging.getLogger("clipv")
    if root.handlers:
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)

    if to_stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        root.addHandler(sh)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            fh.setFormatter(formatter)
            fh.setLevel(logging.DEBUG if verbose else logging.INFO)
            root.addHandler(fh)
        except OSError:  # 日志文件不可写不应影响主流程
            pass


def truncate_text(text: str, max_bytes: int) -> str:
    """按字节上限截断文本，附加截断标记。"""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    cut = raw[:max_bytes].decode("utf-8", errors="ignore")
    return cut + "\n…[内容过长已截断]"


def preview_line(text: str, max_chars: int = 200) -> str:
    """把多行文本折叠为单行预览。"""
    line = " ".join(seg.strip() for seg in text.splitlines() if seg.strip())
    if len(line) > max_chars:
        line = line[:max_chars] + "…"
    return line


def tooltip_text(text: str, max_chars: int = 800) -> str:
    """条目悬浮提示：展示更多内容（不注入 HTML）。"""
    shown = text if len(text) <= max_chars else text[:max_chars] + "\n…"
    return shown.replace("&", "&amp;").replace("<", "&lt;")


def relative_time(ts: float, now: Optional[float] = None) -> str:
    """相对时间描述：刚刚 / N 分钟前 / HH:MM / 昨天 HH:MM / 月-日。"""
    now = time.time() if now is None else now
    delta = max(0.0, now - ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    lt = time.localtime(ts)
    if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("%H:%M", lt)
    yesterday = now - 86400
    if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d", time.localtime(yesterday)):
        return time.strftime("昨天 %H:%M", lt)
    if lt.tm_year == time.localtime(now).tm_year:
        return time.strftime("%m-%d %H:%M", lt)
    return time.strftime("%Y-%m-%d %H:%M", lt)


def send_notification(title: str, body: str) -> bool:
    """通过 notify-send 发送桌面通知（不存在则忽略）。"""
    if not shutil.which("notify-send"):
        return False
    try:
        subprocess.Popen(
            ["notify-send", "-a", __app_name__, "-t", "4000", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
