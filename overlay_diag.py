# -*- coding: utf-8 -*-
"""overlay 全链路诊断: 引擎判定 -> 遮挡检测 -> 几何应用 -> 实际位置回读。"""
import ctypes
import ctypes.wintypes as wt
import importlib.util
import os
import sys
import time
import tkinter as tk

spec = importlib.util.spec_from_file_location('sync_full', r'D:\demo\dj\yys_sync\sync_full.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
user32 = m.user32
u = user32
u.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
u.GetWindowLongPtrW.restype = ctypes.c_longlong
u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_longlong]

engine = m.Engine()
engine.refresh_windows()

root = tk.Tk()
root.geometry("360x80+40+40")
root.title("overlay 诊断 (40 秒)")

overlay = tk.Toplevel(root)
overlay.overrideredirect(True)
KEY = '#010203'
overlay.configure(bg=KEY)
try:
    overlay.attributes('-transparentcolor', KEY)
    overlay.attributes('-topmost', True)
except Exception as e:
    print('transparentcolor/topmost 异常:', e)
cv = tk.Canvas(overlay, highlightthickness=0, bd=0, bg=KEY)
cv.pack(fill='both', expand=True)
overlay.bind('<Configure>', lambda e: cv.delete('all') or cv.create_rectangle(
    3, 3, max(overlay.winfo_width() - 3, 3), max(overlay.winfo_height() - 3, 3),
    outline='#22c55e', width=6))
overlay.withdraw()
ct_done = [False]


def clickthrough():
    try:
        hwnd = int(overlay.frame(), 16)
        style = u.GetWindowLongPtrW(hwnd, -20)
        u.SetWindowLongPtrW(hwnd, -20, ctypes.c_longlong(style | 0x00080000 | 0x00000020 | 0x08000000))
        return True
    except Exception as e:
        print('clickthrough 异常:', e)
        return False


def occluded(master, rect):
    area = max((rect[2] - rect[0]) * (rect[3] - rect[1]), 1)
    try:
        ov = int(overlay.frame(), 16)
    except Exception:
        ov = None
    try:
        rt = int(root.frame(), 16)
    except Exception:
        rt = None
    skip = {ov, rt}
    st = {'seen': False, 'hit': None}

    def cb(hwnd, _):
        if hwnd == master:
            st['seen'] = True
            return True
        if not st['seen'] or hwnd in skip:
            return True
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        try:
            val = wt.DWORD(0)
            ctypes.WinDLL('dwmapi').DwmGetWindowAttribute(
                wt.HWND(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val))
            if val.value:   # cloaked 幽灵窗口
                return True
        except Exception:
            pass
        r = m.get_rect(hwnd)
        if not r:
            return True
        ix = min(rect[2], r[2]) - max(rect[0], r[0])
        iy = min(rect[3], r[3]) - max(rect[1], r[1])
        if ix > 0 and iy > 0 and ix * iy >= 0.15 * area:
            st['hit'] = hwnd
            return False
        return True

    user32.EnumWindows(m.WNDENUMPROC(cb), 0)
    return st['hit']


def diag_tick(n=0):
    fg = user32.GetForegroundWindow() or 0
    h = engine.overlay_master()
    state = overlay.state()
    line = f"{n:2d}s fg={fg:#x} 主控={hex(h) if h else None} overlay={state}"
    if h and state == 'normal':
        r = m.get_rect(h)
        occ = occluded(h, r)
        x, y, w, hh = r[0], r[1], r[2] - r[0], r[3] - r[1]
        overlay.geometry(f"{w}x{hh}{x:+d}{y:+d}")
        overlay.attributes('-topmost', True)
        overlay.update_idletasks()
        line += (f" 目标=({x},{y}) {w}x{hh} 遮挡={hex(occ) if occ else False}"
                 f" 实际=({overlay.winfo_x()},{overlay.winfo_y()})")
        if state == 'withdrawn' or not ct_done[0]:
            overlay.deiconify()
            ct_done[0] = clickthrough()
    print(line, flush=True)
    if n < 40:
        root.after(1000, lambda: diag_tick(n + 1))
    else:
        root.destroy()


print('=== 40 秒诊断开始: 请点击进入任意游戏窗口并停留 ===', flush=True)
diag_tick(0)
root.mainloop()
print('=== 诊断结束 ===', flush=True)
