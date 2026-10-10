#!/usr/bin/env bash
# =============================================================================
# ClipV deb 打包脚本（v2：GTK3 系统 Python 包模式）
# -----------------------------------------------------------------------------
# 用法：  packaging/build_deb.sh
# 产物：  dist/clipv_<版本>_all.deb（预期 <100KB）
#
# 设计说明：
#   * ClipV 只面向 Linux 桌面，GUI 栈直接使用发行版系统包
#     （GTK3 / PyGObject，Cinnamon/Mint/Ubuntu 默认预装），
#     deb 内只有纯 Python 源码（Architecture: all），无 venv、无 Qt。
#   * 对比：Qt 系（PySide6/PyQt6）自包含方案体积硬下限 20~30MB
#     （libQt6Core/Gui/Widgets + ICU 数据），GTK3 方案小两个数量级。
#   * 依赖声明固定五项：python3, python3-gi, gir1.2-gtk-3.0,
#     python3-gi-cairo, python3-xlib（X11 热键/粘贴）。
#   * 无需 root 构建：dpkg-deb --root-owner-group 将文件属主置为 root:root。
#   * 构建目录位于原生文件系统（/tmp）：若项目盘为 NTFS/exFAT（fuseblk），
#     chmod 会失效导致 deb 内文件权限异常；产物最后拷贝回 dist/。
# =============================================================================
set -euo pipefail
umask 022   # 统一文件权限为 644/755（避免构建环境 umask 002 带入 664/775）

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PKG_NAME="clipv"
VERSION="$(sed -n 's/^version = *"\(.*\)"/\1/p' pyproject.toml | head -1)"
if [ -z "$VERSION" ]; then
    echo "错误：无法从 pyproject.toml 解析版本号" >&2
    exit 1
fi
ARCH="all"   # 纯 Python 源码包，与 CPU 架构无关
BUILD_DIR="${TMPDIR:-/tmp}/clipv-deb-build"   # 必须在原生文件系统（见头部说明）
ROOT="$BUILD_DIR/root"
DIST_DIR="$PROJECT_DIR/dist"
DEB_FILE="$DIST_DIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"

# ---- [1/5] 目录准备 ----------------------------------------------------------
echo "==> [1/5] 准备构建目录"
rm -rf "$BUILD_DIR"
mkdir -p "$ROOT/usr/lib/python3/dist-packages" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/scalable/apps" \
         "$ROOT/usr/share/doc/$PKG_NAME" "$ROOT/DEBIAN"
mkdir -p "$DIST_DIR"

# ---- [2/5] 安装 Python 包源码 -------------------------------------------------
echo "==> [2/5] 拷贝 Python 源码到 dist-packages"
cp -r "$PROJECT_DIR/clipv" "$ROOT/usr/lib/python3/dist-packages/clipv"
find "$ROOT/usr/lib/python3/dist-packages/clipv" \
    -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$ROOT/usr/lib/python3/dist-packages/clipv" -name "*.pyc" -delete 2>/dev/null || true

# ---- [3/5] 入口、图标与桌面集成 ----------------------------------------------
echo "==> [3/5] 生成启动器、安装图标与桌面文件"
cat > "$ROOT/usr/bin/clipv" <<'WRAP'
#!/bin/sh
# ClipV 启动器：系统 Python 包模式（GTK3/PyGObject 由 apt 依赖提供）
exec /usr/bin/python3 -m clipv "$@"
WRAP
chmod 755 "$ROOT/usr/bin/clipv"

cat > "$ROOT/usr/share/applications/clipv.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=ClipV 剪贴板历史
Comment=类似 Win+V 的剪贴板历史面板；点击或按 Super+V 呼出
Exec=/usr/bin/clipv
Icon=clipv
Terminal=false
StartupNotify=false
Categories=Utility;
Keywords=clipboard;history;paste;
DESK
chmod 644 "$ROOT/usr/share/applications/clipv.desktop"

# 品牌图标：安装到 hicolor 主题（托盘与应用菜单统一使用）
cp "$PROJECT_DIR/packaging/icons/clipv.svg" \
   "$ROOT/usr/share/icons/hicolor/scalable/apps/clipv.svg"
chmod 644 "$ROOT/usr/share/icons/hicolor/scalable/apps/clipv.svg"

# ---- [4/5] 文档 --------------------------------------------------------------
echo "==> [4/5] 生成文档（版权/许可/变更日志/README）"
cat > "$ROOT/usr/share/doc/$PKG_NAME/copyright" <<'COPY'
ClipV — Linux 剪贴板历史管理工具（类似 Windows Win+V）
Copyright (C) 2026 arvin

License: MIT

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
COPY
chmod 644 "$ROOT/usr/share/doc/$PKG_NAME/copyright"

cp "$PROJECT_DIR/README.md" "$ROOT/usr/share/doc/$PKG_NAME/README.md"
chmod 644 "$ROOT/usr/share/doc/$PKG_NAME/README.md"
cp "$PROJECT_DIR/LICENSE" "$ROOT/usr/share/doc/$PKG_NAME/LICENSE"
chmod 644 "$ROOT/usr/share/doc/$PKG_NAME/LICENSE"

