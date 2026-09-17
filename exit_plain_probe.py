# -*- coding: utf-8 -*-
"""对照实验：**不用 Job Object**、不用 proc_tree，直接 Popen 起 exe，关掉窗口看父进程。

为什么要做这个对照：
    exit_hang_probe.py 走的是 proc_tree.start()，它会把进程放进一个
    「关掉句柄就杀掉里面所有进程」的 Job Object。Job 会连带影响子进程，
    万一引导器父进程的收尾和 Job 有交互，那测出来的「卡住」就是我自己的
    测试工具造成的假象 —— 而用户机器上根本没有 Job。

    这正是「先写能证伪自己判断的用例」：如果这里父进程正常退出，
    那前面的复现就作废，得换方向找原因。

用法：
    python exit_plain_probe.py [<exe>] [观察秒数]
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import exit_hang_probe as P   # noqa: E402  复用 cpu_seconds / threads_of / windows_of
import paths                  # noqa: E402
import shot                   # noqa: E402

user32 = P.user32
WM_CLOSE = P.WM_CLOSE


def main():
    args = sys.argv[1:]
    observe = 60.0
    if args and args[-1].replace(".", "").isdigit():
        observe = float(args.pop())
    exe = args[0] if args else paths.default_exe()
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 1

    print("被测: %s" % exe)
    print("对照条件: **不用 Job Object**，纯 Popen，和用户双击运行时一致")
    print("=" * 62)

    proc = subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    parent = proc.pid
    print("引导器父进程 pid=%s" % parent)

    hwnd = child = None
    t0 = time.time()
    while time.time() - t0 < 40:
        for pid in [parent] + P.proc_tree.children_of(parent):
            h = shot.find_window(pid, timeout=0.05)
            if h:
                hwnd, child = h, pid
                break
        if hwnd:
            break
        time.sleep(0.2)
    if not hwnd:
        print("!! 等不到窗口（受控会话建 Tk 不稳），这次对照无效")
        proc.kill()
        return 2
    print("子进程 pid=%s / 窗口 hwnd=%s" % (child, hwnd))
    time.sleep(0.8)

    print("发出 WM_CLOSE")
    user32.PostMessageW(hwnd, WM_CLOSE, None, None)

    t0 = time.time()
    exited_at = None
    prev = None
    while time.time() - t0 < observe:
        el = time.time() - t0
        if not P.proc_tree.alive(parent):
            exited_at = el
            break
        cpu = P.cpu_seconds(parent)
        dcpu = "" if prev is None or cpu is None else "  (+%.2fs CPU)" % (cpu - prev)
        print("  %6.1fs  父进程存活 线程=%s CPU=%.2fs%s"
              % (el, P.threads_of(parent), cpu if cpu is not None else -1, dcpu))
        prev = cpu
        time.sleep(5.0)

    print()
    print("=" * 62)
    if exited_at is not None:
        print("父进程在 %.2fs 后自己退出了。" % exited_at)
        print("=> **前面的复现是测试工具的假象**，得换方向找原因。")
        return 0
    print("父进程 %.0fs 后**仍然活着**（无 Job Object）。" % observe)
    P.describe_windows(parent, "父窗口")
    print()
    print("=> 不是测试工具造成的：用户双击运行时同样会留下这个进程。")
    try:
        proc.kill()
    except Exception:
        pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
