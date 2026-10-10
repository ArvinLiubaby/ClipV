# ClipV 功能文档（设计文档）

> 本文档为设计与需求文档；面向用户的介绍、安装与使用说明见项目根目录 [README.md](../README.md)。

**版本**：2.1（GTK3 原生重构 + 图片剪贴板支持，见文末「修订记录」）
**定位**：Linux 下的剪贴板历史管理工具，类似 Windows `Win+V`。
**命令名**：`clipv`

---

## 1. 产品概述

ClipV 在后台运行，自动记录系统剪贴板内容。用户通过全局快捷键呼出历史面板，可搜索、选择、固定或删除历史条目，并将选中内容重新写回剪贴板，必要时自动粘贴到当前窗口。

**技术形态**：Python + GTK3（PyGObject）桌面应用 + 常驻守护进程；数据仅存本地 SQLite。

---

## 2. 功能需求

### 2.1 剪贴板监听

| 编号 | 功能 | 优先级 | 说明 |
| :--- | :--- | :--- | :--- |
| F-01 | 后台自动监听系统剪贴板变化 | 高 | X11 通过 Gtk.Clipboard owner-change 信号；Wayland 依赖合成器 data-control（见 §4.3） |
| F-02 | 记录文本类型剪贴板内容 | 高 | 含富文本时提取纯文本 |
| F-03 | 重复内容不累积：同一内容再次复制时**全局去重并置顶**（更新时间戳） | 中 | 修订：原"连续复制只留一条"升级为全局去重+置顶，与 Win+V 一致 |
| F-04 | 支持忽略指定应用的复制内容 | 低 | 仅 X11 可识别来源应用；Wayland 下该项失效（见 F-62 降级） |
| F-05 | 预留图片、文件路径等类型扩展 | 低→**已落地（图片）** | 数据结构含 `kind` 字段；2.1 已实现 `image` 类型（F-06） |
| F-06 | 记录图片类型剪贴板内容 **[2.1 新增]** | 高 | 截图工具/浏览器复制的图像数据，以及复制图片文件（`file://` 路径）的场景；图片以 PNG 存于 `~/.local/share/clipv/images`（内容哈希去重），选中后写回图片剪贴板 |

### 2.2 历史记录管理

| 编号 | 功能 | 优先级 | 说明 |
| :--- | :--- | :--- | :--- |
| F-10 | 默认保存最近 100 条**非固定**历史，可配置 | 高 | 修订：上限仅约束非固定条目；单条上限默认 512KB，超限截断 |
| F-11 | 历史记录本地持久化，重启后不丢失 | 高 | SQLite（WAL 模式） |
| F-12 | 按时间倒序展示 | 高 | 修订：固定条目置顶成组，组内及各组均按时间倒序 |
| F-13 | 支持固定/收藏条目 | 中 | |
| F-14 | 固定条目不被自动清理 | 中 | 条数上限、清空操作均不涉及固定条目（`clear --all` 除外） |
| F-15 | 支持删除单条历史 | 中 | |
| F-16 | 支持一键清空非固定历史 | 中 | 修订：改为"同键二次确认"（连按 2 次，2.5s 内），防误删 |
| F-17 | 每条记录显示内容预览、时间、来源应用 | 中 | 修订：Wayland 下来源应用显示"未知"（安全模型限制）；图片条目显示行内缩略图 + "[图片] 宽×高" |

### 2.3 历史面板

