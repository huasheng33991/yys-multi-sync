"""阴阳师桌面版 多开后台同步器 (正式版)

已验证结论: 游戏过滤带注入标记的输入(SendInput), 但接受干净的窗口消息(PostMessage),
且对非前台窗口同样生效 → 采用 "低级钩子观察物理输入 + PostMessage 后台转发" 架构。

用法:
    python -I sync_full.py            (非管理员自动 UAC 提权)
热键:
    F8           = 开/关 同步
    Ctrl+Shift+2 = 平铺排列所有阴阳师窗口
    F9           = 退出
"""

import ctypes
import ctypes.wintypes as wt
import math
import os
import random
import subprocess
import sys
import threading
import time
import traceback

# ── 可调参数 ────────────────────────────────────────────────────
TARGET_TITLE_KEY = "阴阳师"       # 窗口标题关键字
MOVE_THROTTLE_MS = 8              # 鼠标移动转发最小间隔
RESCAN_INTERVAL_S = 2             # 窗口列表刷新周期
CLICK_JITTER_PX = 10              # 受控窗口点击随机偏移上限 (像素, 0=关闭)
CLICK_JITTER_INFLUENCE = 1.0      # 偏移聚集度 (1.0 时 σ≈2px, 模拟真人手抖)
LOG_EVENTS = False                # True 则记录每次点击事件到日志(调试用)

# 打包版 (PyInstaller) 下日志放在 exe 旁边, 而不是解包临时目录
if getattr(sys, "frozen", False):
    _LOG_BASE = os.path.dirname(sys.executable)
else:
    _LOG_BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_LOG_BASE, "sync_click.log")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _dpi_aware():
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass


_dpi_aware()

# ── Win32 常量 ──────────────────────────────────────────────────
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
HC_ACTION = 0
LLKHF_INJECTED = 0x10
LLKHF_EXTENDED = 0x01
LLKHF_ALTDOWN = 0x20

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E

MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010
MK_SHIFT, MK_CONTROL = 0x0004, 0x0008
CS_DBLCLKS = 0x0008
GCL_STYLE = -26
SM_CXDOUBLECLK, SM_CYDOUBLECLK = 36, 37
WM_HOTKEY = 0x0312
MOD_NOREPEAT, MOD_CONTROL, MOD_SHIFT, MOD_ALT = 0x4000, 0x0002, 0x0004, 0x0001
VK_F8, VK_F9, VK_KEY_2 = 0x77, 0x78, 0x32
WM_QUIT = 0x0012
QS_ALLINPUT = 0x04FF
PM_REMOVE = 0x0001
ERROR_ALREADY_EXISTS = 183


# ── 结构体 ──────────────────────────────────────────────────────
class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wt.WPARAM, wt.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

# ── API 原型 ────────────────────────────────────────────────────
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.GetForegroundWindow.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = ctypes.c_void_p
user32.SetDlgItemTextW.argtypes = [wt.HWND, ctypes.c_int, wt.LPCWSTR]
user32.SetDlgItemTextW.restype = wt.BOOL
user32.GetDlgItemTextW.argtypes = [wt.HWND, ctypes.c_int, wt.LPWSTR, ctypes.c_int]
user32.GetDlgItemTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL]
user32.GetDoubleClickTime.restype = wt.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
class_long = user32.GetClassLongPtrW if hasattr(user32, "GetClassLongPtrW") else user32.GetClassLongW
class_long.restype = ctypes.c_size_t
class_long.argtypes = [wt.HWND, ctypes.c_int]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


# ── 提权 ────────────────────────────────────────────────────────
def ensure_admin():
    if ctypes.windll.shell32.IsUserAnAdmin():
        log("已以管理员运行")
        return
    if getattr(sys, "frozen", False):  # 打包版: 直接以管理员重启 exe
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, None, None, 1)
        if ret > 32:
            sys.exit(0)
        print("!! 未获得管理员权限 (游戏以管理员运行, 同步器必须同级提权)")
        input("按回车关闭...")
        sys.exit(1)
    python = sys.executable
    script = os.path.abspath(__file__)
    args = [python, "-I", script]
    params = " ".join(f'"{a}"' for a in args)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", python, params, None, 1)
    log(f"ShellExecuteW(runas) 返回 {ret}")
    if ret > 32:
        sys.exit(0)
    print("!! 未获得管理员权限 (游戏以管理员运行, 同步器必须同级提权)")
    input("按回车关闭...")
    sys.exit(1)


# ── 窗口枚举 ────────────────────────────────────────────────────
def _proc_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        s = wt.DWORD(260)
        b = ctypes.create_unicode_buffer(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(s)):
            return b.value
    finally:
        kernel32.CloseHandle(h)
    return ""


