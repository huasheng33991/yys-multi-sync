"""V5 注入诊断: 枚举 V5 进程/对话框, 读改写路径框, 记录每一步结果。"""
import ctypes
import ctypes.wintypes as wt
import importlib.util
import os
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('sync_full', r'D:\demo\dj\yys_sync\sync_full.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
u = m.user32
k32 = m.kernel32
LOG = r'D:\demo\dj\yys_sync\v5_inject_test_log.txt'
lines = []


def out(s):
    print(s)
    lines.append(str(s))


TARGET = r'C:\Program Files\yys\Launch.exe'
WNDENUM = m.WNDENUMPROC
u.GetDlgCtrlID.argtypes = [wt.HWND]
u.GetDlgCtrlID.restype = ctypes.c_int
u.SetDlgItemTextW.argtypes = [wt.HWND, ctypes.c_int, wt.LPCWSTR]
u.SetDlgItemTextW.restype = wt.BOOL
u.GetDlgItemTextW.argtypes = [wt.HWND, ctypes.c_int, wt.LPWSTR, ctypes.c_int]
u.GetDlgItemTextW.restype = ctypes.c_int

out('=== 当前 v5.exe 进程 ===')
os.system('tasklist /fi "IMAGENAME eq V5.exe" /fo csv /nh >> nul')
r = os.popen('tasklist /fi "IMAGENAME eq V5.exe" /fo csv /nh').read()
out(r.strip() or '(无)')

# 找所有 V5 对话框
dialogs = []


def cb(hwnd, _):
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = m._proc_name(pid.value)
    if name and name.lower().endswith('v5.exe') and u.IsWindowVisible(hwnd):
        c = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, c, 64)
        if c.value == '#32770':
            dialogs.append(int(hwnd))
    return True


u.EnumWindows(WNDENUM(cb), 0)
out(f'V5 对话框: {[hex(d) for d in dialogs]}')

for dlg in dialogs:
    t = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(dlg, t, 256)
    out(f'--- 对话框 {dlg:#x} "{t.value}"')
    for cid in (1000, 1001, 1002):
        h = m._child_by_ctrlid(dlg, cid)
        if not h:
            out(f'  ctrlid {cid}: 不存在')
            continue
        buf = ctypes.create_unicode_buffer(512)
        n = u.GetDlgItemTextW(dlg, cid, buf, 512)
        out(f'  ctrlid {cid}: hwnd={h:#x} 当前文本="{buf.value}"')

    # 写入测试
    r = bool(u.SetDlgItemTextW(dlg, 1000, TARGET))
    buf = ctypes.create_unicode_buffer(512)
    u.GetDlgItemTextW(dlg, 1000, buf, 512)
    ok = buf.value == TARGET
    out(f'  写入测试: SetDlgItemTextW ret={r} 读回="{buf.value}" {"✓ 注入有效" if ok else "✗ 注入无效!"}')

# 顺带看集成的 v5 config.ini
cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'dist',
                   '阴阳师多开同步器_1.2', 'v5', 'config.ini')
alt = r'D:\demo\dj\yys_sync\dist\阴阳师多开同步器_1.2\v5\config.ini'
for p in (os.path.abspath(cfg), alt):
    if os.path.exists(p):
        out(f'--- {p}')
        out('  ' + open(p, encoding='gbk', errors='ignore').read().replace('\n', ' | '))

open(LOG, 'w', encoding='utf-8').write('\n'.join(lines))
print('DONE')
