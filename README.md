# 阴阳师多开同步器

给《阴阳师》桌面版（MuMu 模拟器专版客户端）做的 **多开 + 后台同步** 工具。
在一个窗口里正常操作，其余窗口在后台实时执行相同的点击/拖拽/滚轮/键盘——
不切窗口、不抢鼠标。

超鬼王期间创了不少小号，干脆建了一个僵尸寮，但一个个保日活太麻烦，模拟器又太占空间。其他的同步器又达不到我想要的效果，干脆AI大法搞一个。

## 功能

- **一键多开**：自动驱动 V5 沙盘多开器（每 3 秒一个、模拟手动节奏），
  数量 1~99，启动完成自动平铺
- **毕方式平铺**：单元格保持游戏自然宽高比（游戏不会自行改窗口尺寸），
  支持自动选行列或手动指定（1行2列 / 2行2列 / …）
- **后台同步**：低级钩子观察主控窗口的真实操作，以窗口消息（PostMessage）
  转发到其余窗口；主控/受控动态切换，也可手动固定主控
- **拟人偏移**：受控窗口的点击叠加以真实点击点为中心的二维正态分布随机偏移，各窗口落点互不相同

## 环境要求

- Windows 10/11 x64
- Python 3.11+（源码运行）或直接使用 PyInstaller 打包的绿色版
- [V5 多开器](https://www.onlinedown.net/soft/2257.htm)（沙盘隔离多开的载体，
  **本仓库不包含**，需自备；在 `yys_sync_config.json` 里配置 `v5_src_dir` 指向其所在目录）
- 《阴阳师》桌面版客户端（`Launch.exe`）

## 使用

```bat
:: 源码运行（GUI 版 / 控制台版）
python -I gui_sync.py
python -I sync_full.py
```

或用 PyInstaller 打包绿色版：

```bat
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --uac-admin --name YYSSync gui_sync.py
:: 产物在 dist\YYSSync\，将 V5 的 4 个文件（V5.exe + 3 个 v5_*.dll）放入其 v5\ 子目录即可
```

启动后：确认游戏路径 → 一键多开并平铺 → 登录各号 → **F8** 开启同步 →
在主控窗口正常操作。热键：F8 开关同步 / Ctrl+Shift+2 平铺 / F9 退出。

## 目录结构

```
gui_sync.py      GUI（tkinter）
sync_full.py     同步引擎（钩子观察 + PostMessage 转发 + 多开 + 平铺）
start_sync.bat / start_sync_console.bat   开发用启动器（自动 UAC 提权）
YYSSync.spec     PyInstaller 打包配置
```

## 免责声明

- 本工具仅转发使用者本人的实时操作，不含任何自动打怪/脚本逻辑
- 游戏官方对按键类功能存在检测机制，使用本工具产生的账号风险由使用者自担
- 请遵守游戏用户协议，合理使用，勿用于牟利或破坏游戏环境