def count_game_procs():
    """当前 onmyoji.exe 进程数 (实例的真相, 比窗口数可靠)。"""
    try:
        r = os.popen('tasklist /fi "IMAGENAME eq onmyoji.exe" /fo csv /nh').read()
        return r.count("onmyoji")
    except Exception:
        return -1


def find_game_windows():
    """返回 (hwnd 列表, {hwnd: 标题})。
    双重过滤: 标题含关键字 且 进程必须是 onmyoji.exe
    (排除同步器自身窗口、浏览器标签页等标题带"阴阳师"的无关窗口)。"""
    hwnds, titles = [], {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = os.path.basename(_proc_name(pid.value) or "").lower()
            if "onmyoji" not in proc:
                return True
            t = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, t, 256)
            if TARGET_TITLE_KEY in t.value:
                hwnds.append(int(hwnd))
                titles[int(hwnd)] = t.value
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return hwnds, titles


# ── 工具 ────────────────────────────────────────────────────────
def pack_point(x, y):
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def scale_point(master_rect, slave_rect, x, y):
    mw = master_rect[2] - master_rect[0]
    mh = master_rect[3] - master_rect[1]
    sw = slave_rect[2] - slave_rect[0]
    sh = slave_rect[3] - slave_rect[1]
    rx = (x - master_rect[0]) / mw if mw > 0 else 0.0
    ry = (y - master_rect[1]) / mh if mh > 0 else 0.0
    return slave_rect[0] + int(rx * sw), slave_rect[1] + int(ry * sh)


def get_rect(hwnd):
    r = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (r.left, r.top, r.right, r.bottom)
    return None


def has_dblclks(hwnd):
    try:
        return bool(class_long(hwnd, GCL_STYLE) & CS_DBLCLKS)
    except Exception:
        return False


def jitter_point(x, y):
    """受控窗口的拟人点击偏移 (方案移植 assttyys_autojs getRegionBiasRnd2):
    以真实点击点为中心的二维正态分布, σ=0.1/influence²×(上限×2)。
    按下/抬起共用同一偏移点; 双击两下亦共用 (见引擎 _last_jitter)。"""
    half = CLICK_JITTER_PX
    inf = CLICK_JITTER_INFLUENCE
    u = v = 0
    while u == 0:
        u = random.random()
    while v == 0:
        v = random.random()
    dx = math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)
    dy = math.sqrt(-2.0 * math.log(u)) * math.sin(2.0 * math.pi * v)
    for _ in range(8):
        sd = 0.1 / (inf * inf) * (half * 2)
        jx, jy = dx * sd, dy * sd
        if abs(jx) <= half and abs(jy) <= half:
            break
        inf *= 2  # 越界: 放大方差重采样 (同参考实现)
    return int(x + jx), int(y + jy)


