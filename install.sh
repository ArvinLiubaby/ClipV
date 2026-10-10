#!/usr/bin/env bash
# ClipV 开发模式安装：检查系统 GTK 依赖，源码直接运行，生成 ~/.local/bin/clipv 入口。
#
# ClipV 现在是"系统 Python 包"架构：GUI 栈（GTK3/PyGObject）使用发行版系统包，
# 无 venv、无 pip 安装、无 PySide6/Qt 依赖。生产分发请用 packaging/build_deb.sh。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"

echo "==> ClipV 安装脚本（开发模式）"
echo "    项目目录：$PROJECT_DIR"

# 1) 检查系统依赖（Debian/Ubuntu 包名）
MISSING=()
check_pkg() {
    local mod="$1" pkg="$2"
    if ! python3 -c "import $mod" >/dev/null 2>&1; then
        MISSING+=("$pkg")
    fi
}
python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk" \
    >/dev/null 2>&1 || MISSING+=("python3-gi gir1.2-gtk-3.0")
check_pkg "cairo" "python3-gi-cairo"
check_pkg "Xlib" "python3-xlib"

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "错误：缺少系统依赖，请先安装：" >&2
    echo "    sudo apt install ${MISSING[*]}" >&2
    exit 1
fi
echo "==> 系统依赖检查通过（GTK3 / PyGObject / python-xlib）"

# 2) 生成可执行入口：包装脚本直接调系统 python3（开发模式，源码即运行）
mkdir -p "$BIN_DIR"
if [ -e "$BIN_DIR/clipv" ] && [ ! -L "$BIN_DIR/clipv" ] \
   && ! grep -q "ClipV" "$BIN_DIR/clipv" 2>/dev/null; then
    echo "警告：$BIN_DIR/clipv 已存在且不是本项目的入口，跳过创建" >&2
else
    cat > "$BIN_DIR/clipv" <<EOF
#!/usr/bin/env bash
# ClipV 开发模式启动器（源码目录：$PROJECT_DIR）
exec env PYTHONPATH="$PROJECT_DIR\${PYTHONPATH:+:\$PYTHONPATH}" python3 -m clipv "\$@"
EOF
    chmod +x "$BIN_DIR/clipv"
    echo "==> 已创建入口：$BIN_DIR/clipv"
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "提示：请把 $BIN_DIR 加入 PATH：export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo
echo "==> 安装完成，后续步骤："
echo "    clipv doctor              # 环境诊断"
echo "    clipv shortcut install    # 绑定全局快捷键（如已被占用会提示）"
echo "    clipv autostart enable    # 登录自启动"
echo "    clipv                     # 呼出面板"
echo
echo "生产安装（deb 包）：bash packaging/build_deb.sh && sudo dpkg -i dist/*.deb"