| 编号 | 功能 | 优先级 | 说明 |
| :--- | :--- | :--- | :--- |
| F-20 | 通过全局快捷键呼出/隐藏面板 | 高 | 修订：X11 由守护进程自主抢占注册（`XGrabKey`）；Wayland 下普通应用无法抢占全局键，**必须**通过桌面环境快捷键绑定 `clipv toggle`（F-63 升为必选路径） |
| F-21 | 面板显示历史条目列表 | 高 | 固定组与普通组分区展示；面板默认 690×780（约 v2.0 可视面积的 1.5 倍），小屏幕自动夹取位置 |
| F-22 | 支持键盘上下键选择 | 高 | 焦点常驻搜索框，↑/↓ 直接移动列表选择；面板显示后校验输入焦点，被 WM 焦点窃取防护拒绝时由 X11 层强制聚焦 |
| F-23 | 按 Enter 确认选择 | 高 | 确认=写回剪贴板（+可选粘贴）并关闭面板 |
| F-24 | 按 Esc 关闭面板 | 高 | |
| F-25 | 支持鼠标点击选择 | 高 | 单击条目即确认；右键菜单：固定/仅复制/删除 |
| F-26 | 选择后内容写回系统剪贴板 | 高 | |
| F-27 | 可选自动模拟粘贴到当前焦点窗口 | 中 | 时序见 §3；X11 用 XTest（无需外部工具），Wayland 用 ydotool/wtype |
| F-28 | 面板默认出现在鼠标所在显示器 | 中 | 修订：X11 精确居中于鼠标所在显示器；Wayland 位置由合成器决定，应用无法自定位 |
| F-29 | 面板失去焦点后自动隐藏 | 中 | 修订：为避免与自动粘贴冲突，粘贴时序为"先隐藏面板→激活原窗口→延迟注入按键"；面板内不使用独立弹窗（确认交互改为面板内状态提示） |
| F-30 | 支持深色/浅色主题 | 低 | 跟随系统（GTK prefer-dark / 主题名）或手动指定 |

### 2.4 搜索与筛选

| 编号 | 功能 | 优先级 | 说明 |
| :--- | :--- | :--- | :--- |
| F-40 | 面板内提供搜索框 | 高 | 呼出后自动聚焦 |
| F-41 | 输入关键词实时过滤历史 | 高 | |
| F-42 | 支持匹配文本内容 | 高 | 大小写不敏感子串匹配；图片条目可用"图片/image"关键词命中 |
| F-43 | 搜索结果高亮关键词 | 低 | 预览行内高亮命中片段 |

### 2.5 快捷键

| 操作 | 默认快捷键 | 可配置 | 说明 |
| :--- | :--- | :--- | :--- |
| 呼出/隐藏面板 | `Super+V` | 是 | 托盘右键「设置快捷键…」或 `clipv shortcut edit` 图形化修改（按键录制）；也可改配置后用 `clipv shortcut install` 重绑 |
| 上一条 / 下一条 | `↑` / `↓` | 否 | |
| 确认选择 | `Enter` | 否 | |
| 关闭面板 | `Esc` | 否 | |
| 固定/取消固定 | `Ctrl+P` | 是 | |
| 删除当前条目 | `Delete` | 是 | |
| 清空非固定历史 | `Ctrl+Shift+Delete` | 是 | 二次确认（F-16） |

### 2.6 配置功能

| 编号 | 功能 | 优先级 | 说明 |
| :--- | :--- | :--- | :--- |
| F-50 | 提供本地配置文件 | 高 | `~/.config/clipv/config.json` |
| F-51 | 可配置全局快捷键 | 高 | 语法：修饰键（Ctrl/Alt/Shift/Super）+ 单键 |
| F-52 | 可配置历史保存条数 | 高 | `history.max_items` |
| F-53 | 可配置是否自动粘贴 | 中 | `paste.auto_paste`，默认 `false`（仅写回剪贴板） |
| F-54 | 可配置忽略应用列表 | 低 | `ignore.apps`（WM_CLASS，仅 X11 生效） |
| F-55 | 可配置存储路径 | 低 | `storage.path`，空=默认 |
| F-56 | 可配置主题跟随系统或手动指定 | 低 | `ui.theme`: `auto`/`dark`/`light` |

### 2.7 命令行功能

