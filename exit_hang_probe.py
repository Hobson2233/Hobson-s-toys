# -*- coding: utf-8 -*-
"""关掉界面之后，那个卡住的引导器父进程到底在干什么？

exit_gui_test.py 已经量出：窗口 0.11s 就消失，但引导器父进程 45s 还在，
而且 `_MEI` 目录里 223 个文件**一个都没删掉** —— 说明它不是「删到一半卡住」，
而是压根没开始删。

这个探针回答三件事：
  1. 父进程是**忙等**（CPU 时间在涨）还是**阻塞**（CPU 不动）？
  2. 它有没有弹出什么窗口（比如 PyInstaller 的
     "Failed to remove temporary directory" 对话框）？连不可见的窗口一起枚举。
  3. 它到底会不会自己结束？（观察到 150s）

对照组：同样的 exe 跑 --selftest（不建界面），看父进程收尾是否正常。
这一条能区分「GUI 特有」还是「所有路径都这样」。

用法：
    python exit_hang_probe.py [<exe>] [gui|selftest]
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths        # noqa: E402
import proc_tree    # noqa: E402
import shot         # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = ctypes.c_bool
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p]
user32.PostMessageW.restype = ctypes.c_bool
kernel32.OpenProcess.restype = ctypes.c_void_p

OBSERVE = 150.0


def cpu_seconds(pid):
    """进程累计占用的 CPU 秒数（用户 + 内核）。拿不到返回 None。

    这是区分「忙等」和「阻塞」最直接的办法：忙等的进程这个数会一直涨。
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                        ctypes.byref(k), ctypes.byref(u)):
            return None
        to_s = lambda ft: (ft.dwHighDateTime << 32 | ft.dwLowDateTime) / 1e7
        return to_s(k) + to_s(u)
    finally:
        kernel32.CloseHandle(h)


def threads_of(pid):
    """进程当前线程数。"""
    h = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)   # TH32CS_SNAPTHREAD
    if h == -1:
        return -1
    class TE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD)]
    n = 0
    try:
        te = TE()
        te.dwSize = ctypes.sizeof(TE)
        if kernel32.Thread32First(h, ctypes.byref(te)):
            while True:
                if te.th32OwnerProcessID == pid:
                    n += 1
                te.dwSize = ctypes.sizeof(TE)
                if not kernel32.Thread32Next(h, ctypes.byref(te)):
                    break
    finally:
        kernel32.CloseHandle(h)
    return n


def windows_of(pid):
    """该 pid 名下**所有**顶层窗口（含不可见的），返回 [(hwnd, 类名, 标题, 可见)]。"""
    out = []

    def cb(hwnd, _):
        wpid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            n = user32.GetWindowTextLengthW(hwnd)
            t = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, t, n + 2)
            out.append((hwnd, cls.value, t.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), None)
    return out


def describe_windows(pid, label):
    ws = windows_of(pid)
    if not ws:
        print("     %s 名下没有顶层窗口" % label)
        return
    for hwnd, cls, title, vis in ws:
        print("     %s hwnd=%s 类=%-22s 可见=%-5s 标题=%r"
              % (label, hwnd, cls, vis, title))


def main():
    args = sys.argv[1:]
    mode = "gui"
    if args and args[-1] in ("gui", "selftest"):
        mode = args.pop()
    exe = args[0] if args else paths.default_exe()
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 1

    print("被测: %s" % exe)
    print("模式: %s" % mode)
    print("=" * 62)

    proc, job = proc_tree.start([exe] if mode == "gui" else [exe, "--selftest"])
    parent = proc.pid
    print("引导器父进程 pid=%s" % parent)

    child = None
    hwnd = None
    if mode == "gui":
        t0 = time.time()
        while time.time() - t0 < 40:
            for pid in [parent] + proc_tree.children_of(parent):
                h = shot.find_window(pid, timeout=0.05)
                if h:
                    hwnd, child = h, pid
                    break
            if hwnd:
                break
            time.sleep(0.2)
        if not hwnd:
            print("!! 等不到窗口，放弃")
            proc_tree.shutdown(proc, job)
            return 2
        print("子进程 pid=%s / 窗口 hwnd=%s" % (child, hwnd))
        time.sleep(0.8)
        print("发出 WM_CLOSE")
        user32.PostMessageW(hwnd, WM_CLOSE, None, None)
    else:
        # 等它自己跑完（--selftest 不建窗口）
        t0 = time.time()
        while time.time() - t0 < 60 and proc_tree.alive(parent):
            time.sleep(0.2)

    t0 = time.time()
    prev_cpu = None
    print()
    print("观察中（最多 %.0fs）……" % OBSERVE)
    while time.time() - t0 < OBSERVE:
        el = time.time() - t0
        parent_alive = proc_tree.alive(parent)
        cpu = cpu_seconds(parent) if parent_alive else None
        kids = proc_tree.children_of(parent)
        if not parent_alive:
            print("  %6.1fs  父进程已退出" % el)
            break
        dcpu = "" if (prev_cpu is None or cpu is None) else "  (+%.2fs CPU)" % (cpu - prev_cpu)
        print("  %6.1fs  父进程存活  线程=%s 子进程=%s CPU=%.2fs%s"
              % (el, threads_of(parent), kids,
                 cpu if cpu is not None else -1, dcpu))
        prev_cpu = cpu
        if hwnd is not None:
            print("           子进程窗口还在吗: %s" % bool(user32.IsWindow(hwnd)))
        describe_windows(parent, "父窗口")
        time.sleep(5.0)

    print()
    print("=" * 62)
    left = proc_tree.alive(parent)
    print("最终: 父进程 %s" % ("**仍然活着**" if left else "已退出"))
    if left:
        describe_windows(parent, "父窗口")
        print("CPU 累计: %.2fs" % (cpu_seconds(parent) or -1))
        print()
        print("结论: 引导器父进程**不会自己结束** —— 这就是用户看到的「关不掉」。")
    else:
        print("结论: 父进程正常收尾。")
    proc_tree.shutdown(proc, job)
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
