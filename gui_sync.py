"""阴阳师多开同步器 — GUI 版
复用 sync_full.py 的同步引擎 (钩子观察 + PostMessage 后台转发)。

启动: pythonw -I gui_sync.py (非管理员时自动弹 UAC 重启自身, 无控制台窗口)
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # -I 隔离模式不会自动加脚本目录

# 绿色版 (PyInstaller 打包) 下, 配置/日志/V5 资源一律放在 exe 旁边
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = HERE

CFG_PATH = os.path.join(BASE_DIR, "yys_sync_config.json")
DEFAULT_LAUNCH_PATH = r"D:\others\yys\Launch.exe"
DEFAULT_V5_SRC_DIR = r"D:\others\60848_20170622011404"  # 现有 V5 所在目录 (集成来源)
V5_LOCAL_DIR = os.path.join(BASE_DIR, "v5")            # 集成后的本地 V5 目录
V5_FILES = ["V5.exe", "v5_hook.dll", "v5_Process_Manager.dll", "v5_Log.dll"]


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(c):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


CFG = load_cfg()


def ensure_v5_local():
    """把 V5 及其 DLL 集成到本地 v5/ 目录 (绿色版自带); 返回可用的 V5 exe 路径或 None。"""
    local_exe = os.path.join(V5_LOCAL_DIR, "V5.exe")
    if os.path.exists(local_exe):
        return local_exe
    src = CFG.get("v5_src_dir", DEFAULT_V5_SRC_DIR)
    src_exe = os.path.join(src, "V5.exe")
    if not os.path.exists(src_exe):
        return None
    try:
        os.makedirs(V5_LOCAL_DIR, exist_ok=True)
        copied = []
        for f in V5_FILES:
            s = os.path.join(src, f)
            if os.path.exists(s):
                shutil.copy2(s, os.path.join(V5_LOCAL_DIR, f))
                copied.append(f)
        # 让集成版 V5 记住游戏路径 (原版配置是 GBK 编码)
        launch = CFG.get("launch_path", "")
        with open(os.path.join(V5_LOCAL_DIR, "config.ini"), "w", encoding="gbk",
                  errors="ignore") as f:
            f.write("[V5]\nTitle=V5 程序多开器 0.1 Beta\n"
                    f"LastPath={launch}\nurl=www.prayaya.com\nQuery=0\n")
        log(f"V5 已集成到本地 v5/: {copied}")
    except Exception as ex:
        log(f"V5 集成失败: {ex!r}")
    return local_exe if os.path.exists(local_exe) else None


def detect_launch_path():
    """启动路径: 已配置 > 老版 V5 配置里的 LastPath > 已知默认位置。"""
    if CFG.get("launch_path") and os.path.exists(CFG["launch_path"]):
        return CFG["launch_path"]
    try:
        import configparser
        src_ini = os.path.join(CFG.get("v5_src_dir", DEFAULT_V5_SRC_DIR), "config.ini")
        cp = configparser.ConfigParser()
        cp.read(src_ini, encoding="gbk")
        p = cp.get("V5", "LastPath", fallback="")
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    if os.path.exists(DEFAULT_LAUNCH_PATH):
        return DEFAULT_LAUNCH_PATH
    return ""

import tkinter as tk
from tkinter import ttk

import sync_full as _core  # noqa: F401  (触发其 DPI 感知初始化)
from sync_full import (Engine, find_game_windows, get_rect, log,
                       user32, kernel32, WM_QUIT)


def ensure_admin_gui():
    """非管理员时经 UAC 重启自身; 返回 True = 当前已提权可继续。"""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return True
    if getattr(sys, "frozen", False):  # 打包版: 直接以管理员重启 exe
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, None, None, 1)
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pythonw if os.path.exists(pythonw) else sys.executable
        script = os.path.abspath(__file__)
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, f'-I "{script}"', None, 1)
    if ret > 32:
        return False  # 已拉起提权实例, 当前进程静默退出
    ctypes.windll.user32.MessageBoxW(
        0, "未获得管理员权限。\n游戏以管理员运行, 同步器必须同级提权才能转发消息。",
        "阴阳师多开同步器", 0x40 | 0x1000)  # MB_ICONINFO | MB_TOPMOST
    return False


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.engine = Engine()
        if not self.engine.acquire_single_instance():
            ctypes.windll.user32.MessageBoxW(0, "同步器已在运行 (单实例保护)",
                                             "阴阳师多开同步器", 0x40 | 0x1000)
            raise SystemExit(1)
        self.engine.notify = lambda msg: self.q.put(("log", msg))
        self._hotkey_thread = None
        self._hotkey_tid = 0

        root.title("阴阳师多开同步器")
        root.geometry("820x640")
        root.minsize(740, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._apply_dpi_scaling()

        # ── 状态栏 ──
        top = ttk.Frame(root, padding=(10, 8))
        top.pack(fill="x")
        self.var_status = tk.StringVar(value="○ 同步已停止")
        self.var_count = tk.StringVar(value="窗口: -")
        self._status_lbl = tk.Label(top, textvariable=self.var_status,
                                    font=("Microsoft YaHei UI", 11, "bold"), fg="#888")
        self._status_lbl.pack(side="left")
        tk.Label(top, textvariable=self.var_count).pack(side="right")

        # ── 按钮区 ──
        btns = ttk.Frame(root, padding=(10, 0))
        btns.pack(fill="x")
        self.btn_toggle = ttk.Button(btns, text="开启同步 (F8)", width=18, command=self.toggle)
        self.btn_toggle.pack(side="left")
        ttk.Label(btns, text="平铺排布:").pack(side="left", padx=(10, 2))
        self.var_layout = tk.StringVar(value="自动")
        ttk.Combobox(btns, textvariable=self.var_layout, width=7, state="readonly",
                     values=("自动", "1行2列", "2行1列", "1行3列", "3行1列",
                             "2行2列", "2行3列", "3行2列", "3行3列")
                     ).pack(side="left")
        ttk.Button(btns, text="平铺窗口", width=12,
                   command=self.tile).pack(side="left", padx=(4, 8))
        ttk.Button(btns, text="刷新列表", width=12,
                   command=lambda: self.refresh_list(True)).pack(side="left")
        ttk.Button(btns, text="退出", width=10, command=self.on_close).pack(side="right")

        # ── 一键多开 ──
        mk = ttk.Frame(root, padding=(10, 4))
        mk.pack(fill="x")
        ttk.Label(mk, text="一键多开:").pack(side="left")
        self.var_open_count = tk.StringVar(value="2")
        ttk.Spinbox(mk, from_=1, to=99, width=5, textvariable=self.var_open_count).pack(
            side="left", padx=(4, 0))
        ttk.Button(mk, text="多开并平铺", width=12,
                   command=self.multi_open).pack(side="left", padx=(6, 14))
        ttk.Label(mk, text="游戏路径:").pack(side="left")
        self.var_launch = tk.StringVar(value=detect_launch_path())
        ttk.Entry(mk, textvariable=self.var_launch).pack(
            side="left", padx=4, fill="x", expand=True)

        # ── 窗口列表 ──
        lst = ttk.LabelFrame(root, text=" 阴阳师窗口 (双击/右键可指定主控; 默认跟随前台) ",
                             padding=(6, 4))
        lst.pack(fill="both", expand=True, padx=10, pady=(8, 4))
        cols = ("role", "hwnd", "rect", "title")
        self.tree = ttk.Treeview(lst, columns=cols, show="headings", height=5)
        for cid, txt, w in (("role", "角色", 70), ("hwnd", "句柄", 100),
                            ("rect", "位置/尺寸", 190), ("title", "标题", 180)):
            self.tree.heading(cid, text=txt)
            self.tree.column(cid, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        # 双击 / 右键 指定主控
        self.tree.bind("<Double-1>", self._on_double_click)
        self.ctx_menu = tk.Menu(root, tearoff=0)
        self.ctx_menu.add_command(label="设为主控 (同步源)", command=self._ctx_set_master)
        self.ctx_menu.add_command(label="恢复自动主控 (跟随前台)",
                                  command=lambda: (self.engine.set_auto_master(),
                                                   self.refresh_list(False)))
        self.tree.bind("<Button-3>", self._show_menu)

        # 主控描边覆盖层 (点击穿透, 标记当前主控窗口)
        self.overlay = tk.Toplevel(root)
        self.overlay.overrideredirect(True)
        self._ov_key = '#010203'          # 透明键色 (中心挖空区域)
        self.overlay.configure(bg=self._ov_key)
        try:
            self.overlay.attributes('-transparentcolor', self._ov_key)
            self.overlay.attributes('-topmost', True)
        except Exception:
            pass
        self._ov_canvas = tk.Canvas(self.overlay, highlightthickness=0,
                                    bd=0, bg='#22c55e')
        self._ov_canvas.pack(fill='both', expand=True)
        self.overlay.bind('<Configure>', lambda e: self._ov_redraw())
        self.overlay.withdraw()
        self._ov_ct_done = False
        self.tree.tag_configure('master', background='#dcfce7')

        # ── 日志 ──
        lg = ttk.LabelFrame(root, text=" 运行日志 ", padding=(6, 4))
        lg.pack(fill="both", expand=True, padx=10, pady=(4, 8))
        self.txt = tk.Text(lg, height=9, state="disabled", font=("Consolas", 9),
                           background="#111", foreground="#ccc")
        self.txt.pack(fill="both", expand=True)

        tk.Label(root, text="F8 = 开/关同步    Ctrl+Shift+2 = 平铺    F9 = 退出    "
                            "双击/右键窗口行可指定主控 (指定后仅操作该窗口时同步)",
                 foreground="#666").pack(pady=(0, 6))

        # 引擎窗口快照初始化 + 后台扫描线程
        self.engine.refresh_windows()
        threading.Thread(target=self._rescan_loop, daemon=True).start()

        # 热键线程 (F8 / F9 / 平铺)
        self._hotkey_thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        self._hotkey_thread.start()

        # UI 轮询: 队列消息 + 窗口列表刷新
        self.root.after(100, self._poll_queue)
        self.refresh_list(True)
        self.root.after(1000, self._poll_list)

    # ── DPI ──
    def _apply_dpi_scaling(self):
        try:
            user32.GetDC.restype = wt.HDC
            user32.GetDeviceCaps.argtypes = [wt.HDC, ctypes.c_int]
            user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
            dc = user32.GetDC(0)
            dpi = user32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
            user32.ReleaseDC(0, dc)
            if dpi and dpi != 96:
                self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    # ── 后台线程 ──
    def _rescan_loop(self):
        while True:
            time.sleep(2)
            try:
                self.engine.refresh_windows()
            except Exception:
                pass

    def _hotkey_loop(self):
        self._hotkey_tid = kernel32.GetCurrentThreadId()
        MOD_NOREPEAT, MOD_CONTROL, MOD_SHIFT = 0x4000, 2, 4
        ok_f8 = user32.RegisterHotKey(None, 1, MOD_NOREPEAT, 0x77)       # F8
        ok_f9 = user32.RegisterHotKey(None, 2, MOD_NOREPEAT, 0x78)       # F9
        ok_til = user32.RegisterHotKey(None, 3, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x32)
        if not ok_f8:
            self.q.put(("log", "!! F8 热键被占用"))
        if not ok_til:
            self.q.put(("log", "!! Ctrl+Shift+2 热键被占用"))
        import ctypes.wintypes as w
        msg = w.MSG()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == 0x0312:  # WM_HOTKEY
                self.q.put(("hotkey", msg.wParam))
        user32.UnregisterHotKey(None, 1)
        user32.UnregisterHotKey(None, 2)
        user32.UnregisterHotKey(None, 3)

    # ── UI 轮询 ──
    def _poll_queue(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._append_log(val)
                elif kind == "hotkey":
                    if val == 1:
                        self.toggle()
                    elif val == 2:
                        self.on_close()
                    elif val == 3:
                        self.tile()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _poll_list(self):
        try:
            self.refresh_list(False)
            self._update_overlay()
        except Exception:
            log('列表/描边刷新异常:\n' + traceback.format_exc())
        self.root.after(1000, self._poll_list)  # 务必续链

    # ── 主控描边覆盖层 ──
    def _ov_redraw(self):
        try:
            w, h = self.overlay.winfo_width(), self.overlay.winfo_height()
            b = 6
            cv = self._ov_canvas
            cv.delete('all')
            cv.create_rectangle(b, b, max(w - b, b), max(h - b, b),
                                fill=self._ov_key, outline='')
        except Exception:
            pass

    def _make_clickthrough(self):
        """给覆盖层加 WS_EX_LAYERED|TRANSPARENT|NOACTIVATE: 鼠标完全穿透。"""
        try:
            hwnd = int(self.overlay.frame(), 16)
            GWL_EXSTYLE = -20
            u = user32
            u.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
            u.GetWindowLongPtrW.restype = ctypes.c_longlong
            u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_longlong]
            style = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            style |= 0x00080000 | 0x00000020 | 0x08000000
            u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ctypes.c_longlong(style))
            self._ov_ct_done = True
        except Exception:
            pass

    def _update_overlay(self):
        """把绿色描边盖在当前主控游戏窗口上 (跟随位置与主控变化)。"""
        try:
            h = self.engine.overlay_master()
        except Exception:
            h = None
        if not h or not user32.IsWindow(h):
            try:
                if self.overlay.state() == 'normal':
                    self.overlay.withdraw()
            except Exception:
                pass
            return
        r = get_rect(h)
        if not r:
            return
        b = 6
        x, y, w, hh = r[0] - b, r[1] - b, (r[2] - r[0]) + 2 * b, (r[3] - r[1]) + 2 * b
        if (h, x, y, w, hh) != getattr(self, '_ov_last', None):
            log(f'描边: 主控={h:#x} 位置=({x},{y}) 尺寸={w}x{hh} '
                f'模式={self.engine.master_mode}')
            self._ov_last = (h, x, y, w, hh)
        if self.overlay.state() == 'withdrawn':
            self.overlay.deiconify()
            if not self._ov_ct_done:
                self._make_clickthrough()
        self.overlay.geometry(f"{w}x{hh}{x:+d}{y:+d}")
        self._ov_redraw()

    # ── 主控指定 ──
    def _selected_hwnd(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(self.tree.item(sel[0], "values")[1], 16)
        except (ValueError, IndexError):
            return None

    def _on_double_click(self, _evt):
        h = self._selected_hwnd()
        if h:
            self.engine.set_manual_master(h)
            self.refresh_list(False)
            self._update_overlay()

    def _show_menu(self, evt):
        row = self.tree.identify_row(evt.y)
        if row:
            self.tree.selection_set(row)
            try:
                self.ctx_menu.tk_popup(evt.x_root, evt.y_root)
            finally:
                self.ctx_menu.grab_release()

    def _ctx_set_master(self):
        h = self._selected_hwnd()
        if h:
            self.engine.set_manual_master(h)
            self.refresh_list(False)
            self._update_overlay()

    # ── 一键多开 ──
    def multi_open(self):
        try:
            n = max(1, min(99, int(self.var_open_count.get())))
        except ValueError:
            n = 2
        lp = self.var_launch.get().strip() or detect_launch_path()
        if not lp or not os.path.exists(lp):
            self.q.put(("log", f"!! 游戏路径不存在: {lp or '(未填写)'}"))
            return
        CFG["launch_path"] = lp
        save_cfg(CFG)
        v5_exe = ensure_v5_local()
        if not v5_exe:
            v5_exe = CFG.get("v5_path", "")
        if not v5_exe or not os.path.exists(v5_exe):
            self.q.put(("log", "!! 未找到 V5 多开器 (把 V5.exe 所在目录填入配置文件 v5_src_dir)"))
            return
        CFG["v5_path"] = v5_exe
        save_cfg(CFG)
        layout_str = self.var_layout.get()  # tk 变量只能在主线程读
        self.q.put(("log", f"[多开] 开始启动 {n} 个实例 (模拟手动节奏, 每 3 秒一个)..."))
        threading.Thread(target=self._multi_open_worker, args=(n, lp, layout_str, v5_exe),
                         daemon=True).start()

    def _multi_open_worker(self, n, lp, layout_str, v5_exe):
        try:
            ok, opened = self.engine.launch_multi(v5_exe, lp, n)
            if not ok:
                self.q.put(("log", "[多开] 未启动任何新实例 (检查 V5 路径与游戏路径, 详情见日志文件)"))
                return
            layout = None
            if layout_str != "自动":
                try:
                    rows = int(layout_str.split("行")[0])
                    cols = int(layout_str.split("行")[1].rstrip("列"))
                    layout = (cols, rows)
                except Exception:
                    layout = None
            self.engine.tile_windows(layout)
            self.q.put(("log", f"[多开] 完成: {ok} 个新实例已平铺, 按 F8 即可开启同步"))
        except BaseException:
            tb = traceback.format_exc()
            log("多开崩溃:\n" + tb)
            self.q.put(("log", "!! 多开执行异常 (详情见日志文件): " + tb.strip().splitlines()[-1]))

    # ── 功能 ──
    def tile(self):
        """平铺: '自动' 用引擎选最优行列; 'a行b列' 手动指定。"""
        s = self.var_layout.get()
        if s == "自动":
            self.engine.tile_windows()
            return
        try:
            rows = int(s.split("行")[0])
            cols = int(s.split("行")[1].rstrip("列"))
            self.engine.tile_windows((cols, rows))
        except Exception:
            self.engine.tile_windows()

    def toggle(self):
        if self.engine.running:
            self.engine.stop()
            self.var_status.set("○ 同步已停止")
            self.var_status_fg("#888")
            self.btn_toggle.config(text="开启同步 (F8)")
        else:
            self.engine.start()
            if self.engine.running:
                self.var_status.set("● 同步中")
                self.var_status_fg("#c00")
                self.btn_toggle.config(text="停止同步 (F8)")
            else:
                self.var_status.set("○ 同步已停止")
                self.var_status_fg("#888")

    def var_status_fg(self, color):
        self._status_lbl.config(fg=color)

    def refresh_list(self, manual):
        hwnds, titles = find_game_windows()
        fg = user32.GetForegroundWindow() or 0
        self.var_count.set(f"窗口: {len(hwnds)}")
        is_manual = self.engine.master_mode == "manual"
        mm = self.engine.manual_master
        # 以窗口句柄为固定行 id 原地更新, 不整表重建 (保住选中状态/滚动位置)
        want = {}
        for h in hwnds:
            r = get_rect(h)
            if is_manual:
                role = "★主控(手动)" if h == mm else "受控"
            else:
                role = "★主控(前台)" if h == fg else "受控"
            want[hex(h)] = (role, hex(h),
                            f"{r[0]},{r[1]} - {r[2]},{r[3]}" if r else "?",
                            titles.get(h, ""))
        for i in self.tree.get_children():
            if i not in want:
                self.tree.delete(i)
        for key, values in want.items():
            if self.tree.exists(key):
                if tuple(self.tree.item(key, "values")) != values:
                    self.tree.item(key, values=values)
            else:
                self.tree.insert("", "end", iid=key, values=values,
                                 tags=('master',) if role.startswith("★") else ())

    def _append_log(self, text):
        self.txt.config(state="normal")
        self.txt.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def on_close(self):
        try:
            self.engine.stop()
        except Exception:
            pass
        if self._hotkey_tid:
            user32.PostThreadMessageW(wt.DWORD(self._hotkey_tid), WM_QUIT, 0, 0)
        self.root.destroy()


def main():
    if not ensure_admin_gui():
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # 若带控制台调试启动
    except Exception:
        pass
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        log("GUI崩溃:\n" + tb)
        ctypes.windll.user32.MessageBoxW(0, tb[-1500:], "同步器错误", 0x30 | 0x1000)


if __name__ == "__main__":
    main()
