"""配置管理：默认值、加载/校验/原子保存、点号键访问、快捷键语法解析。"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

VALID_THEMES = ("auto", "dark", "light")

#: 默认配置。storage.path 为空表示使用 `default_db_path()`。
DEFAULTS: Dict[str, Any] = {
    "hotkey": {
        "toggle": "Super+V",          # 修饰键( Ctrl/Alt/Shift/Super ) + 单键
    },
    "history": {
        "max_items": 100,             # 非固定条目保留上限（F-10/F-14）
        "max_item_kb": 512,           # 单条内容上限，超限截断
    },
    "paste": {
        "auto_paste": True,           # 确认后自动模拟粘贴（类似 Win+V）
        "delay_ms": 120,              # 隐藏面板后注入按键的延迟
    },
    "ignore": {
        "apps": ["keepassxc", "keepass", "bitwarden", "1password", "enpass"],
    },
    "storage": {
        "path": "",                   # 空 = 默认 ~/.local/share/clipv/history.db
    },
    "ui": {
        "theme": "auto",              # auto | dark | light
    },
    "daemon": {
        "poll_interval_ms": 350,      # 剪贴板轮询防抖
    },
}

_KNOWN_MODS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt",
               "shift": "shift", "super": "super", "meta": "super",
               "win": "super", "cmd": "super"}
_KEY_RE = re.compile(r"^(?:[a-z0-9]|f(?:[1-9]|1[0-9]|2[0-4]))$")


# ---------------------------------------------------------------- 路径

def _xdg_dir(env: str, default: str) -> str:
    value = os.environ.get(env, "")
    if not value or not value.startswith("/"):
        value = os.path.expanduser(default)
    return value


def config_dir() -> str:
    return os.path.join(_xdg_dir("XDG_CONFIG_HOME", "~/.config"), "clipv")


def data_dir() -> str:
    return os.path.join(_xdg_dir("XDG_DATA_HOME", "~/.local/share"), "clipv")


def state_dir() -> str:
    return os.path.join(_xdg_dir("XDG_STATE_HOME", "~/.local/state"), "clipv")


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def default_db_path() -> str:
    return os.path.join(data_dir(), "history.db")


def log_path() -> str:
    return os.path.join(state_dir(), "clipv.log")


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "clipv.sock")
    return f"/tmp/clipv-{os.getuid()}.sock"


def ensure_dir(path: str, mode: int = 0o700) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)  # F-73：目录仅当前用户可访问
    except OSError:
        pass


# ---------------------------------------------------------------- 快捷键

def parse_hotkey(hotkey: str) -> Tuple[List[str], str]:
    """解析快捷键字符串为 (修饰键列表, 主键)，如 'Super+Shift+V' → (['super','shift'],'v')。

    修饰键支持 Ctrl/Alt/Shift/Super（含别名 Control/Meta/Win/Cmd）；
    主键支持字母、数字、F1-F24。非法输入抛 ValueError。
    """
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    if len(parts) < 1:
        raise ValueError("快捷键不能为空")
    mods: List[str] = []
    key: Optional[str] = None
    for part in parts:
        if part in _KNOWN_MODS:
            mod = _KNOWN_MODS[part]
            if mod in mods:
                raise ValueError(f"修饰键重复：{part}")
            mods.append(mod)
        elif key is None and _KEY_RE.match(part):
            key = part
        else:
            raise ValueError(f"无法识别的按键：{part}（主键仅支持字母/数字/F1-F24）")
    if key is None:
        raise ValueError("缺少主键（如 Super+V 中的 V）")
    if not mods:
        raise ValueError("至少需要一个修饰键（Ctrl/Alt/Shift/Super）")
    return mods, key


def format_hotkey(mods: List[str], key: str) -> str:
    """规范显示形式，如 ['super'] + 'v' → 'Super+V'。"""
    order = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super"}
    return "+".join([order[m] for m in mods] + [key.upper()])


# ---------------------------------------------------------------- 加载/保存

class ConfigError(Exception):
    pass


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_value(key: str, value: Any) -> Any:
    """按键校验并归一化取值，失败抛 ConfigError。"""
    if key == "hotkey.toggle":
        if not isinstance(value, str):
            raise ConfigError("hotkey.toggle 必须是字符串")
        try:
            mods, main = parse_hotkey(value)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        return format_hotkey(mods, main)
    if key == "history.max_items":
        value = int(value)
        if not 1 <= value <= 10000:
            raise ConfigError("history.max_items 应在 1-10000 之间")
        return value
    if key == "history.max_item_kb":
        value = int(value)
        if not 1 <= value <= 10240:
            raise ConfigError("history.max_item_kb 应在 1-10240 之间")
        return value
    if key == "paste.auto_paste":
        return bool(value)
    if key == "paste.delay_ms":
        value = int(value)
        if not 0 <= value <= 3000:
            raise ConfigError("paste.delay_ms 应在 0-3000 之间")
        return value
    if key == "ignore.apps":
        if isinstance(value, str):
            value = [s for s in re.split(r"[,\s]+", value) if s]
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ConfigError("ignore.apps 应为字符串列表")
        return [s.strip().lower() for s in value if s.strip()]
    if key == "storage.path":
        if not isinstance(value, str):
            raise ConfigError("storage.path 必须是字符串")
        return os.path.expanduser(value.strip())
    if key == "ui.theme":
        if value not in VALID_THEMES:
            raise ConfigError(f"ui.theme 应为 {'/'.join(VALID_THEMES)} 之一")
        return value
    if key == "daemon.poll_interval_ms":
        value = int(value)
        if not 100 <= value <= 5000:
            raise ConfigError("daemon.poll_interval_ms 应在 100-5000 之间")
        return value
    raise ConfigError(f"未知配置项：{key}")


class Config:
    """运行期配置对象；changed 记录最近一次加载的文件 mtime，用于热重载。"""

    def __init__(self, path: Optional[str] = None, data: Optional[Dict[str, Any]] = None):
        self.path = path or config_path()
        self.data = data if data is not None else copy.deepcopy(DEFAULTS)
        self.loaded_mtime: float = 0.0
        self.load_error: str = ""

    # ------------------------------------------------------ 取值
    def get(self, key: str) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ConfigError(f"未知配置项：{key}")
            node = node[part]
        return node

    def db_path(self) -> str:
        custom = self.get("storage.path")
        return custom or default_db_path()

    # ------------------------------------------------------ 文件
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """读取配置文件；文件不存在时用默认值并自动落盘一份。"""
        cfg = cls(path=path)
        file_path = cfg.path
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("配置根节点必须是对象")
            merged = _deep_merge(DEFAULTS, raw)
            for key in _all_keys(DEFAULTS):
                try:
                    value = _get_node(merged, key)
                    _set_node(merged, key, _validate_value(key, value))
                except (ConfigError, ValueError) as exc:
                    cfg.load_error = f"{key}: {exc}"
            cfg.data = merged
        except FileNotFoundError:
            cfg.save()
        except (OSError, ValueError) as exc:
            cfg.load_error = f"配置读取失败，已回退默认值：{exc}"
        try:
            cfg.loaded_mtime = os.path.getmtime(file_path)
        except OSError:
            cfg.loaded_mtime = 0.0
        return cfg

    def reload_if_changed(self) -> bool:
        """文件被外部修改时重新加载。返回是否发生重载。"""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime <= self.loaded_mtime:
            return False
        fresh = Config.load(self.path)
        self.data = fresh.data
        self.loaded_mtime = fresh.loaded_mtime
        self.load_error = fresh.load_error
        return True

    def save(self) -> None:
        ensure_dir(os.path.dirname(self.path))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def set(self, key: str, value: Any) -> Any:
        """校验并写入配置项（点号键），随后原子保存。返回归一化后的值。"""
        normalized = _validate_value(key, value)
        _set_node(self.data, key, normalized)
        self.save()
        self.loaded_mtime = time.time()
        return normalized


def _all_keys(tree: Dict[str, Any], prefix: str = "") -> List[str]:
    keys: List[str] = []
    for key, value in tree.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys.extend(_all_keys(value, full))
        else:
            keys.append(full)
    return keys


def _get_node(tree: Dict[str, Any], key: str) -> Any:
    node: Any = tree
    for part in key.split("."):
        node = node[part]
    return node


def _set_node(tree: Dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
