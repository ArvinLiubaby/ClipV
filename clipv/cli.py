"""命令行入口。

设计约束：本模块与子命令实现（list/clear/config/doctor 等）**不导入 GUI 库**，
保证 `clipv toggle` 在快捷键路径上的启动速度；需要 GUI 的命令在函数内
延迟导入 daemon 模块。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import __app_name__, __version__, config as cfg_mod, ipc
from .config import Config, ConfigError, default_db_path
from .images import gc_orphan_images, png_size
from .store import Store, open_store
from .util import relative_time, setup_logging

# ---------------------------------------------------------------- 工具

_STATE_KEYS_BACKUP = os.path.join(cfg_mod.state_dir(), "shortcut-backup.json")


def _pkg_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clipv_command() -> str:
    """生成稳定的 clipv 启动命令（供 autostart / 快捷键绑定使用）。"""
    venv_script = os.path.join(os.path.dirname(sys.executable), "clipv")
    if os.path.isfile(venv_script):
        return venv_script
    found = shutil.which("clipv")
    if found:
        return found
    return f"env PYTHONPATH={_pkg_root()} {sys.executable} -m clipv"


_DAEMON_BG_ENV = "CLIPV_DAEMON_BG"  # 标记“后台重启”子进程，防止自我重启循环


def _daemon_env() -> Dict[str, str]:
    env = os.environ.copy()
    root = _pkg_root()
    if root not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn_daemon_process() -> None:
    """以全新解释器进程在后台启动守护进程。

    fork 之后继续初始化 GTK 在部分环境会死锁（fork 前已加载的 GLib
    状态被继承），因此后台化统一采用“重新 exec 一个进程”的方式。
    """
    env = _daemon_env()
    env[_DAEMON_BG_ENV] = "1"
    subprocess.Popen(
        [sys.executable, "-m", "clipv", "daemon"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, env=env)


def _daemon_running() -> bool:
    return ipc.is_running()


def _start_daemon_background() -> bool:
    """拉起后台守护进程并等待 socket 就绪。"""
    _spawn_daemon_process()
    for _ in range(50):  # 最多等待 ~7.5 秒（GTK 冷启动通常 <1 秒，保守预留）
        time.sleep(0.15)
        if _daemon_running():
            return True
    return False


def _notify_daemon_reload() -> None:
    try:
        ipc.call({"cmd": "reload"}, timeout=2)
    except ipc.IpcError:
        pass


# ---------------------------------------------------------------- 子命令

def cmd_toggle(_args) -> int:
    try:
        resp = ipc.call({"cmd": "toggle"}, timeout=3)
        return 0 if resp.get("ok") else _fail(resp)
    except ipc.IpcNotRunning:
        pass
    if not _start_daemon_background():
        _err("守护进程启动失败，请运行 `clipv daemon` 查看错误")
        return 1
    resp = ipc.call({"cmd": "toggle"}, timeout=5)
    return 0 if resp.get("ok") else _fail(resp)


def cmd_list(args) -> int:
    config = Config.load()
    store = open_store(config)
    try:
        entries = store.list(query=args.query)
        if args.limit:
            entries = entries[: args.limit]
        if args.json:
            print(json.dumps([
                {"id": e.id, "kind": e.kind, "content": e.content,
                 "pinned": e.pinned, "captured_at": e.captured_at,
                 "source_app": e.source_app}
                for e in entries], ensure_ascii=False, indent=2))
            return 0
        if not entries:
            print("（无历史记录）")
            return 0
        for i, e in enumerate(entries, 1):
            pin = "★" if e.pinned else " "
            when = relative_time(e.captured_at)
            source = e.source_app or "未知"
            if e.kind == "image":
                size = png_size(e.content)
                preview = (f"[图片] {size[0]}×{size[1]}" if size else "[图片]")
            else:
                preview = e.content.replace("\n", " ").strip()
                if len(preview) > 60:
                    preview = preview[:60] + "…"
            print(f"{i:>4}  {pin}  {when:<10}  {source:<12}  {preview}")
    finally:
        store.close()
    return 0


def cmd_clear(args) -> int:
    if args.all and not args.yes:
        if sys.stdin.isatty():
            answer = input("将清空全部历史（含固定条目），确认？[y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("已取消")
                return 1
        else:
            _err("清空全部历史需要 --yes 确认（非交互环境）")
            return 1
    request = {"cmd": "clear", "all": bool(args.all)}
    try:
        resp = ipc.call(request, timeout=3)
        if resp.get("ok"):
            print(f"已清空 {resp.get('data', {}).get('removed', 0)} 条历史")
            return 0
        return _fail(resp)
    except ipc.IpcNotRunning:
        pass
    config = Config.load()
    store = open_store(config)
    try:
        removed = store.clear_all() if args.all else store.clear_unpinned()
        gc_orphan_images(store)  # 清理不再被引用的图片文件
        print(f"已清空 {removed} 条历史")
    finally:
        store.close()
    return 0


def cmd_add(args) -> int:
    if args.text is not None:
        content = args.text
    else:
        raw = sys.stdin.buffer.read()
        content = raw.decode("utf-8", errors="replace")
    content = content.rstrip("\n")
    if not content.strip():
        _err("内容为空，未添加")
        return 1
    try:
        resp = ipc.call({"cmd": "add", "content": content,
                         "source_app": "cli"}, timeout=3)
        return 0 if resp.get("ok") else _fail(resp)
    except ipc.IpcNotRunning:
        pass
    config = Config.load()
    store = open_store(config)
    try:
        max_bytes = int(config.get("history.max_item_kb")) * 1024
        raw = content.encode("utf-8", errors="replace")
        if len(raw) > max_bytes:
            content = raw[:max_bytes].decode("utf-8", errors="ignore")
        store.add(content, source_app="cli")
    finally:
        store.close()
    return 0


def cmd_ingest(_args) -> int:
    """隐藏命令：Wayland 下由 `wl-paste --watch` 逐次调用，stdin 为剪贴板内容。"""
    raw = sys.stdin.buffer.read()
    content = raw.decode("utf-8", errors="replace")
    if not content.strip():
        return 0
    try:
        ipc.call({"cmd": "add", "content": content}, timeout=5)
        return 0
    except ipc.IpcError:
        pass
    config = Config.load()
    store = open_store(config)
    try:
        store.add(content)
    finally:
        store.close()
    return 0


def cmd_daemon(args) -> int:
    if args.detached and not os.environ.get(_DAEMON_BG_ENV):
        _spawn_daemon_process()   # 重新 exec 一个全新进程提供后台化
        return 0
    setup_logging(log_file=cfg_mod.log_path(), verbose=args.verbose,
                  to_stderr=not args.detached
                  and not os.environ.get(_DAEMON_BG_ENV))
    from .daemon import run_daemon   # 延迟导入：需要 GTK
    return run_daemon(detached=args.detached, verbose=args.verbose)


def cmd_stop(_args) -> int:
    try:
        resp = ipc.call({"cmd": "quit"}, timeout=3)
        if resp.get("ok"):
            print("守护进程已停止")
            return 0
        return _fail(resp)
    except ipc.IpcNotRunning:
        print("守护进程未运行")
        return 0


def cmd_config(args) -> int:
    sub = args.config_command or "show"
    path = cfg_mod.config_path()
    if sub == "path":
        print(path)
        return 0
    if sub == "show":
        config = Config.load()
        print(json.dumps(config.data, ensure_ascii=False, indent=2))
        print(f"# 配置文件：{path}", file=sys.stderr)
        if config.load_error:
            print(f"# 警告：{config.load_error}", file=sys.stderr)
        return 0
    if sub == "edit":
        editor = os.environ.get("EDITOR") or shutil.which("nano") or "vi"
        Config.load()  # 确保文件存在
        rc = subprocess.call([editor, path])
        if rc == 0:
            _notify_daemon_reload()
        return rc
    if sub == "get":
        config = Config.load()
        try:
            value = config.get(args.key)
        except ConfigError as exc:
            _err(str(exc))
            return 1
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return 0
    if sub == "set":
        config = Config.load()
        value: Any = args.value
        try:
            value = json.loads(args.value)
        except (json.JSONDecodeError, ValueError):
            pass  # 按字符串处理
        try:
            normalized = config.set(args.key, value)
        except (ConfigError, ValueError) as exc:
            _err(str(exc))
            return 1
        print(f"{args.key} = "
              f"{normalized if isinstance(normalized, str) else json.dumps(normalized, ensure_ascii=False)}")
        _notify_daemon_reload()
        return 0
    if sub == "reset":
        config = Config.load()
        config.data = json.loads(json.dumps(cfg_mod.DEFAULTS))
        config.save()
        print("配置已重置为默认值")
        _notify_daemon_reload()
        return 0
    _err(f"未知配置子命令：{sub}")
    return 1


# ---------------------------------------------------------------- autostart

def _autostart_path() -> str:
    return os.path.join(cfg_mod._xdg_dir("XDG_CONFIG_HOME", "~/.config"),
                        "autostart", "clipv.desktop")


def cmd_autostart(args) -> int:
    path = _autostart_path()
    if args.action == "enable":
        cfg_mod.ensure_dir(os.path.dirname(path))
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=ClipV 剪贴板历史\n"
            "Comment=ClipV 剪贴板历史守护进程（类似 Win+V）\n"
            f"Exec={_clipv_command()} daemon\n"
            "Icon=clipv\n"
            "Terminal=false\n"
            "StartupNotify=false\n"
            "X-GNOME-Autostart-enabled=true\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"已启用登录自启动：{path}")
        return 0
    if args.action == "disable":
        if os.path.exists(path):
            os.unlink(path)
            print("已禁用登录自启动")
        else:
            print("登录自启动未启用")
        return 0
    _err(f"未知操作：{args.action}")
    return 1


# ---------------------------------------------------------------- shortcut

def _gsettings_get(schema: str, key: str) -> Any:
    try:
        out = subprocess.run(["gsettings", "get", schema, key],
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        text = out.stdout.strip()
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text.strip("'")
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gsettings_set(schema: str, key: str, value: str) -> bool:
    try:
        out = subprocess.run(["gsettings", "set", schema, key, value],
                             capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _hotkey_to_gsettings(hotkey: str) -> str:
    mods, key = cfg_mod.parse_hotkey(hotkey)
    order = [("<Super>", "super"), ("<Ctrl>", "ctrl"), ("<Alt>", "alt"),
             ("<Shift>", "shift")]
    return "".join(token for token, mod in order if mod in mods) + key


def _reload_cinnamon_customs() -> bool:
    """触发 Cinnamon 重新加载自定义快捷键。

    Cinnamon 的 keybindingManager 只在 custom-list 变化时重建各条目的
    注册；直接修改已有条目的 binding/command（custom-list 未变）不会
    生效，表现为快捷键仍执行旧命令（内存缓存）。这里把 custom-list
    临时置空再放回以触发重载，使磁盘上的最新配置立即生效。
    """
    schema = "org.cinnamon.desktop.keybindings"
    custom_list = _gsettings_get(schema, "custom-list") or []
    if isinstance(custom_list, str):
        custom_list = [custom_list]
    if not custom_list:
        return True
    ok = _gsettings_set(schema, "custom-list", repr([]))
    ok = _gsettings_set(schema, "custom-list", repr(custom_list)) and ok
    return ok


def de_binds_toggle(hotkey: str) -> bool:
    """检测桌面环境是否已把 hotkey 绑定到本工具的 toggle 命令。

    守护进程用它避免与桌面环境快捷键"双路径"重复触发：
    如 Cinnamon 的 `clipv shortcut install` 已写入绑定，则不再自主抢占注册。
    目前仅实现 Cinnamon；其他桌面返回 False（由守护进程自主注册）。
    """
    if not shutil.which("gsettings"):
        return False
    if "cinnamon" not in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return False
    try:
        binding_text = _hotkey_to_gsettings(hotkey)
    except (ValueError, ConfigError):
        return False
    schema = "org.cinnamon.desktop.keybindings"
    custom_list = _gsettings_get(schema, "custom-list") or []
    if isinstance(custom_list, str):
        custom_list = [custom_list]
    for idx in custom_list:
        schema_path = (f"org.cinnamon.desktop.keybindings.custom-keybinding:"
                       f"/org/cinnamon/desktop/keybindings/custom-keybindings/{idx}/")
        binding = _gsettings_get(schema_path, "binding")
        if not isinstance(binding, list) or binding_text not in binding:
            continue
        command = _gsettings_get(schema_path, "command")
        if isinstance(command, str) and "clipv" in command and "toggle" in command:
            return True
    return False


def _shortcut_install_cinnamon(hotkey: str, command: str) -> int:
    schema = "org.cinnamon.desktop.keybindings"
    binding_text = _hotkey_to_gsettings(hotkey)
    custom_list = _gsettings_get(schema, "custom-list") or []
    if isinstance(custom_list, str):
        custom_list = [custom_list]

    def entry_path(idx: str) -> str:
        return (f"org.cinnamon.desktop.keybindings.custom-keybinding:"
                f"/org/cinnamon/desktop/keybindings/custom-keybindings/{idx}/")

    existing_same_command: Optional[str] = None
    existing_same_binding: Optional[str] = None
    for idx in custom_list:
        schema_path = entry_path(idx)
        cmd = _gsettings_get(schema_path, "command")
        binding = _gsettings_get(schema_path, "binding")
        if isinstance(cmd, str) and "clipv" in cmd and "toggle" in cmd:
            existing_same_command = idx
        if isinstance(binding, list) and binding_text in binding:
            existing_same_binding = idx

    target_idx = existing_same_command or existing_same_binding
    if target_idx is None:
        used = {int(i.replace("custom", "")) for i in custom_list
                if i.startswith("custom") and i[6:].isdigit()}
        n = 0
        while n in used:
            n += 1
        target_idx = f"custom{n}"
        new_list = list(custom_list) + [target_idx]
        if not _gsettings_set(schema, "custom-list", repr(new_list)):
            return 1
        backup = {"desktop": "cinnamon", "action": "created", "idx": target_idx}
    else:
        backup = {
            "desktop": "cinnamon", "action": "modified", "idx": target_idx,
            "name": _gsettings_get(entry_path(target_idx), "name"),
            "binding": _gsettings_get(entry_path(target_idx), "binding"),
            "command": _gsettings_get(entry_path(target_idx), "command"),
        }
    cfg_mod.ensure_dir(cfg_mod.state_dir())
    with open(_STATE_KEYS_BACKUP, "w", encoding="utf-8") as fh:
        json.dump(backup, fh, ensure_ascii=False, indent=2)

    ok = (_gsettings_set(entry_path(target_idx), "name", "'ClipV 剪贴板历史'")
          and _gsettings_set(entry_path(target_idx), "binding", repr([binding_text]))
          and _gsettings_set(entry_path(target_idx), "command", f"'{command} toggle'"))
    if not ok:
        _err("写入 Cinnamon 快捷键失败")
        return 1
    if not _reload_cinnamon_customs():
        print("提示：未能触发桌面环境重载快捷键，如未生效请注销后重新登录")
    print(f"已将 {hotkey} 绑定到 `clipv toggle`（原绑定已备份，可用 "
          f"`clipv shortcut remove` 还原）")
    return 0


def _shortcut_remove_cinnamon() -> int:
    try:
        with open(_STATE_KEYS_BACKUP, "r", encoding="utf-8") as fh:
            backup = json.load(fh)
    except (OSError, json.JSONDecodeError):
        _err("没有可还原的快捷键备份（仅支持还原本工具修改的绑定）")
        return 1
    if backup.get("desktop") != "cinnamon":
        _err("备份不属于当前桌面环境")
        return 1
    schema = "org.cinnamon.desktop.keybindings"
    entry = (f"org.cinnamon.desktop.keybindings.custom-keybinding:"
             f"/org/cinnamon/desktop/keybindings/custom-keybindings/"
             f"{backup['idx']}/")
    if backup.get("action") == "created":
        custom_list = _gsettings_get(schema, "custom-list") or []
        if isinstance(custom_list, str):
            custom_list = [custom_list]
        new_list = [i for i in custom_list if i != backup["idx"]]
        _gsettings_set(schema, "custom-list", repr(new_list))
    else:
        _gsettings_set(entry, "name", repr(backup.get("name") or ""))
        _gsettings_set(entry, "binding", repr(backup.get("binding") or []))
        _gsettings_set(entry, "command", repr(backup.get("command") or ""))
    _reload_cinnamon_customs()
    os.unlink(_STATE_KEYS_BACKUP)
    print("快捷键绑定已还原")
    return 0


def cmd_shortcut(args) -> int:
    if args.action == "edit":
        # 打开图形化快捷键设置对话框（由守护进程弹出，与托盘菜单同一入口）
        try:
            resp = ipc.call({"cmd": "hotkey-dialog"}, timeout=5)
        except ipc.IpcNotRunning:
            if not _start_daemon_background():
                _err("守护进程启动失败，请运行 `clipv daemon` 查看错误")
                return 1
            try:
                resp = ipc.call({"cmd": "hotkey-dialog"}, timeout=5)
            except ipc.IpcNotRunning:
                _err("守护进程未运行")
                return 1
        if resp.get("ok"):
            print("已打开快捷键设置对话框，按下新的组合键后点「保存」即可")
            return 0
        return _fail(resp)
    hotkey = Config.load().get("hotkey.toggle")
    command = _clipv_command()
    if args.action == "install":
        gsettings = shutil.which("gsettings")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if gsettings and "cinnamon" in desktop:
            return _shortcut_install_cinnamon(hotkey, command)
        print("未检测到可自动配置的桌面环境，请手动绑定：")
        print(f"  快捷键 {hotkey} → 命令 `{command} toggle`")
        print("  GNOME：设置 → 键盘 → 自定义快捷键")
        print("  KDE：系统设置 → 快捷键 → 自定义快捷键（命令处填上述命令）")
        print("  Sway/Hyprland：在配置文件里绑定 exec " + f"'{command} toggle'")
        return 1
    if args.action == "remove":
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "cinnamon" in desktop:
            return _shortcut_remove_cinnamon()
        _err("当前桌面环境不支持自动还原，请手动删除自定义快捷键")
        return 1
    _err(f"未知操作：{args.action}")
    return 1


# ---------------------------------------------------------------- about

def cmd_about(_args) -> int:
    """打开关于对话框（与托盘菜单「关于 ClipV」同一入口）。"""
    try:
        resp = ipc.call({"cmd": "about-dialog"}, timeout=5)
    except ipc.IpcNotRunning:
        if not _start_daemon_background():
            _err("守护进程启动失败，请运行 `clipv daemon` 查看错误")
            return 1
        try:
            resp = ipc.call({"cmd": "about-dialog"}, timeout=5)
        except ipc.IpcNotRunning:
            _err("守护进程未运行")
            return 1
    if resp.get("ok"):
        print("已打开「关于」对话框")
        return 0
    return _fail(resp)


# ---------------------------------------------------------------- doctor

def _doctor_hotkey(daemon_ok: bool) -> None:
    config = Config.load()
    hotkey = config.get("hotkey.toggle")
    if daemon_ok:
        try:
            data = ipc.call({"cmd": "status"}, timeout=3).get("data", {})
            owner = data.get("hotkey_owner")
            if owner == "de":
                print(f"  ✓ 全局热键 {hotkey}：由桌面环境接管（`clipv shortcut install`）")
            elif owner == "self":
                print(f"  ✓ 全局热键 {hotkey}：守护进程已注册")
            elif owner == "conflict" or data.get("hotkey_conflict"):
                print(f"  ⚠ 全局热键 {hotkey}：注册失败（被其他程序占用）")
                print("    建议：clipv shortcut install（自动重绑）")
            else:
                print(f"  – 全局热键 {hotkey}：未注册")
            return
        except ipc.IpcError:
            pass
    # 守护进程未运行时做无副作用探测
    if not os.environ.get("DISPLAY"):
        print(f"  – 全局热键 {hotkey}：非 X11 会话，需桌面环境绑定 `clipv toggle`")
        return
    try:
        from .x11util import HotkeyConflict, X11, X11Unavailable
        x = X11()
        try:
            x.grab_hotkey(hotkey)
            print(f"  ✓ 全局热键 {hotkey}：当前可自主注册")
        except HotkeyConflict:
            print(f"  ⚠ 全局热键 {hotkey}：已被占用（可能已被桌面环境绑定到本工具或其他程序）")
            print("    可用 `clipv shortcut install` 检查/重绑")
        finally:
            x.close()
    except X11Unavailable:
        print(f"  – 全局热键 {hotkey}：无法连接 X 服务器")
    except Exception as exc:  # Xlib 依赖缺失等
        print(f"  – 全局热键 {hotkey}：检查失败（{exc}）")


def cmd_doctor(_args) -> int:
    from .session import detect
    from .paste import describe_paste_methods

    config = Config.load()
    session = detect()
    print(f"{__app_name__} {__version__} 环境诊断")
    print("─" * 46)

    print(f"会话：{session.session_type}", end="")
    if session.desktop:
        print(f"（{session.desktop}）", end="")
    print()
    if session.display:
        print(f"  DISPLAY={session.display}", end="")
    if session.wayland_display:
        print(f"  WAYLAND_DISPLAY={session.wayland_display}", end="")
    print()

    running = _daemon_running()
    if running:
        try:
            data = ipc.call({"cmd": "status"}, timeout=3).get("data", {})
            print(f"守护进程：✓ 运行中（pid={data.get('pid', '?')}，"
                  f"面板{'显示' if data.get('panel_visible') else '隐藏'}）")
        except ipc.IpcError:
            print("守护进程：✓ 运行中")
    else:
        print("守护进程：– 未运行")

    print(f"配置：{cfg_mod.config_path()}"
          + (f"（⚠ {config.load_error}）" if config.load_error else "（✓）"))

    db_path = config.db_path()
    if os.path.exists(db_path):
        mode = oct(os.stat(db_path).st_mode & 0o777)
        try:
            store = open_store(config)
            total, pinned = store.stats()
            store.close()
            print(f"存储：{db_path}（权限 {mode}，{total} 条 / {pinned} 固定）")
        except Exception as exc:
            print(f"存储：{db_path}（⚠ 读取失败：{exc}）")
    else:
        print(f"存储：{db_path}（尚未创建）")

    _doctor_hotkey(running)

    if session.is_x11:
        print("剪贴板监听：✓ X11 GTK owner-change 信号（含来源应用/忽略列表）")
    elif session.is_wayland:
        if session.wl_data_control:
            print("剪贴板监听：✓ wl-paste --watch（合成器支持 data-control）")
        else:
            print("剪贴板监听：⚠ 降级——合成器不支持 data-control，后台无法监听")
            print("  说明：旧版 GNOME 等环境需登录 X11 会话，或等待合成器支持")
            if not session.has_tool("wl-paste"):
                print("  另外未安装 wl-clipboard（sudo apt install wl-clipboard）")
    else:
        print("剪贴板监听：✗ 无图形会话")

    print("自动粘贴：")
    for name, ok, note in describe_paste_methods(session):
        mark = "✓" if ok else "–"
        print(f"  {mark} {name}：{note}")
    if not config.get("paste.auto_paste"):
        print("  （配置中 auto_paste=false，当前仅写回剪贴板）")

    print("运行环境：")
    print(f"  Python {sys.version.split()[0]}（{sys.executable}）")
    print(f"  clipv 命令：{_clipv_command()}")
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        print("  GTK3（PyGObject）：✓")
    except (ImportError, ValueError):
        print("  GTK3（PyGObject）：✗ 未安装（sudo apt install python3-gi gir1.2-gtk-3.0）")
    try:
        import Xlib  # noqa: F401
        print("  python-xlib：✓")
    except ImportError:
        print("  python-xlib：✗ 未安装（X11 热键/粘贴需要）")
    return 0


def cmd_version(_args) -> int:
    print(f"{__app_name__} {__version__}")
    return 0


# ---------------------------------------------------------------- 入口

def _err(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)


def _fail(resp: Dict[str, Any]) -> int:
    _err(str(resp.get("error", "未知错误")))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipv", description=f"{__app_name__} —— Linux 剪贴板历史管理工具")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("toggle", help="呼出/隐藏历史面板（默认命令）")

    p_list = sub.add_parser("list", help="列出历史记录")
    p_list.add_argument("-n", "--limit", type=int, default=0, help="最多显示条数")
    p_list.add_argument("-q", "--query", default=None, help="关键词过滤")
    p_list.add_argument("--json", action="store_true", help="以 JSON 输出")

    p_clear = sub.add_parser("clear", help="清空非固定历史")
    p_clear.add_argument("--all", action="store_true", help="同时清空固定条目")
    p_clear.add_argument("-y", "--yes", action="store_true", help="跳过确认")

    p_add = sub.add_parser("add", help="新增一条历史（无参数时读取 stdin）")
    p_add.add_argument("text", nargs="?", default=None, help="文本内容")

    p_daemon = sub.add_parser("daemon", help="启动守护进程")
    p_daemon.add_argument("--detached", action="store_true", help="后台运行")
    p_daemon.add_argument("-v", "--verbose", action="store_true", help="调试日志")

    sub.add_parser("stop", help="停止守护进程")

    p_config = sub.add_parser("config", help="查看或修改配置")
    config_sub = p_config.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="显示当前配置")
    config_sub.add_parser("path", help="显示配置文件路径")
    config_sub.add_parser("edit", help="用编辑器打开配置文件")
    config_sub.add_parser("reset", help="重置为默认配置")
    p_cfg_get = config_sub.add_parser("get", help="读取配置项")
    p_cfg_get.add_argument("key")
    p_cfg_set = config_sub.add_parser("set", help="修改配置项")
    p_cfg_set.add_argument("key")
    p_cfg_set.add_argument("value")

    p_auto = sub.add_parser("autostart", help="登录自启动管理")
    p_auto.add_argument("action", choices=["enable", "disable"])

    p_short = sub.add_parser("shortcut", help="全局快捷键绑定管理")
    p_short.add_argument("action", choices=["install", "remove", "edit"])

    sub.add_parser("about", help="打开关于对话框")

    sub.add_parser("doctor", help="环境与能力诊断")

    sub.add_parser("version", help="显示版本")

    sub.add_parser("_ingest", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "toggle"
    handlers = {
        "toggle": cmd_toggle,
        "list": cmd_list,
        "clear": cmd_clear,
        "add": cmd_add,
        "daemon": cmd_daemon,
        "stop": cmd_stop,
        "config": cmd_config,
        "autostart": cmd_autostart,
        "shortcut": cmd_shortcut,
        "about": cmd_about,
        "doctor": cmd_doctor,
        "version": cmd_version,
        "_ingest": cmd_ingest,
    }
    return handlers[command](args)


if __name__ == "__main__":
    raise SystemExit(main())