{
    echo "$PKG_NAME ($VERSION) unstable; urgency=low"
    echo
    echo "  * 2.1.1：托盘菜单新增「关于 ClipV」（版本 / GitHub 仓库地址 /"
    echo "    MIT 许可，链接可在浏览器打开）；clipv about 同入口。"
    echo "  * 2.1.0：新增图片剪贴板支持（大缩略图预览）；系统托盘图标"
    echo "    （右键可设置快捷键、退出）；Enter 确认后自动粘贴（类似 Win+V）；"
    echo "    面板可视面积放大 1.5 倍、底部提示字号加大；修复呼出后键盘"
    echo "    导航可能失灵（WM 拒绝聚焦时由 X11 层强制切换输入焦点）。"
    echo "  * 2.0.0 GTK3 原生重构：以发行版 GTK3/PyGObject 替代自包含"
    echo "    PySide6 运行时，deb 体积由 56MB 降至 100KB 以内。"
    echo
    echo " -- arvin <arvin@localhost>  $(date -R)"
} | gzip -9 -n > "$ROOT/usr/share/doc/$PKG_NAME/changelog.Debian.gz"
chmod 644 "$ROOT/usr/share/doc/$PKG_NAME/changelog.Debian.gz"

# ---- [5/5] 元数据与打包 ------------------------------------------------------
echo "==> [5/5] 生成 DEBIAN 元数据并打包"
chmod -R go-w "$ROOT"
INSTALLED_KB="$(du -sk "$ROOT" | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<CTRL
Package: $PKG_NAME
Version: $VERSION
Architecture: $ARCH
Maintainer: arvin <arvin@localhost>
Installed-Size: $INSTALLED_KB
Depends: python3, python3-gi, gir1.2-gtk-3.0, python3-gi-cairo, python3-xlib
Recommends: libnotify-bin
Suggests: wl-clipboard
Section: utils
Priority: optional
Description: 类似 Windows Win+V 的 Linux 剪贴板历史工具（GTK3 原生）
 常驻后台记录剪贴板历史，Super+V 呼出搜索面板；
 X11 全功能（全局热键/来源应用/自动粘贴），Wayland 降级可用；
 GTK3 原生实现，直接使用系统 GUI 库，包体积极小。
CTRL
chmod 644 "$ROOT/DEBIAN/control"

cat > "$ROOT/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    # 预编译字节码（可选，加速日常启动；失败不影响功能）
    /usr/bin/python3 -m compileall -q /usr/lib/python3/dist-packages/clipv/ \
        >/dev/null 2>&1 || true
    # 刷新图标缓存（让品牌图标立即在托盘/菜单可见；失败不影响功能）
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor \
            >/dev/null 2>&1 || true
    fi
    echo "ClipV 安装完成。建议依次执行："
    echo "  clipv doctor            # 环境与能力诊断"
    echo "  clipv autostart enable  # 登录自启动"
    echo "  clipv shortcut install  # 绑定 Super+V（Cinnamon 下自动生效）"
    echo "之后按 Super+V 即可呼出剪贴板历史面板。"
fi
exit 0
POST
chmod 755 "$ROOT/DEBIAN/postinst"

cat > "$ROOT/DEBIAN/postrm" <<'POST'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    echo "ClipV 已卸载。若守护进程仍在运行：注销并重新登录即可结束。"
    echo "用户数据（历史与配置）保留在 ~/.local/share/clipv 与 ~/.config/clipv。"
fi
exit 0
POST
chmod 755 "$ROOT/DEBIAN/postrm"

dpkg-deb --build --root-owner-group -Zzstd "$ROOT" "$DEB_FILE" >/dev/null

# ---- 验证 --------------------------------------------------------------------
echo "==> 验证打包产物"
CHECK_DIR="$BUILD_DIR/check"
rm -rf "$CHECK_DIR" && mkdir -p "$CHECK_DIR"
dpkg-deb -x "$DEB_FILE" "$CHECK_DIR"
C_SITE="$CHECK_DIR/usr/lib/python3/dist-packages"
echo "     入口 shebang: $(head -1 "$CHECK_DIR/usr/bin/clipv")"
PYTHONPATH="$C_SITE" /usr/bin/python3 -m clipv version \
    || { echo "错误：解包运行失败" >&2; exit 1; }
PYTHONPATH="$C_SITE" /usr/bin/python3 -c "
import clipv.panel, clipv.clipboard, clipv.daemon
print('     模块导入冒烟 OK（panel/clipboard/daemon）')" \
    || { echo "错误：模块导入失败" >&2; exit 1; }
/usr/bin/python3 -c "
import xml.etree.ElementTree as ET
ET.parse('$CHECK_DIR/usr/share/icons/hicolor/scalable/apps/clipv.svg')
print('     品牌图标 SVG 解析 OK（hicolor/scalable/apps/clipv.svg）')" \
    || { echo "错误：图标文件缺失或不是有效 SVG" >&2; exit 1; }
rm -rf "$CHECK_DIR"

echo
echo "==> 完成：$DEB_FILE"
echo "    大小：$(du -h "$DEB_FILE" | cut -f1)（安装大小 $(du -sh "$ROOT" | cut -f1)）"
echo "    安装：sudo apt install ./$(basename "$DEB_FILE")"
