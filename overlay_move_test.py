"""覆盖层移动行为复现: 与 GUI 相同的创建方式, 在两个位置间交替移动并打印实际位置。"""
import ctypes
import ctypes.wintypes as wt
import tkinter as tk

root = tk.Tk()
root.geometry("300x100+50+50")
root.title("覆盖层测试(3秒后自动关闭)")

overlay = tk.Toplevel(root)
overlay.overrideredirect(True)
KEY = '#010203'
overlay.configure(bg=KEY)
overlay.attributes('-transparentcolor', KEY)
overlay.attributes('-topmost', True)
cv = tk.Canvas(overlay, highlightthickness=0, bd=0, bg='#22c55e')
cv.pack(fill='both', expand=True)
overlay.geometry("400x300+100+100")

# 点击穿透 (与 GUI 相同)
user32 = ctypes.WinDLL("user32")
uGWP = user32.GetWindowLongPtrW if hasattr(user32, "GetWindowLongPtrW") else user32.GetWindowLongW
uSWP = user32.SetWindowLongPtrW if hasattr(user32, "SetWindowLongPtrW") else user32.SetWindowLongW


def clickthrough():
    hwnd = int(overlay.frame(), 16)
    style = uGWP(hwnd, -20)
    uSWP(hwnd, -20, ctypes.c_longlong(style | 0x00080000 | 0x00000020 | 0x08000000))


def redraw():
    w, h = overlay.winfo_width(), overlay.winfo_height()
    cv.delete('all')
    cv.create_rectangle(6, 6, max(w - 6, 6), max(h - 6, 6), fill=KEY, outline='')


overlay.bind('<Configure>', lambda e: redraw())
overlay.update_idletasks()
clickthrough()

POS_A = "+100+100"
POS_B = "+1500+100"
states = []


def tick(n=0):
    pos = POS_A if n % 2 == 0 else POS_B
    overlay.geometry(f"400x300{pos}")
    overlay.update_idletasks()
    info = (f"step{n}: 目标{pos} 实际 winfo_x={overlay.winfo_x()} "
            f"y={overlay.winfo_y()} geometry={overlay.geometry()} "
            f"state={overlay.state()}")
    print(info)
    states.append(info)
    if n < 5:
        root.after(1500, lambda: tick(n + 1))
    else:
        root.after(200, root.destroy)


tick(0)
root.mainloop()

ok = all(('目标' + POS_A in s) == False or ('winfo_x=100' in s or 'winfo_x=1500' in s) for s in states)
moved = any('winfo_x=1500' in s for s in states) and any('winfo_x=100' in s for s in states)
print("移动测试:", "通过 ✓" if moved else "失败 ✗ (覆盖层没有真的移动)")
