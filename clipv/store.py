"""历史记录存储：SQLite（WAL），支持去重置顶、固定、容量清理。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import Config, default_db_path, ensure_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL DEFAULT 'text',
    content      TEXT    NOT NULL,
    hash         TEXT    NOT NULL,
    pinned       INTEGER NOT NULL DEFAULT 0,
    captured_at  REAL    NOT NULL,
    source_app   TEXT,
    source_class TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_captured ON entries(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_hash     ON entries(hash);
"""


@dataclass
class Entry:
    id: int
    kind: str
    content: str
    pinned: bool
    captured_at: float
    source_app: Optional[str]
    source_class: Optional[str]

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Entry":
        return Entry(
            id=row["id"], kind=row["kind"], content=row["content"],
            pinned=bool(row["pinned"]), captured_at=row["captured_at"],
            source_app=row["source_app"], source_class=row["source_class"])


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def search_text(entry: Entry) -> str:
    """条目参与搜索匹配的文本（已 casefold）。

    图片条目的 content 为文件路径，用"图片/image"作为可搜索别名。
    """
    if entry.kind == "image":
        return "图片 image"
    return entry.content.casefold()


class Store:
    def __init__(self, path: Optional[str] = None, max_items: int = 100):
        self.path = path or default_db_path()
        self.max_items = max_items
        ensure_dir(os.path.dirname(self.path))
        self.conn = sqlite3.connect(self.path, timeout=3.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=3000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        try:  # F-73：数据文件仅当前用户可读写
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------ 写入
    def add(self, content: str, source_app: Optional[str] = None,
            source_class: Optional[str] = None, kind: str = "text"
            ) -> Tuple[Entry, str]:
        """新增历史。同一内容（全局哈希去重，F-03）已存在时移动到顶部。

        返回 (entry, action)，action 为 'created' 或 'promoted'。
        """
        digest = content_hash(content)
        now = time.time()
        with self.conn:
            rows = self.conn.execute(
                "SELECT pinned FROM entries WHERE hash = ?", (digest,)).fetchall()
            pinned = any(bool(r["pinned"]) for r in rows)
            if rows:
                self.conn.execute("DELETE FROM entries WHERE hash = ?", (digest,))
                action = "promoted"
            else:
                action = "created"
            cur = self.conn.execute(
                "INSERT INTO entries(kind, content, hash, pinned, captured_at,"
                " source_app, source_class) VALUES(?,?,?,?,?,?,?)",
                (kind, content, digest, 1 if pinned else 0, now,
                 source_app, source_class))
            entry_id = cur.lastrowid
            self._prune()
        return self.get(entry_id), action  # type: ignore[return-value]

    def _prune(self) -> int:
        """仅清理非固定条目，保留最近 max_items 条（F-10/F-14）。"""
        cur = self.conn.execute(
            "DELETE FROM entries WHERE pinned = 0 AND id NOT IN "
            "(SELECT id FROM entries WHERE pinned = 0 ORDER BY captured_at DESC LIMIT ?)",
            (self.max_items,))
        return cur.rowcount

    def set_pinned(self, entry_id: int, pinned: bool) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE entries SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, entry_id))
        if cur.rowcount and not pinned:
            with self.conn:
                self._prune()
        return bool(cur.rowcount)

    def delete(self, entry_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        return bool(cur.rowcount)

    def clear_unpinned(self) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM entries WHERE pinned = 0")
        return cur.rowcount

    def clear_all(self) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM entries")
        return cur.rowcount

    # ------------------------------------------------------------ 读取
    def get(self, entry_id: int) -> Optional[Entry]:
        row = self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return Entry.from_row(row) if row else None

    def list(self, query: Optional[str] = None) -> List[Entry]:
        """固定组置顶 + 组内时间倒序，其余时间倒序（F-12 修订）。"""
        rows = self.conn.execute(
            "SELECT * FROM entries ORDER BY pinned DESC, captured_at DESC").fetchall()
        entries = [Entry.from_row(r) for r in rows]
        if query:
            q = query.casefold()
            entries = [e for e in entries if q in search_text(e)]
        return entries

    def stats(self) -> Tuple[int, int]:
        total = self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        pinned = self.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE pinned = 1").fetchone()[0]
        return total, pinned

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


def open_store(config: Config) -> Store:
    """按配置打开存储（daemon 与 CLI 共用入口）。"""
    return Store(path=config.db_path(), max_items=config.get("history.max_items"))
