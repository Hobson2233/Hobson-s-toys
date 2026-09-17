# -*- coding: utf-8 -*-
"""在窗口**还活着**的时候测「无响应」，并覆盖三种关闭时机。

为什么要单独写这个：
    exit_gui_test.py 里的 `IsHungAppWindow(hwnd)` 是在发完 WM_CLOSE **之后**测的，
    而窗口在 0.1 秒内就被 destroy 了 —— 之后 hwnd 失效，
    `IsHungAppWindow` 对无效句柄**恒返回 False**。所以那个「从未出现无响应」
    是**假阴性**，证明不了任何东西。

    要测用户看到的那四个字，必须在窗口**存在期间**持续测。

覆盖三种时机（用户可能在任何时刻点 X）：
    0.2s  窗口刚出来（初始状态轮询正忙）
    1.0s  轮询进行中
    3.0s  基本空闲

判据：
    - 窗口存在期间从未进入「无响应」
    - 关窗后窗口能消失
    - 关窗后整棵进程树 15s 内自己清空

用法：
    python close_timing_test.py [<exe>]
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import exit_hang_probe as P   # noqa: E402
import paths                  # noqa: E402
import proc_tree              # noqa: E402
import shot                   # noqa: E402

user32 = P.user32
WM_CLOSE = P.WM_CLOSE
# 必须显式声明：hwnd 是 64 位指针，默认 ctypes 会按 int 截断，
# 截断后的句柄 IsWindow 恒返回 False —— 会得到「窗口一直不存在」的假象。
user32.IsWindow.argtypes = [P.ctypes.c_void_p]
user32.IsWindow.restype = P.ctypes.c_int
user32.IsHungAppWindow.argtypes = [P.ctypes.c_void_p]
user32.IsHungAppWindow.restype = P.ctypes.c_int

FIND_TIMEOUT = 40.0
TREE_LIMIT = 15.0
DELAYS = [0.2, 1.0, 3.0]

results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok, got, want))
    print("  [%s] %s" % ("OK" if ok else "!!", name))
    if not ok:
        print("        实际=%r  期望=%r" % (got, want))


def one(exe, delay):
    print()
    print("--- 时机 %.1fs 后点 X ---" % delay)
    proc = subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    parent = proc.pid
    hwnd = child = None
    t0 = time.time()
    while time.time() - t0 < FIND_TIMEOUT:
        for pid in [parent] + proc_tree.children_of(parent):
            h = shot.find_window(pid, timeout=0.05)
            if h:
                hwnd, child = h, pid
                break
        if hwnd:
            break
        time.sleep(0.2)
    if not hwnd:
        print("  等不到窗口，本轮作废")
        try:
            proc.kill()
        except Exception:
            pass
        return None

    # 阶段 1：关窗**之前**，窗口存在期间，持续测「无响应」
    hung_before = False
    t0 = time.time()
    while time.time() - t0 < delay:
        if user32.IsHungAppWindow(hwnd):
            hung_before = True
        time.sleep(0.1)
    check("关窗前窗口从未「无响应」（测了 %.1fs）" % delay, hung_before, False)

    # 阶段 2：关窗，并在窗口**还活着**时继续测「无响应」
    hung_after = False
    user32.PostMessageW(hwnd, WM_CLOSE, None, None)
    t0 = time.time()
    window_gone = None
    while time.time() - t0 < 5.0:
        if user32.IsHungAppWindow(hwnd):
            hung_after = True
        if not user32.IsWindow(hwnd):
            window_gone = time.time() - t0
            break
        time.sleep(0.05)
    check("关窗后窗口消失了（%.2fs）" % (window_gone if window_gone is not None else -1),
          window_gone is not None, True)
    check("关窗过程中窗口没有「无响应」（窗口存活期间测的）", hung_after, False)

    # 阶段 3：整棵树必须自己清空
    t0 = time.time()
    tree_gone = None
    while time.time() - t0 < TREE_LIMIT:
        if not proc_tree.alive(parent):
            tree_gone = time.time() - t0
            break
        time.sleep(0.1)
    print("  整棵树消失: %s"
          % ("%.2fs" % tree_gone if tree_gone is not None else "**从未**"))
    check("整棵树 %.0fs 内自己清空" % TREE_LIMIT, tree_gone is not None, True)
    if tree_gone is None:
        print("  （本轮失败，测试代为清理）")
        try:
            proc.kill()
        except Exception:
            pass
        for pid in proc_tree.children_of(parent):
            try:
                proc_tree.kill_tree(pid)
            except Exception:
                pass
    return True


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else paths.default_exe()
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 1
    print("被测: %s" % exe)
    print("=" * 66)
    done = 0
    for d in DELAYS:
        if one(exe, d):
            done += 1
    print()
    print("=" * 66)
    bad = [r for r in results if not r[1]]
    print("有效轮次 %d/%d，检查项 %d，失败 %d" % (done, len(DELAYS), len(results), len(bad)))
    for name, _, got, want in bad:
        print("  !! %s（实际=%r 期望=%r）" % (name, got, want))
    if bad or done < len(DELAYS):
        print("结论: 失败")
        return 1
    print("结论: 通过 —— 三种时机关窗，窗口都不「无响应」，进程树都会自己清空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