| 命令 | 功能 |
| :--- | :--- |
| `clipv` | 等价于 `clipv toggle`（修订：明确二者同义） |
| `clipv toggle` | 呼出/隐藏面板；守护进程未运行时自动拉起 |
| `clipv list [-n N] [--query Q] [--json]` | 列出历史记录（不依赖守护进程） |
| `clipv clear [--all]` | 清空非固定历史；`--all` 含固定条目（二次确认） |
| `clipv add [文本]` | 新增一条历史（无参数读 stdin）；供脚本/外部工具集成 **[新增]** |
| `clipv daemon [--detached]` | 启动守护进程（前台/后台） |
| `clipv stop` | 停止守护进程 **[新增]** |
| `clipv config show/path/edit/get/set/reset` | 查看或修改配置 |
| `clipv autostart enable/disable` | 登录自启动（XDG autostart）**[新增，落地 F-60]** |
| `clipv shortcut install/remove` | 将全局快捷键绑定到 `clipv toggle`（自动处理 Cinnamon/GNOME，其余打印指引；Cinnamon 下自动触发绑定重载，无需注销）**[新增，落地 F-63]** |
| `clipv shortcut edit` | 打开图形化快捷键设置对话框（托盘菜单同款，按键录制后立即生效） |
| `clipv doctor` | 环境与能力诊断（会话类型、依赖工具、降级提示）**[新增]** |
| `clipv version` | 版本信息 |

### 2.8 系统集成功能

| 编号 | 功能 | 优先级 | 说明 |
| :--- | :--- | :--- | :--- |
| F-60 | 支持用户登录后自动启动 | 高 | `clipv autostart enable` |
| F-61 | 支持 X11 环境 | 高 | 全功能：监听/热键/来源应用/自动粘贴（XTest） |
| F-62 | 支持 Wayland 环境，受协议限制时降级 | 中 | 降级矩阵见 §4.3；`clipv doctor` 明确告知当前能力 |
| F-63 | 支持通过桌面环境快捷键绑定 `clipv toggle` | 中→**高** | Wayland 下这是唯一可靠的热键路径；Cinnamon/GNOME 可自动安装 |
| F-64 | 自动粘贴不可用时，仅写回剪贴板并提示手动粘贴 | 中 | 修订：仅当**开启了**自动粘贴但不可用时才发系统通知提醒；纯剪贴板模式不打扰 |
| F-65 | 系统托盘常驻 **[2.1 新增]** | 中 | AppIndicator 图标：左键呼出面板；右键菜单「显示面板 / 设置快捷键… / 退出 ClipV」 |

### 2.9 安全与隐私功能

| 编号 | 功能 | 优先级 | 说明 |
| :--- | :--- | :--- | :--- |
| F-70 | 所有历史仅保存在本地，不联网 | 高 | 无任何网络代码 |
| F-71 | 支持忽略密码管理器等敏感应用 | 中 | 默认忽略列表含常见密码管理器；仅 X11 生效 |
| F-72 | 支持手动清空全部历史 | 高 | `clipv clear --all` 或面板内操作 |
| F-73 | 历史数据文件仅当前用户可读写 | 中 | 目录 0700、数据文件 0600 |
| F-74 | 日志不记录剪贴板内容 | 中 | 仅记录哈希前缀/长度 **[新增]** |

---

## 3. 核心交互流程

1. 用户正常复制文本。
2. ClipV 后台自动记录到历史（去重+置顶，F-03）。
3. 用户按下 `Super+V`（X11 自主注册；Wayland 走桌面环境绑定）。
4. 历史面板弹出并聚焦搜索框（显示前先记录当前焦点窗口，供粘贴用）。
5. 用户输入关键词过滤，或直接 ↑/↓ 选择。
6. 按 `Enter` 确认（或鼠标单击条目）。
7. 选中内容写回系统剪贴板。
8. 面板**先隐藏**；若开启自动粘贴：
   - X11：重新激活第 4 步记录的原窗口 → 延迟（默认 120ms）注入 `Ctrl+V`；
   - Wayland：延迟注入 `Ctrl+V`（ydotool/wtype）；
   - 若工具不可用：不注入，发系统通知"已复制，请手动粘贴"（F-64）。
9. 流程结束，原窗口保持焦点。

---

## 4. 技术方案

### 4.1 组件

