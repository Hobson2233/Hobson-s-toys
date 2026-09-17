# -*- coding: utf-8 -*-
"""带插桩的退出探针：关掉窗口后，把子进程自己的阶段日志读出来。

和 exit_plain_probe.py 的区别：
  1. 设 CAMPUS_EXIT_TRACE=1 启动，让程序自己往 exit-trace.log 打点；
  2. 这次盯的是**子进程**（真正跑代码那个），不是引导器父进程 ——
     前两轮探针只打父进程，而父进程本来就应该在等，看不出东西；
  3. 实时把 exit-trace.log 的增量打出来，卡在哪一行一目了然；
  4. 顺带看 exit-stacks.log 有没有内容：
     有栈 = 卡在 Python 层；空的 = 卡在 os._exit 的 C 层（定时器线程已被干掉）。

用法：
    python exit_trace_probe.py [<exe>] [观察秒数]
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import exit_hang_probe as P   # noqa: E402
import paths                  # noqa: E402
import shot                   # noqa: E402
import campus_login           # noqa: E402  只为了拿 data_dir()，导入它不会建窗口

user32 = P.user32
WM_CLOSE = P.WM_CLOSE


def read_trace():
    path = os.path.join(campus_login.data_dir(), "exit-trace.log")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def main():
    args = sys.argv[1:]
    observe = 70.0
    if args and args[-1].replace(".", "").isdigit():
        observe = float(args.pop())
    exe = args[0] if args else paths.default_exe()
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 1

    trace_path = os.path.join(campus_login.data_dir(), "exit-trace.log")
    stack_path = os.path.join(campus_login.data_dir(), "exit-stacks.log")
    for p in (trace_path, stack_path):
        try:
            os.remove(p)
        except OSError:
            pass

    print("被测: %s" % exe)
    print("插桩日志: %s" % trace_path)
    print("=" * 66)

    env = dict(os.environ)
    env["CAMPUS_EXIT_TRACE"] = "1"
    proc = subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)
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
        print("!! 等不到窗口（受控会话建 Tk 不稳），这轮作废")
        proc.kill()
        return 2
    print("子进程 pid=%s / 窗口 hwnd=%s" % (child, hwnd))
    time.sleep(0.8)

    print()
    print("--- 关闭前 exit-trace.log ---")
    for ln in read_trace():
        print("   " + ln)

    print()
    print("发出 WM_CLOSE")
    user32.PostMessageW(hwnd, WM_CLOSE, None, None)

    t0 = time.time()
    seen = len(read_trace())
    child_gone_at = None
    while time.time() - t0 < observe:
        el = time.time() - t0
        lines = read_trace()
        while seen < len(lines):
            print("  [%5.2fs] + %s" % (el, lines[seen]))
            seen += 1
        kids = P.proc_tree.children_of(parent)
        c_alive = child in kids or P.proc_tree.alive(child)
        if not c_alive and child_gone_at is None:
            child_gone_at = el
            print("  [%5.2fs] 子进程消失" % el)
        cpu = P.cpu_seconds(child)
        print("  [%5.2fs] 父存活=%-5s 子存活=%-5s 子CPU=%s"
              % (el, P.proc_tree.alive(parent), c_alive,
                 ("%.2fs" % cpu) if cpu is not None else "N/A"))
        time.sleep(3.0)

    print()
    print("=" * 66)
    print("--- 最终 exit-trace.log ---")
    for ln in read_trace():
        print("   " + ln)
    print()
    if os.path.isfile(stack_path):
        body = open(stack_path, "r", encoding="utf-8", errors="replace").read()
        print("--- exit-stacks.log（%d 字节）---" % len(body))
        print(body[:3000] if body.strip() else "   （空：说明卡在 C 层，定时器线程已没了）")
    else:
        print("--- exit-stacks.log 不存在 ---")

    print()
    print("=> 子进程消失于: %s" % ("%.2fs" % child_gone_at if child_gone_at is not None
                                   else "观察期内从未消失"))
    try:
        proc.kill()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