# ── 同步引擎 ────────────────────────────────────────────────────
class Engine:
    # 鼠标按钮: (MK标志, UP消息, DBLCLK消息); 键 = 按下/抬起/双击消息
    DOWN_TO_SPEC = {
        WM_LBUTTONDOWN: (MK_LBUTTON, WM_LBUTTONUP, WM_LBUTTONDBLCLK),
        WM_RBUTTONDOWN: (MK_RBUTTON, WM_RBUTTONUP, WM_RBUTTONDBLCLK),
        WM_MBUTTONDOWN: (MK_MBUTTON, WM_MBUTTONUP, WM_MBUTTONDBLCLK),
        WM_LBUTTONUP: (MK_LBUTTON, WM_LBUTTONUP, WM_LBUTTONDBLCLK),
        WM_RBUTTONUP: (MK_RBUTTON, WM_RBUTTONUP, WM_RBUTTONDBLCLK),
        WM_MBUTTONUP: (MK_MBUTTON, WM_MBUTTONUP, WM_MBUTTONDBLCLK),
    }

    def __init__(self):
        self.running = False
        self.notify = print  # UI 可覆写 (GUI 版注入队列回调); 默认控制台
        self._mutex = None
        # 受控窗口快照 (定时线程重建, 钩子线程无锁读取)
        self._game_hwnds = tuple()
        self._titles = {}
        self._lock = threading.Lock()
        # 配对/判定状态
        self._pressed = {}          # hwnd -> {MK标志: 客户坐标(x,y)}
        self._mk_state = 0          # 物理按键镜像
        self._last_move_ms = 0
        self._last_down = {}        # MK标志 -> (time, x, y)
        self._dbl_time = 500
        self._dbl_dx, self._dbl_dy = 4, 4
        self._dblcls_cache = {}
        self._last_jitter = {}      # MK标志 -> 双击两下共用的偏移 (dx, dy)
        self._kb_hook = None
        self._ms_hook = None
        self._kb_proc = None
        self._ms_proc = None
        self._hook_thread = None
        self._stop_evt = threading.Event()
        # 主控模式: "auto"=跟随前台 | "manual"=手动指定窗口
        self.master_mode = "auto"
        self.manual_master = None

    def acquire_single_instance(self):
        """单实例保护: 双开会导致同一点击被转发两次。False = 已有实例在运行。"""
        h = kernel32.CreateMutexW(None, False, "Local\\YysSync.SingleInstance")
        self._mutex = h
        return bool(h) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    # ---- 快照 ----
    def refresh_windows(self):
        hwnds, titles = find_game_windows()
        with self._lock:
            old = set(self._game_hwnds)
            new = set(hwnds)
            self._game_hwnds = tuple(hwnds)
            self._titles = titles
        for h in old - new:
            self._pressed.pop(h, None)
            self._dblcls_cache.pop(h, None)
        if new - old:
            log(f"窗口刷新: 新纳入 {[titles[h] + '#' + hex(h) for h in new - old]}")
            self.notify(f"[窗口] 新纳入同步: {len(new - old)} 个 (当前共 {len(hwnds)} 个)")
        elif old - new:
            self.notify(f"[窗口] 移除 {len(old - new)} 个 (当前共 {len(hwnds)} 个)")
        # 手动主控窗口已关闭 → 回退自动模式
        if (self.master_mode == "manual" and self.manual_master
                and self.manual_master not in hwnds):
            self.master_mode = "auto"
            self.manual_master = None
            self.notify("[主控] 手动主控窗口已关闭, 恢复自动跟随前台")
        return hwnds, titles

    def _snapshot(self):
        with self._lock:
            return self._game_hwnds

    # ---- 前台主控判定 ----
    def _master_hwnd(self):
        fg = user32.GetForegroundWindow() or 0
        if self.master_mode == "manual":
            # 物理输入只会落在前台窗口: 仅当手动主控正处于前台时同步
            if (self.manual_master and fg == self.manual_master
                    and self.manual_master in self._snapshot()):
                return int(self.manual_master)
            return None
        if fg in self._snapshot():
            return int(fg)
        return None

    def set_manual_master(self, hwnd):
        """手动指定主控窗口 (同步源); 仅操作该窗口时, 其余窗口跟随。"""
        self.master_mode = "manual"
        self.manual_master = int(hwnd)
        self.notify(f"[主控] 已手动指定 {hex(hwnd)} — 仅操作该窗口时同步")

    def set_auto_master(self):
        """恢复自动模式: 主控 = 当前前台的游戏窗口。"""
        self.master_mode = "auto"
        self.manual_master = None
        self.notify("[主控] 已恢复自动 (跟随前台窗口)")

    # ---- 消息投递 ----
    def _post(self, hwnd, msg, wparam, lparam):
        return bool(user32.PostMessageW(hwnd, msg, wparam & 0xFFFFFFFF, lparam & 0xFFFFFFFF))

    def _forward_move(self, master, master_rect, x, y, mk):
        now = time.perf_counter()
        if (now - self._last_move_ms) * 1000 < MOVE_THROTTLE_MS:
            return
        self._last_move_ms = now
        for h in self._snapshot():
            if h == master or not user32.IsWindow(h):
                continue
            r = get_rect(h)
            if r is None:
                continue
            sx, sy = scale_point(master_rect, r, x, y)
            pt = wt.POINT(sx, sy)
            user32.ScreenToClient(h, ctypes.byref(pt))
            self._post(h, WM_MOUSEMOVE, mk, pack_point(pt.x, pt.y))

    def _repair_stuck(self):
        """自愈: 物理已松开但受控窗口仍欠 UP → 补发。"""
        for h, btns in list(self._pressed.items()):
            if not user32.IsWindow(h):
                self._pressed.pop(h, None)
                continue
            for mk, (cx, cy) in list(btns.items()):
                spec = next((v for v in self.DOWN_TO_SPEC.values() if v[0] == mk), None)
                if spec and not (user32.GetAsyncKeyState(spec[0] == MK_LBUTTON and 0x01 or
                                                          spec[0] == MK_RBUTTON and 0x02 or 0x04) & 0x8000):
                    self._post(h, spec[1], 0, pack_point(cx, cy))
                    btns.pop(mk, None)

    def _game_window_at(self, x, y):
        """光标落点对应的游戏窗口; 点在无关窗口(同步器/浏览器等)上返回 None。

        游戏内部嵌 CEF 容器/渲染子窗口, 其祖先链不落在主窗口本体上,
        因此分两级: 1) 沿父链找命中; 2) 命中窗口属于游戏进程时,
        按落点所在的窗口矩形归并 (多开窗口并列摆放时无歧义)。
        """
        snap = self._snapshot()
        pt = wt.POINT(x, y)
        cur = int(user32.WindowFromPoint(pt) or 0)
        hops = 0
        while cur and hops < 10:
            if cur in snap:
                return cur
            parent = int(user32.GetParent(cur) or 0)
            if not parent:
                break
            cur = parent
            hops += 1
        if cur:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(cur, ctypes.byref(pid))
            proc = os.path.basename(_proc_name(pid.value) or "").lower()
            if "onmyoji" not in proc:
                return None
        for h in snap:  # 几何回退: 落点在哪个游戏窗口矩形内
            r = get_rect(h)
            if r and r[0] <= x < r[2] and r[1] <= y < r[3]:
                return h
        return None

    # ---- 钩子回调 ----
    def mouse_cb(self, nCode, wParam, lParam):
        try:
            if nCode == HC_ACTION and self.running:
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if ms.flags & LLKHF_INJECTED:   # 其他软件注入的输入不转发
                    return self._end(nCode, wParam, lParam)

                spec = self.DOWN_TO_SPEC.get(wParam)

                # 松开: 与已投递的 DOWN 严格配对, 不受落点/前台限制 (防卡按下)
                if spec and wParam == spec[1]:
                    self._mk_state &= ~spec[0]
                    for h, btns in list(self._pressed.items()):
                        if spec[0] in btns:
                            cx, cy = btns[spec[0]]
                            wp = self._mk_state & ~spec[0] & 0xFFFF
                            if self._post(h, spec[1], wp, pack_point(cx, cy)):
                                btns.pop(spec[0], None)
                            if not btns:
                                self._pressed.pop(h, None)
                    return self._end(nCode, wParam, lParam)

                x, y = int(ms.pt.x), int(ms.pt.y)
                # 以光标落点所在窗口判定主控 (激活点击瞬间前台尚未切换)
                master = self._game_window_at(x, y)
                if master is None:
                    return self._end(nCode, wParam, lParam)
                if self.master_mode == "manual" and master != self.manual_master:
                    return self._end(nCode, wParam, lParam)  # 手动模式: 仅主控自身操作触发同步

                if spec and wParam in (0x0201, 0x0204, 0x0207):
                    self._mk_state |= spec[0]   # 维护物理按键镜像 (DOWN)

                master_rect = get_rect(master)
                if master_rect is None:
                    return self._end(nCode, wParam, lParam)

                if wParam == WM_MOUSEMOVE:
                    self._forward_move(master, master_rect, x, y, self._mk_state)

                elif wParam == WM_MOUSEWHEEL:
                    delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                    wp = ((delta & 0xFFFF) << 16) | (self._mk_state & 0xFFFF)
                    for h in self._snapshot():
                        if h == master or not user32.IsWindow(h):
                            continue
                        r = get_rect(h)
                        if r is None:
                            continue
                        sx, sy = scale_point(master_rect, r, x, y)
                        # 滚轮 lParam 是屏幕坐标
                        self._post(h, WM_MOUSEWHEEL, wp, pack_point(sx, sy))

                elif spec:  # DOWN (含双击判定)
                    mk, up_msg, dbl_msg = spec
                    prev = self._last_down.get(mk)
                    is_dbl = False
                    if prev:
                        dt = (ms.time - prev[0]) & 0xFFFFFFFF
                        if dt <= self._dbl_time and abs(x - prev[1]) <= self._dbl_dx \
                           and abs(y - prev[2]) <= self._dbl_dy:
                            is_dbl = True
                            self._last_down.pop(mk, None)
                    if not is_dbl:
                        self._last_down[mk] = (ms.time, x, y)
                    wp = (self._mk_state | mk) & 0xFFFF
                    for h in self._snapshot():
                        if h == master or not user32.IsWindow(h):
                            continue
                        r = get_rect(h)
                        if r is None:
                            continue
                        sx, sy = scale_point(master_rect, r, x, y)
                        pt = wt.POINT(sx, sy)
                        user32.ScreenToClient(h, ctypes.byref(pt))
                        # 拟人随机偏移: 每个受控窗口落点略微不同 (按/抬同点);
                        # 双击两下共用同一偏移, 避免超出系统双击容差被判成单击
                        if is_dbl and mk in self._last_jitter:
                            ddx, ddy = self._last_jitter[mk]
                        else:
                            jx, jy = jitter_point(pt.x, pt.y)
                            ddx, ddy = jx - pt.x, jy - pt.y
                            self._last_jitter[mk] = (ddx, ddy)
                        pt = wt.POINT(pt.x + ddx, pt.y + ddy)
                        send = dbl_msg if (is_dbl and self._dblcls_cache.get(h, has_dblclks(h))) else wParam
                        self._dblcls_cache[h] = self._dblcls_cache.get(h, has_dblclks(h))
                        if self._post(h, send, wp, pack_point(pt.x, pt.y)):
                            self._pressed.setdefault(h, {})[mk] = (pt.x, pt.y)
                    if LOG_EVENTS:
                        log(f"点击 {hex(wParam)} @({x},{y}) dbl={is_dbl}")
                    # 主控自身物理点击已自然生效; 顺便自愈
                    self._repair_stuck()
        except Exception:
            pass
        return self._end(nCode, wParam, lParam)

    def _end(self, nCode, wParam, lParam):
        return user32.CallNextHookEx(self._ms_hook, nCode, wParam, lParam)

    def keyboard_cb(self, nCode, wParam, lParam):
        try:
            if nCode == HC_ACTION and self.running:
                master = self._master_hwnd()
                if master is None:
                    return user32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.flags & LLKHF_INJECTED:
                    return user32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)
                is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                msg = wParam  # WM_KEYDOWN/WM_KEYUP/WM_SYSKEYDOWN/WM_SYSKEYUP
                lparam = 1 | (kb.scanCode << 16)
                if kb.flags & LLKHF_EXTENDED:
                    lparam |= 1 << 24
                if kb.flags & LLKHF_ALTDOWN:
                    lparam |= 1 << 29
                if not is_down:
                    lparam |= (1 << 30) | (1 << 31)
                for h in self._snapshot():
                    if h == master or not user32.IsWindow(h):
                        continue
                    self._post(h, msg, kb.vkCode, lparam)
        except Exception:
            pass
        return user32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)

    # ---- 钩子线程 ----
    def _hook_proc_thread(self):
        self._kb_proc = HOOKPROC(self.keyboard_cb)
        self._ms_proc = HOOKPROC(self.mouse_cb)
        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, None, 0)
        self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, None, 0)
        if not self._kb_hook or not self._ms_hook:
            self.notify(f"!! 钩子安装失败 (键盘={bool(self._kb_hook)} 鼠标={bool(self._ms_hook)})")
            log(f"钩子安装失败 kb={bool(self._kb_hook)} ms={bool(self._ms_hook)}")
            self.running = False
            return
        self._dbl_time = int(user32.GetDoubleClickTime() or 500)
        self._dbl_dx = max(user32.GetSystemMetrics(SM_CXDOUBLECLK) // 2, 1)
        self._dbl_dy = max(user32.GetSystemMetrics(SM_CYDOUBLECLK) // 2, 1)
        self.notify("[同步] 已开启 — 直接操作主控窗口, 其余窗口后台跟随 (再按 F8 停止)")
        log("同步开启")
        msg = wt.MSG()
        while not self._stop_evt.is_set():
            user32.MsgWaitForMultipleObjects(0, None, False, 100, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return
        # 卸载钩子前补发所有欠着的 UP
        for h, btns in list(self._pressed.items()):
            for mk, (cx, cy) in btns.items():
                spec = next((v for v in self.DOWN_TO_SPEC.values() if v[0] == mk), None)
                if spec:
                    self._post(h, spec[1], 0, pack_point(cx, cy))
        self._pressed.clear()
        if self._kb_hook:
            user32.UnhookWindowsHookEx(self._kb_hook)
        if self._ms_hook:
            user32.UnhookWindowsHookEx(self._ms_hook)
        self._kb_hook = self._ms_hook = None
        self.notify("[同步] 已停止")
        log("同步停止")

    def start(self):
        if self.running:
            return
        hwnds, _ = self.refresh_windows()
        if len(hwnds) < 2:
            self.notify("!! 只识别到 1 个阴阳师窗口, 没有可同步的目标 (用 V5多开器再开几个)")
            return
        self._stop_evt.clear()
        self.running = True
        self._hook_thread = threading.Thread(target=self._hook_proc_thread, daemon=True)
        self._hook_thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._stop_evt.set()
        t = self._hook_thread
        if t and t.is_alive():
            t.join(timeout=3)
        self._hook_thread = None

    # ---- 一键多开 (驱动 V5 多开器的沙盘隔离, 不做任何注入) ----
    def _all_v5_dialogs(self, timeout=8):
        """返回所有可见的 V5 #32770 对话框; 一个都没有时启动 V5 再找。"""
        launched = False
        deadline = time.time() + timeout

        def scan():
            found = []

            def cb(hwnd, _):
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                name = os.path.basename(_proc_name(pid.value) or "").lower()
                if name == "v5.exe" and user32.IsWindowVisible(hwnd):
                    c = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, c, 64)
                    if c.value == "#32770":
                        found.append(int(hwnd))
                return True

            user32.EnumWindows(WNDENUMPROC(cb), 0)
            return found

        while time.time() < deadline:
            found = scan()
            if found:
                log(f"V5 对话框: {[hex(d) for d in found]}")
                if len(found) > 1:
                    # 残留多个 V5 时只保留第一个, 其余关闭 (避免操作错窗口)
                    for extra in found[1:]:
                        user32.PostMessageW(extra, 0x0010, 0, 0)  # WM_CLOSE
                    self.notify(f"[多开] 发现 {len(found)} 个 V5 窗口, 已关闭多余的")
                    time.sleep(1)
                    found = scan()
                if found:
                    user32.ShowWindow(found[0], 9)
                    user32.SetForegroundWindow(found[0])
                    return found[0]
            if not launched:
                launched = True
                log("未发现 V5 窗口, 启动 V5 多开器")
                try:
                    subprocess.Popen([self._v5_exe],
                                     cwd=os.path.dirname(self._v5_exe) or ".",
                                     creationflags=subprocess.DETACHED_PROCESS)
                except Exception as ex:
                    log(f"启动 V5 失败: {ex!r}")
                    self.notify(f"!! 启动 V5 多开器失败: {ex!r}")
                    return None
            time.sleep(0.5)
        log("V5 对话框等待超时")
        return None

    def _child_by_ctrlid(self, dlg, ctrlid):
        got = [None]

        def cb(hwnd, _):
            if user32.GetDlgCtrlID(hwnd) == ctrlid:
                got[0] = int(hwnd)
                return False
            return True

        user32.EnumChildWindows(dlg, WNDENUMPROC(cb), 0)
        return got[0]

    def launch_multi(self, v5_path, launch_path, count):
        """通过 V5 的'隔离打开程序'按钮启动实例。

        节奏完全复刻手动操作: 每 3 秒点一次, 连续点, 不等上一个窗口。
        以"新进程数 / 新窗口数"双信号判断达标即停手 (进程先于窗口出现,
        所以不会超发; 被吞掉的点击由后续点击自然补上)。
        返回 (实际新增窗口数, 新窗口 hwnd 列表)。"""
        self._v5_exe = v5_path
        log(f"多开开始: count={count} v5={v5_path} launch={launch_path}")
        # 解压后首次运行: 集成的 v5/config.ini 里 LastPath 是空的, V5 启动
        # 时会读到空路径并在其自身初始化时覆盖注入 → 先把正确路径写进配置
        try:
            v5_cfg = os.path.join(os.path.dirname(v5_path), "config.ini")
            os.makedirs(os.path.dirname(v5_cfg), exist_ok=True)
            with open(v5_cfg, "w", encoding="gbk", errors="ignore") as f:
                f.write("[V5]\nTitle=V5 程序多开器 0.1 Beta\n"
                        f"LastPath={launch_path}\nurl=www.prayaya.com\nQuery=0\n")
        except Exception:
            pass
        dlg = self._all_v5_dialogs()
        if dlg is None:
            self.notify("!! 未找到 V5 多开器窗口 (检查 V5 路径)")
            return 0, []
        edit = self._child_by_ctrlid(dlg, 1000)
        btn = self._child_by_ctrlid(dlg, 1002)
        log(f"V5 控件: edit={hex(edit) if edit else None} btn={hex(btn) if btn else None}")
        if not edit or not btn:
            # 可能是不同版本的 V5 (ctrlid 不同), 记录实际控件结构便于适配
            kids = []

            def kcb(h, _):
                t = ctypes.create_unicode_buffer(128)
                user32.GetWindowTextW(h, t, 128)
                c = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(h, c, 64)
                kids.append(f"{user32.GetDlgCtrlID(h)}:{c.value}:{t.value}")
                return True

            user32.EnumChildWindows(dlg, WNDENUMPROC(kcb), 0)
            log(f"V5 控件结构异常: {kids}")
            self.notify("!! V5 对话框控件缺失 (期望 ctrlid 1000/1002), 详情见日志")
            return 0, []

        def inject_path():
            """写入路径 → 停顿观察是否被 V5 自身初始化覆盖 → 读回验证。"""
            user32.SetDlgItemTextW(dlg, 1000, launch_path)
            time.sleep(0.4)
            buf = ctypes.create_unicode_buffer(512)
            user32.GetDlgItemTextW(dlg, 1000, buf, 512)
            log(f"V5 路径注入: 期望={launch_path} 读回={buf.value!r}")
            return buf.value

        # 注入带重试: 首次运行 V5 的初始化可能延迟覆盖写入, 不生效就重写
        readback = ""
        for attempt in range(1, 6):
            readback = inject_path()
            if readback == launch_path:
                break
            self.notify(f"[多开] 路径注入第 {attempt} 次未生效 "
                        f"(V5 输入框: {readback!r}), 重写...")
            time.sleep(0.6)
        if readback != launch_path:
            self.notify(f"!! 游戏路径注入 V5 失败: V5 输入框实际是 {readback!r}")
            return 0, []

        start_procs = count_game_procs()
        start_windows = set(find_game_windows()[0])
        clicked = 0
        max_clicks = count + 4  # 容忍偶发吞点击, 自动补点
        deadline = time.time() + count * 3 + 100
        while clicked < max_clicks and time.time() < deadline:
            inject_path()  # 每次点击前重写, 防止 V5 自行清空路径
            user32.PostMessageW(btn, 0x00F5, 0, 0)  # BM_CLICK (异步, 同手点)
            clicked += 1
            hwnds, _ = find_game_windows()
            new_windows = [h for h in hwnds if h not in start_windows]
            grown = count_game_procs() - start_procs
            self.notify(f"[多开] 已点 {clicked} 次: 新窗口 {len(new_windows)}/{count}, "
                        f"新进程 {grown}/{count}")
            log(f"多开: 第{clicked}次点击, 新窗口={len(new_windows)} 新进程={grown}")
            if len(new_windows) >= count or grown >= count:
                log("多开: 数量达标, 停止点击")
                break
            time.sleep(3)  # 手动节奏: 每 3 秒一次

        # 收尾: 等窗口全部出现 (最长 60 秒; 进程已在加载的会陆续出窗)
        remain_deadline = time.time() + 60
        while time.time() < remain_deadline:
            hwnds, _ = find_game_windows()
            new_windows = [h for h in hwnds if h not in start_windows]
            if len(new_windows) >= count:
                break
            time.sleep(1)
        hwnds, _ = find_game_windows()
        new_windows = [h for h in hwnds if h not in start_windows]

        log(f"多开结束: 点击 {clicked} 次, 新窗口 {len(new_windows)}/{count}, "
            f"进程 {start_procs}→{count_game_procs()}")
        if len(new_windows) < count:
            self.notify(f"!! 实际新增 {len(new_windows)}/{count} 个窗口 (已点 {clicked} 次)")
        else:
            self.notify(f"[多开] {count} 个实例全部启动")
        return len(new_windows), new_windows

    # ---- 平铺 (毕方式: 保持游戏宽高比, 防止游戏自行改尺寸) ----
    def tile_windows(self, layout=None):
        """等尺寸网格平铺, 单元格保持游戏自然宽高比。

        实测直接把窗口压进任意形状的单元格, 游戏会自行拉回比例
        (1280x1380 被游戏改成 2384x1380), 所以单元格必须按比例算。
        layout: None=自动选最优行列; 或 (cols, rows)。
        """
        hwnds, _ = find_game_windows()
        n = len(hwnds)
        if n < 2:
            self.notify("!! 窗口不足, 无需平铺")
            return
        rects = {h: get_rect(h) for h in hwnds}
        # 按当前位置从左到右、从上到下排 (空间顺序更符合直觉)
        order = sorted(hwnds, key=lambda h: (rects[h][1], rects[h][0]))

        # 工作区: 第一个窗口所在显示器
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wt.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.MonitorFromWindow.restype = ctypes.c_void_p  # HMONITOR 是指针宽度
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        hmon = user32.MonitorFromWindow(hwnds[0], 2)  # MONITOR_DEFAULTTONEAREST
        wa = None
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            wa = (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)
        if wa is None:
            wa = (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        W, H = wa[2] - wa[0], wa[3] - wa[1]

        # 游戏自然宽高比 = 当前各窗口比例均值 (防个别窗口已变形)
        ratios = [(r[2] - r[0]) / (r[3] - r[1]) for r in rects.values() if r[3] > r[1]]
        A = min(max(sum(ratios) / len(ratios), 0.5), 3.0) if ratios else 1.7

        if layout:
            cols, rows = layout
            cw = min(W // cols, int(H // rows * A))
            ch = int(cw / A)
            if cw * cols > W or ch * rows > H:
                self.notify(f"!! 行列 {rows}行x{cols}列 在当前分辨率下放不下")
                return
        else:
            best = None  # (单元面积, cols, rows, cw, ch)
            for cols in range(1, n + 1):
                rows = math.ceil(n / cols)
                cw1, ch1 = W // cols, int(W // cols / A)      # 宽度受限
                cw2, ch2 = int(H // rows * A), H // rows     # 高度受限
                cand = None
                if ch1 * rows <= H:
                    cand = (cw1, ch1)
                if cw2 * cols <= W and (
                        cand is None or cw2 * ch2 > cand[0] * cand[1]):
                    cand = (cw2, ch2)
                if cand and (best is None or cand[0] * cand[1] > best[0]):
                    best = (cand[0] * cand[1], cols, rows, cand[0], cand[1])
            if best is None:
                self.notify("!! 无法计算排布")
                return
            _, cols, rows, cw, ch = best

        # 整体在工作区内居中
        ox = wa[0] + (W - cols * cw) // 2
        oy = wa[1] + (H - rows * ch) // 2
        targets = {}
        for i, h in enumerate(order):
            cx, cy = i % cols, i // cols
            targets[h] = (ox + cx * cw, oy + cy * ch, cw, ch)
        for h, (x, y, w2, h2) in targets.items():
            user32.MoveWindow(h, x, y, w2, h2, True)
        self.notify(f"[平铺] {n} 个窗口 {cols}列x{rows}行 单元 {cw}x{ch}")
        log(f"平铺 {n} 窗口 cols={cols} rows={rows} cell={cw}x{ch} wa={wa} layout={layout}")

        # 游戏仍可能微调: 400ms 后复核, 偏了就压回去
        def verify():
            time.sleep(0.4)
            fixed = 0
            for h, (x, y, w2, h2) in targets.items():
                r = get_rect(h)
                if r and (abs(r[0] - x) > 2 or abs(r[1] - y) > 2
                          or abs(r[2] - r[0] - w2) > 2 or abs(r[3] - r[1] - h2) > 2):
                    user32.MoveWindow(h, x, y, w2, h2, True)
                    fixed += 1
            if fixed:
                log(f"平铺复核修正 {fixed} 个窗口")
        threading.Thread(target=verify, daemon=True).start()


# ── 主程序 ──────────────────────────────────────────────────────
def main():
    engine = Engine()
    if not engine.acquire_single_instance():
        print("!! 同步器已在运行 (单实例保护)")
        input("按回车关闭...")
        sys.exit(1)
    hwnds, titles = engine.refresh_windows()
    print(f"识别到 {len(hwnds)} 个阴阳师窗口:")
    for h in hwnds:
        r = get_rect(h)
        print(f"  hwnd={h:#x} rect={r} '{titles.get(h, '')}'")

    ok_f8 = user32.RegisterHotKey(None, 1, MOD_NOREPEAT, VK_F8)
    ok_f9 = user32.RegisterHotKey(None, 2, MOD_NOREPEAT, VK_F9)
    ok_tile = user32.RegisterHotKey(None, 3, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_KEY_2)
    if not ok_f8:
        print("!! F8 热键注册失败 (可能被占用)")
    if not ok_tile:
        print("!! Ctrl+Shift+2 热键注册失败 (可能被占用)")
    log(f"热键: F8={ok_f8} F9={ok_f9} 平铺={ok_tile}")

    # 后台窗口扫描线程: 新开窗口自动纳入
    def rescan():
        while True:
            time.sleep(RESCAN_INTERVAL_S)
            try:
                engine.refresh_windows()
            except Exception:
                pass
    threading.Thread(target=rescan, daemon=True).start()

    print("\n================ 阴阳师多开同步器 ================")
    print("  F8           = 开/关 同步 (开启后直接操作主控窗口)")
    print("  Ctrl+Shift+2 = 平铺排列所有阴阳师窗口 (对齐坐标更准)")
    print("  F9           = 退出")
    print("=====================================================")
    print("说明: 同步使用后台消息投递, 不会切换窗口、不会移动你的光标\n")

    msg = wt.MSG()
    while True:
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r <= 0:
            break
        if msg.message == WM_HOTKEY:
            if msg.wParam == 1:      # F8 开关
                if engine.running:
                    engine.stop()
                else:
                    engine.start()
            elif msg.wParam == 2:    # F9 退出
                engine.stop()
                print("退出")
                break
            elif msg.wParam == 3:    # 平铺
                engine.tile_windows()
    user32.UnregisterHotKey(None, 1)
    user32.UnregisterHotKey(None, 2)
    user32.UnregisterHotKey(None, 3)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    log(f"启动: argv={sys.argv} admin={ctypes.windll.shell32.IsUserAnAdmin()}")
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        print(tb)
        log("崩溃:\n" + tb)
        try:
            input("发生错误, 按回车关闭...")
        except Exception:
            pass