| 组件 | 选型 |
| :--- | :--- |
| GUI | GTK3 / PyGObject（系统自带），无边框置顶面板 + Cairo 自绘阴影与 CSS 主题 |
| 常驻进程 | `clipv daemon`：GLib 主循环承载面板、监听、热键与 IPC |
| 进程通信 | Unix Domain Socket（`$XDG_RUNTIME_DIR/clipv.sock`）+ 行式 JSON |
| 存储 | SQLite（WAL），默认 `~/.local/share/clipv/history.db` |
| X11 能力 | python-xlib：`XGrabKey` 全局热键、`_NET_ACTIVE_WINDOW` 来源应用与焦点校验/强制聚焦、XTest 模拟粘贴 |
| 剪贴板 | GTK `Gtk.Clipboard` owner-change 信号（X11，文本与图片）；Wayland 用 `wl-paste --watch`（文本），不可用则降级 |
| 通知 | `notify-send`（可选） |
| 打包 | deb 纯 Python 源码包（Architecture: all），GUI 依赖走 apt 系统包 |

### 4.2 运行环境（本机实测）

- X11 + Cinnamon（muffin WM），Python 3.12（GTK3/PyGObject 为系统包，Cinnamon 环境自带）
- 已知环境事实：
  - `Super+V` 已被 Cinnamon 自定义快捷键占用（原指向 `copyq-toggle`）→ 用 `clipv shortcut install` 重绑（自动备份、可 `remove` 还原）；
  - Cinnamon 菜单 applet（Cinnamenu 等）的“Super 单键开菜单”（overlay-key）与 `Super+V` 可正常共存：muffin 6.x 对纯修饰键绑定有避让处理（实机验证；曾误诊为 active grab 吞键，真因是自定义快捷键命令失效+绑定缓存未重载）；
  - muffin 存在"窗口每次重映射上移约 36px"缺陷 → 面板每次显示前显式设置几何位置来规避；
  - 面板显示后由守护进程校验 `_NET_ACTIVE_WINDOW`，若 WM（焦点窃取防护）未把焦点转给面板，用 `XSetInputFocus` 强制切换输入焦点（实机验证：即使 present() 被拒，键盘导航也可用）；
  - `xdotool/wl-paste/ydotool` 均未安装 → X11 下用内置 XTest，无需外部工具。

### 4.3 平台能力矩阵

| 能力 | X11 | Wayland（KDE/wlroots） | Wayland（GNOME 旧版） |
| :--- | :--- | :--- | :--- |
| 剪贴板监听 | ✅ Gtk.Clipboard owner-change | ✅ wl-paste --watch | ❌ 降级：建议 X11 会话或桌面扩展；`doctor` 提示 |
| 全局热键 | ✅ 自主注册（冲突时提示） | ⚠️ 需 DE 绑定 `clipv toggle` | ⚠️ 同左 |
| 来源应用 | ✅ | ❌ 显示"未知" | ❌ |
| 自动粘贴 | ✅ XTest | ⚠️ 需 ydotool/wtype | ⚠️ 同左 |
| 面板定位 | ✅ 精确居中所属显示器 | ⚠️ 合成器决定 | ⚠️ 同左 |

### 4.4 目录结构

