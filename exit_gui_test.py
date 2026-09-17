# -*- coding: utf-8 -*-
"""点关闭按钮之后，程序多久才真的消失？—— 用「无响应」的官方判据来量。

背景：
    用户反馈「每次关闭程序时程序都会无响应，最后还得手动去关」。
    代码上看，`mainloop()` 返回后走的是 `os._exit(0)`，应该瞬间消失；
    所以**必须实测**，不能靠读代码下结论。

为什么用 IsHungAppWindow：
    Windows 给窗口标题加「（无响应）」用的就是这个 API（user32!IsHungAppWindow），
    它判断的是「窗口消息队列 5 秒没被处理」。直接量它，等于量用户看到的那四个字，
    比「进程还在不在」更贴近症状 —— 进程活着但界面正常的情况是存在的。

量三件事：
    t_window  从发出 WM_CLOSE 到**窗口消失**用了多久
    t_procs   从发出 WM_CLOSE 到**整棵进程树都没了**用了多久
    hung      期间有没有出现过 IsHungAppWindow == True

用法：
    python exit_gui_test.py [<exe>] [轮数]
"""
import ctypes
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths        # noqa: E402
import proc_tree    # noqa: E402
import shot         # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_CLOSE = 0x0010
# 「窗口还在吗」和「它卡住了吗」
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = ctypes.c_bool
user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
user32.IsHungAppWindow.restype = ctypes.c_bool
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p]
user32.PostMessageW.restype = ctypes.c_bool

# 找窗口最多等多久（冷启动解压 _MEI + 建界面，实测 5~9 秒）
FIND_TIMEOUT = 40.0
# 发出关闭之后最多观察多久
OBSERVE = 45.0


def tree_pids(root_pid):
    """启动器父进程 + 真正跑代码的子进程。onefile 是两个进程，别只看父的。"""
    return [root_pid] + proc_tree.children_of(root_pid)


def alive_any(root_pid):
    return [p for p in tree_pids(root_pid) if proc_tree.alive(p)]


def wait_window(root_pid, timeout=FIND_TIMEOUT):
    """等出可见窗口，返回 (hwnd, 拥有它的 pid)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for pid in tree_pids(root_pid):
            h = shot.find_window(pid, timeout=0.05)
            if h:
                return h, pid
        time.sleep(0.2)
    return None, None


def one_round(exe, n):
    print("-" * 62)
    print("第 %d 轮" % n)
    proc, job = proc_tree.start([exe])
    print("  启动器 pid=%s" % proc.pid)

    hwnd, win_pid = wait_window(proc.pid)
    if not hwnd:
        print("  !! 等不到窗口 —— 这一轮作废（受控会话建 Tk 不稳，不是被测对象的问题）")
        proc_tree.shutdown(proc, job)
        return None

    # 关之前先确认它是好的，否则「关的时候卡」可能是它一开始就卡
    hung_before = bool(user32.IsHungAppWindow(hwnd))
    print("  窗口 hwnd=%s 属于 pid=%s / 关闭前就无响应=%s" % (hwnd, win_pid, hung_before))

    t_close = time.time()
    if not user32.PostMessageW(hwnd, WM_CLOSE, None, None):
        print("  !! PostMessage(WM_CLOSE) 失败，err=%s" % ctypes.get_last_error())
        proc_tree.shutdown(proc, job)
        return None

    t_window = t_procs = None
    hung_seen = False
    hung_since = None
    while time.time() - t_close < OBSERVE:
        if t_window is None and not user32.IsWindow(hwnd):
            t_window = time.time() - t_close
        # 窗口还在的时候才问它卡没卡
        if t_window is None and user32.IsHungAppWindow(hwnd):
            hung_seen = True
            if hung_since is None:
                hung_since = time.time() - t_close
        if t_procs is None and not alive_any(proc.pid):
            t_procs = time.time() - t_close
        if t_window is not None and t_procs is not None:
            break
        time.sleep(0.1)

    left = alive_any(proc.pid)
    print("  窗口消失: %s" % ("%.2fs" % t_window if t_window is not None
                              else "**%ds 后还在**" % OBSERVE))
    print("  进程清空: %s" % ("%.2fs" % t_procs if t_procs is not None
                              else "**%ds 后还剩 %s**" % (OBSERVE, left)))
    print("  出现过「无响应」: %s%s" % (hung_seen,
                                     "" if hung_since is None
                                     else "（首次在 %.2fs）" % hung_since))

    proc_tree.shutdown(proc, job)
    return {"t_window": t_window, "t_procs": t_procs,
            "hung_seen": hung_seen, "hung_before": hung_before,
            "left": left}


def main():
    args = [a for a in sys.argv[1:]]
    rounds = 3
    if args and args[-1].isdigit():
        rounds = int(args.pop())
    exe = args[0] if args else paths.default_exe()
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 1
    print("被测: %s（%d 字节）" % (exe, os.path.getsize(exe)))
    print("轮数: %d" % rounds)

    results = []
    for i in range(1, rounds + 1):
        r = one_round(exe, i)
        if r:
            results.append(r)

    print("=" * 62)
    if not results:
        print("一轮都没跑成 —— 受控会话建不出 Tk 窗口。")
        print("这不是被测对象的问题，但也说明**这条路径在本机测不了**。")
        return 2

    hung = [r for r in results if r["hung_seen"]]
    slow_win = [r for r in results if r["t_window"] is None or r["t_window"] > 1.0]
    slow_proc = [r for r in results if r["t_procs"] is None or r["t_procs"] > 3.0]
    print("有效轮数: %d" % len(results))
    print("出现过「无响应」: %d 轮" % len(hung))
    print("窗口消失 > 1s 或没消失: %d 轮" % len(slow_win))
    print("进程清空 > 3s 或没清空: %d 轮" % len(slow_proc))

    bad = bool(hung or slow_win or slow_proc)
    if bad:
        print()
        print("结论: **复现了** —— 关闭时确实会卡。明细见上。")
        return 1
    print()
    print("结论: 没复现 —— 点关闭后窗口和进程都在 1s / 3s 内干净消失，且从未进入无响应。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
