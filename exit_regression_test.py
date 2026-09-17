# -*- coding: utf-8 -*-
"""回归测试：关掉 GUI 之后，整棵进程树必须**自己**消失。

为什么必须单独有这条测试：
    proc_tree.run_exe() 的 finally 里会 shutdown(proc, job)，顺手把还挂着的
    引导器父进程杀掉 —— 于是「父进程卡死」这个 bug 在既有测试里永远显示为
    「干净」。本测试**故意不杀任何进程**，只观察：如果它自己不走，就是失败。
    这正是 MEMORY 第七条第 3 点「检查通过 != 检查真的在跑」的落地。

判据（每一轮都要满足）：
    1. 窗口能在 40s 内出现（等不到就作废这一轮，不计入）
    2. 关窗后 **子进程**（真正跑代码那个）10s 内消失
    3. 关窗后 **整棵树**（含引导器父进程）25s 内消失
    4. 父进程退出后 _MEI 临时目录被清干净（这证明它走完了收尾流程）
    5. 全程没有出现「无响应」（IsHungAppWindow）

用法：
    python exit_regression_test.py [<exe>] [轮数]
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import exit_hang_probe as P   # noqa: E402
import proc_tree              # noqa: E402
import shot                   # noqa: E402

user32 = P.user32
WM_CLOSE = P.WM_CLOSE

FIND_TIMEOUT = 40.0     # 等窗口出现
CHILD_LIMIT = 10.0      # 子进程必须在这个时间内消失
TREE_LIMIT = 25.0       # 整棵树（含引导器父进程）必须在这个时间内消失

results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok, got, want))
    print("  [%s] %s" % ("OK" if ok else "!!", name))
    if not ok:
        print("        实际=%r  期望=%r" % (got, want))
    return ok


def one_round(exe, idx, total):
    print()
    print("--- 第 %d/%d 轮 ---" % (idx, total))
    before_mei = proc_tree.mei_usage()

    # 纯 Popen：不用 Job Object，和用户双击一致。
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
        print("  等不到窗口（受控会话建 Tk 不稳），本轮作废")
        try:
            proc.kill()
        except Exception:
            pass
        return None

    hung = False
    time.sleep(0.8)
    user32.PostMessageW(hwnd, WM_CLOSE, None, None)

    t0 = time.time()
    child_gone = tree_gone = None
    while time.time() - t0 < TREE_LIMIT:
        if user32.IsHungAppWindow(hwnd):
            hung = True
        if child_gone is None and not proc_tree.alive(child):
            child_gone = time.time() - t0
        if not proc_tree.alive(parent):
            tree_gone = time.time() - t0
            break
        time.sleep(0.1)

    print("  子进程消失: %s   整棵树消失: %s   无响应: %s"
          % ("%.2fs" % child_gone if child_gone is not None else "**从未**",
             "%.2fs" % tree_gone if tree_gone is not None else "**从未**",
             hung))

    check("子进程 %.0fs 内自己退出" % CHILD_LIMIT,
          child_gone is not None and child_gone <= CHILD_LIMIT, True)
    check("整棵树（含引导器父进程）%.0fs 内自己退出" % TREE_LIMIT,
          tree_gone is not None, True)
    check("关闭过程没有出现「无响应」", hung, False)

    # 父进程走完收尾 = _MEI 被它删掉。这是端到端的证据。
    time.sleep(1.0)
    after_mei = proc_tree.mei_usage()
    check("_MEI 临时目录没有残留（%d -> %d 个）" % (before_mei[0], after_mei[0]),
          after_mei[0] <= before_mei[0], True)

    # 只有在失败时才收尾，避免机器被垃圾堆满。杀之前先说明白 ——
    # 这里动手杀本身就说明「它自己不会走」，是失败信号。
    if tree_gone is None:
        print("  （本轮失败，测试代为清理残留进程）")
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
    args = sys.argv[1:]
    rounds = 5
    if args and args[-1].isdigit():
        rounds = int(args.pop())
    exe = args[0] if args else os.path.join(
        os.path.expanduser("~"), "Desktop", "校园网自动登录.exe")
    if not os.path.isfile(exe):
        print("找不到 exe: %s" % exe)
        return 1

    print("被测: %s" % exe)
    print("轮数: %d" % rounds)
    print("=" * 66)
    print("注意：本测试**不主动杀进程**。若某轮失败，日志会明确标注。")
    print("=" * 66)

    done = 0
    for i in range(1, rounds + 1):
        if one_round(exe, i, rounds):
            done += 1

    print()
    print("=" * 66)
    bad = [r for r in results if not r[1]]
    print("有效轮次 %d/%d，检查项 %d，失败 %d" % (done, rounds, len(results), len(bad)))
    for name, _, got, want in bad:
        print("  !! %s（实际=%r 期望=%r）" % (name, got, want))
    if bad or done < rounds:
        print("结论: 失败")
        return 1
    print("结论: 通过 —— 关窗后进程树自己会清干净")
    return 0


if __name__ == "__main__":
    sys.exit(main())