```
ClipV/
├── README.md               # 面向用户的说明（安装/使用）
├── LICENSE                 # MIT License
├── pyproject.toml
├── install.sh              # 源码开发安装（检查系统依赖 + 可执行入口，无 venv）
├── docs/
│   └── design.md           # 本文档（功能需求与设计）
├── packaging/
│   ├── build_deb.sh        # 一键构建 deb（纯 Python 源码包，约 48 KB）
│   └── icons/clipv.svg     # 品牌图标（安装到 hicolor 图标主题）
├── dist/                   # 构建产物（clipv_<版本>_all.deb）
├── clipv/
│   ├── __init__.py  __main__.py
│   ├── cli.py              # 命令行入口（不依赖 GUI 库，轻量）
│   ├── config.py           # 配置加载/校验/原子写入
│   ├── store.py            # SQLite 存储（纯 stdlib，可独立测试）
│   ├── ipc.py              # Unix socket 服务端/客户端
│   ├── session.py          # 会话环境探测与工具可用性
│   ├── util.py             # 通用工具（预览行/相对时间等）
│   ├── x11util.py          # 热键注册/来源窗口/XTest 粘贴（python-xlib）
│   ├── paste.py            # 粘贴策略与降级、系统通知
│   ├── images.py           # 图片仓库（PNG 落盘/去重/清理；顶层无 GUI 依赖）
│   ├── clipboard.py        # 剪贴板监听（文本+图片；Gtk.Clipboard owner-change / Wayland 双策略）
│   ├── panel.py            # 历史面板 UI（GTK3 + Cairo + CSS）
│   ├── hotkey_dialog.py    # 快捷键设置对话框（按键录制）
│   ├── tray.py             # 系统托盘图标（AppIndicator/StatusIcon）
│   └── daemon.py           # 守护进程主逻辑（GLib 主循环）
└── tests/                  # unittest（核心用纯 stdlib；GTK 冒烟用例需图形会话，否则跳过）
```

### 4.5 安装与运行

**方式一：deb 安装包（推荐，普通用户）**

适用于 Debian/Ubuntu 系（Linux Mint 22 / Ubuntu 24.04 等）。deb 为纯 Python 源码包（**约 48 KB**，`Architecture: all`）：GUI 依赖直接使用发行版系统包（GTK3/PyGObject，Cinnamon/Mint/Ubuntu 默认预装），由 apt 自动解析，无需 venv/pip。将 `dist/clipv_<版本>_all.deb` 分发（GitHub Releases / 网盘等）后，用户下载并执行：

```bash
sudo apt install ./clipv_2.0.0_all.deb     # 在 deb 所在目录执行；文件名随版本变化
```

- 安装位置 `/usr/lib/python3/dist-packages/clipv`，命令入口 `/usr/bin/clipv`；图形界面下也可双击 deb 安装；
- 系统依赖 `python3, python3-gi, gir1.2-gtk-3.0, python3-gi-cairo, python3-xlib` 由 apt 自动安装；
- 卸载：`sudo apt remove clipv`（用户配置与历史数据保留）。

**方式二：源码安装（开发 / 自行打包）**

```bash
./install.sh                # 检查系统 GTK 依赖，生成 ~/.local/bin/clipv 入口
```

**安装后配置（两种方式通用）**

```bash
clipv doctor                # 环境诊断
clipv shortcut install      # 绑定 Super+V（含冲突处理）
clipv autostart enable      # 登录自启动
clipv                       # 手动呼出
```

**维护者：重新构建 deb**

```bash
packaging/build_deb.sh      # 产物：dist/clipv_<版本>_all.deb（含解包冒烟验证）
```

构建流程：拷贝 `clipv/` 源码到 dist-packages 布局 → 生成 `/usr/bin/clipv` 启动器与桌面文件 → 固定声明 5 项系统依赖 → `dpkg-deb --root-owner-group -Zzstd` 打包并对解包产物做运行冒烟。

---

## 5. 优先级说明

- **高**：v1.0 必须实现。
- **中**：v1.0 尽量实现，或 v1.1 补充。
- **低**：后续版本扩展。

---

## 6. 修订记录（v1.0 → v2.0）

| # | 原文档问题 | 修订 |
| :--- | :--- | :--- |
| 1 | `clipv` 与 `clipv toggle` 定义重复 | 明确二者同义 |
| 2 | F-03 仅"连续"去重 | 升级为全局去重+置顶 |
| 3 | F-12 时间倒序 与 固定条目展示方式不明确 | 固定组置顶、组内时间倒序 |
| 4 | F-10/F-14 条数上限是否含固定条目不明确 | 明确仅约束非固定条目 |
| 5 | F-17/F-04/F-71 假设来源应用可得 | 标注 Wayland 降级（不可得） |
| 6 | F-20 假设应用可自主抢占全局热键 | X11 自主注册；Wayland 必须以 F-63（DE 绑定）为路径，F-63 优先级升为"高" |
| 7 | F-27 与 F-29（失焦隐藏）时序冲突 | 明确"先隐藏→激活原窗口→延迟注入"时序；面板内禁用独立弹窗 |
| 8 | F-16 一键清空误删风险 | 同键二次确认 |
| 9 | F-60/F-63 无落地命令 | 新增 `autostart`、`shortcut` 子命令 |
| 10 | 降级状态用户不可见 | 新增 `doctor` 命令；F-64 通知策略明确 |
| 11 | F-28 面板定位未限定平台差异 | X11 精确；Wayland 标注合成器决定 |
| 12 | 缺少外部集成与运维命令 | 新增 `add`、`stop`；补充 F-74 日志隐私约束 |
| 13 | 缺少安装与分发方式说明 | §4.5 改为 deb 安装优先；新增 `packaging/build_deb.sh`（自包含 venv、自动系统依赖解析、构建内含解包冒烟验证） |

