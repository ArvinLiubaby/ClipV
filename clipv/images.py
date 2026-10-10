"""剪贴板图片存储：PNG 落盘（内容哈希去重）、尺寸解析、URI 识别、垃圾清理。

设计约束：模块顶层不导入 GUI 库（CLI 的快路径可能引入本模块），
需要 GdkPixbuf 的操作在函数内延迟导入。
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse

from .config import data_dir, ensure_dir

log = logging.getLogger("clipv.images")

IMAGE_KIND = "image"
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")
_MAX_SOURCE_BYTES = 20 * 1024 * 1024  # 单个源图片文件转存上限


def images_dir() -> str:
    """图片仓库目录：$XDG_DATA_HOME/clipv/images。"""
    return os.path.join(data_dir(), "images")


def is_image_path(path: str) -> bool:
    return bool(path) and path.lower().endswith(_IMAGE_EXTS)


def uri_to_path(text: str) -> Optional[str]:
    """把 file:// URI 或绝对路径文本转为本地路径（非法/不存在返回 None）。"""
    text = text.strip()
    if not text or "\n" in text:
        return None
    if text.startswith("file://"):
        parsed = urlparse(text)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            return None
        path = unquote(parsed.path)
    elif text.startswith("/"):
        path = text
    else:
        return None
    return path if os.path.isfile(path) else None


def png_size(path: str) -> Optional[Tuple[int, int]]:
    """解析 PNG 头（IHDR）返回 (宽, 高)；非 PNG 或损坏返回 None。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if (len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n"
            or head[12:16] != b"IHDR"):
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _encode_png(pixbuf) -> Optional[bytes]:
    try:
        ok, buf = pixbuf.save_to_bufferv("png", [], [])
    except Exception as exc:
        log.debug("PNG 编码失败：%s", exc)
        return None
    if not ok or buf is None:
        return None
    if isinstance(buf, (bytes, bytearray, memoryview)):
        return bytes(buf)
    try:  # PyGObject 的 GBytes（支持缓冲协议）
        return bytes(buf)
    except TypeError:
        try:
            return bytes(buf.get_data())
        except Exception:  # noqa: BLE001 - 防御不同 PyGObject 版本
            return None


def save_pixbuf_as_png(pixbuf) -> Optional[str]:
    """把 GdkPixbuf 编码为 PNG 并存入图片目录（按内容哈希去重）。

    返回文件路径；编码/写入失败返回 None。
    """
    raw = _encode_png(pixbuf)
    if not raw:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    directory = images_dir()
    ensure_dir(directory)
    path = os.path.join(directory, f"{digest}.png")
    if os.path.exists(path):
        return path
    try:
        with open(path, "wb") as fh:
            fh.write(raw)
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("写入图片失败：%s", exc)
        return None
    log.debug("图片已存储（%d 字节）：%s", len(raw), path)
    return path


def save_image_file(path: str) -> Optional[str]:
    """读取本地图片文件并转存为 PNG（覆盖"复制图片文件"的场景）。"""
    try:
        if os.path.getsize(path) > _MAX_SOURCE_BYTES:
            log.info("图片文件过大（>%dMB），跳过转存：%s",
                     _MAX_SOURCE_BYTES // (1024 * 1024), path)
            return None
    except OSError:
        return None
    pixbuf = load_pixbuf(path)
    if pixbuf is None:
        return None
    return save_pixbuf_as_png(pixbuf)


def load_pixbuf(path: str, max_edge: int = 0):
    """加载图片为 GdkPixbuf；max_edge > 0 时等比缩放到该边长内。

    加载失败（文件缺失/损坏/无 GdkPixbuf）返回 None。
    """
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return None
    try:
        if max_edge > 0:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, max_edge, max_edge, True)
        return GdkPixbuf.Pixbuf.new_from_file(path)
    except Exception as exc:
        log.debug("加载图片失败（%s）：%s", path, exc)
        return None


def gc_orphan_images(store) -> int:
    """删除图片目录中不再被任何历史条目引用的 PNG 文件。

    参数 store 为 Store 实例（鸭子类型，仅需 .list()）。
    返回删除的文件数。
    """
    directory = images_dir()
    if not os.path.isdir(directory):
        return 0
    referenced = set()
    for entry in store.list():
        if entry.kind == IMAGE_KIND:
            try:
                referenced.add(os.path.realpath(entry.content))
            except OSError:
                pass
    removed = 0
    for name in os.listdir(directory):
        if not name.endswith(".png"):
            continue
        full = os.path.join(directory, name)
        if os.path.realpath(full) in referenced:
            continue
        try:
            os.unlink(full)
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("已清理 %d 个未引用的图片文件", removed)
    return removed