### v2.0：GTK3 原生重构（体积优化）

| # | 变更 | 说明 |
| :--- | :--- | :--- |
| 1 | GUI 技术栈：PySide6（Qt6）→ GTK3 / PyGObject | 程序只面向 Linux 桌面，GUI 栈改为发行版系统包；外观与交互保持不变 |
| 2 | deb 体积：56 MB → 约 40 KB | 不再打包自包含 venv（Qt 运行时含 ICU 等解压后约 600 MB），仅分发纯 Python 源码（Architecture: all） |
| 3 | 守护进程主循环：Qt 事件循环 → GLib 主循环 | `GLib.io_add_watch` 监听 X11 fd、`unix_signal_add` 处理信号、`idle_add` 桥接 IPC 线程 |
| 4 | 剪贴板监听：`QClipboard.dataChanged` → `Gtk.Clipboard owner-change` | 防抖、自我写回抑制、来源应用识别逻辑均保留 |
| 5 | 面板：QWidget 自绘 → Gtk.Window + Cairo 阴影 + CSS 主题 | 功能全集保留（搜索高亮/固定/删除/二次确认/右键菜单/toast/定位） |
| 6 | 启动速度 | GTK 冷启动显著快于 Qt：完整测试套件耗时 21s → 2.8s |
| 7 | 依赖与安装 | pip/uv 不再必需；deb 声明 `python3, python3-gi, gir1.2-gtk-3.0, python3-gi-cairo, python3-xlib`，由 apt 解析 |

### v2.1：图片剪贴板支持与交互修复

| # | 变更 | 说明 |
| :--- | :--- | :--- |
| 1 | 新增图片剪贴板支持（F-06） | 截图/浏览器图像数据与"复制图片文件（file:// 路径）"均捕获为图片条目；PNG 存于 `~/.local/share/clipv/images`（内容哈希去重，孤儿文件定期清理）；面板行内渲染缩略图，选中后写回图片剪贴板 |
| 2 | 修复呼出后键盘导航可能失灵 | 面板可见但焦点未转移（WM 焦点窃取防护拒绝 present）时，守护进程在显示后 120ms 校验 `_NET_ACTIVE_WINDOW`，必要时用 `XSetInputFocus` 强制聚焦 |
| 3 | 面板可视面积放大至约 1.5 倍 | 690×780（原 460×520）；字号不变，一屏可见条目更多 |
| 4 | 搜索与 CLI 适配图片 | 图片条目可用"图片/image"命中（F-42）；`clipv list` 显示"[图片] 宽×高"，JSON 输出含 `kind` |
| 5 | 系统托盘集成（F-65） | AppIndicator 常驻图标：左键呼出面板，右键菜单「显示面板 / 设置快捷键… / 退出 ClipV」；打包引入品牌图标 `packaging/icons/clipv.svg` |
| 6 | 快捷键图形化配置 | 托盘右键按键录制对话框（`hotkey_dialog.py`）；`clipv shortcut edit` 同入口；保存后自动更新 Cinnamon 绑定并立即重新注册，无需注销 |
| 7 | 以 MIT 许可证开源 | 新增 README.md、LICENSE 与 docs/ 设计文档；仓库发布于 GitHub |
