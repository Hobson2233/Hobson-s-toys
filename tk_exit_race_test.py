# -*- coding: utf-8 -*-
"""A/B 实验：Tk 用完窗口之后，`os._exit(0)` 和 `TerminateProcess` 哪个能干净退出？

═══════════════════════════════════════════════════════════════════════════════
⚠️ 2026-09-17 结论修正：**本实验的方法不可靠，它得出的结论是错的，别信它的输出。**

  实测结果（当时）：os_exit 卡 7/12、terminate 卡 10/12 →
  当时的结论：「两种方式都卡 → 卡点不在退出方式上，得另找」。

  **这个结论是错的。** 当天稍后用插桩（`exit_trace_probe.py`，在**真实打包 exe**
  里按阶段打点）测出：卡点精确落在 `os._exit` 内部的 `ExitProcess`，改用
  `TerminateProcess` 后关窗到整棵树清空只要 0.4~0.9 秒（5/5 轮）。

  本实验为什么不可靠（两个独立的原因，任一条都足以作废）：
    1. **替身不等价**：用 `pythonw.exe` + 裸 Tk 脚本模拟 `--windowed` 打包后的子进程。
       两者都「没有控制台」，但**加载的 DLL 集合、子系统、CRT 初始化路径都不同** ——
       而卡点恰恰就在 DLL 卸载上。替身必须先在**被测的那个量**上被证明等价。
    2. **噪声淹没了信号**：脚本自己都打出了「正常退出耗时 平均 6.49s、最快 0.64s」，
       说明「建 Tk 窗口」这一步本身就不稳定，和「退出卡住」混在一起测，分不开。

  教训：**用替身做 A/B 之前，先证明替身和真身在关键维度上等价。**
        拿一个噪声比信号大的实验去「证伪」一个假设，得到的只是噪声。

  本文件保留下来只作排查记录，**不要拿它当判据**。要判据请用
  `exit_regression_test.py`（真实 exe、不替用户杀进程）。
═══════════════════════════════════════════════════════════════════════════════

假设（当时要证伪的就是它）：
    `os._exit()` 在 Windows 上最终走 `_exit()` → `ExitProcess()`，而
    `ExitProcess()` **会依次执行所有已加载 DLL 的 `DLL_PROCESS_DETACH`**。
    Tcl/Tk 的卸载回调会死锁，于是进程卡住不退（CPU 为 0、界面已经没了）。
    `TerminateProcess(GetCurrentProcess(), code)` **不执行 DLL 卸载回调**，
    所以应该每次都干净退出。

    ↑ 这个假设**其实是对的**，只是本实验测不出来。

为什么当时想做这个实验：
    光看现象就改代码，等于赌。想做的是把「是不是 os._exit 的锅」单独隔出来。
    出发点没错，错在**替身选错了**（见上面的 ⚠️）。

用法：
    python tk_exit_race_test.py [轮数]     # 仅供追溯，别用它下结论
"""
import ctypes
import os
import subprocess
import sys
import time

DEVNULL = subprocess.DEVNULL
# 用 pythonw（无控制台）—— 和 --windowed 打包后的子进程一致
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.isfile(PYW):
    PYW = sys.executable

PER_ROUND_TIMEOUT = 12.0


def child(mode):
    """子进程：建一个 Tk 窗口 → 销毁 → 用指定方式退出。"""
    import tkinter as tk
    r = tk.Tk()
    r.title("exit race")
    r.geometry("200x120+50+50")
    r.update()            # 真的把窗口建出来、把 Tcl/Tk 都加载起来
    r.destroy()           # 等价于用户点 X（WM_CLOSE → destroy）
    if mode == "os_exit":
        os._exit(0)
    elif mode == "terminate":
        k = ctypes.WinDLL("kernel32")
        k.TerminateProcess(k.GetCurrentProcess(), 0)
        # 万一 TerminateProcess 没生效，兜一下，免得实验挂住
        os._exit(0)
    else:
        raise SystemExit("unknown mode %r" % mode)


def run_mode(mode, rounds):
    hangs = 0
    times = []
    for i in range(rounds):
        t0 = time.time()
        p = subprocess.Popen([PYW, os.path.abspath(__file__), "--child", mode],
                             stdout=DEVNULL, stderr=DEVNULL)
        try:
            p.wait(timeout=PER_ROUND_TIMEOUT)
            times.append(time.time() - t0)
        except subprocess.TimeoutExpired:
            hangs += 1
            p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
    return hangs, times


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
        return 0

    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    print("子进程解释器: %s" % PYW)
    print("每轮最多等 %.0fs，超时即判「卡住」" % PER_ROUND_TIMEOUT)
    print("=" * 58)

    results = {}
    for mode in ("os_exit", "terminate"):
        hangs, times = run_mode(mode, rounds)
        results[mode] = (hangs, times)
        avg = (sum(times) / len(times)) if times else 0
        print("%-12s 卡住 %2d/%d 轮   正常退出耗时 平均 %.2fs  最快 %.2fs"
              % (mode, hangs, rounds, avg, min(times) if times else 0))

    print("=" * 58)
    h_os, _ = results["os_exit"]
    h_tm, _ = results["terminate"]
    if h_os > 0 and h_tm == 0:
        print("结论: **判断成立** —— os._exit 会卡（%d/%d），"
              "TerminateProcess 一次不卡（0/%d）。" % (h_os, rounds, rounds))
        print("      => 修 exit_now()：改用 TerminateProcess。")
        return 1
    if h_os == 0 and h_tm == 0:
        print("结论: **判断被证伪** —— os._exit 一次都没卡。")
        print("      => 卡住的原因不在 os._exit，得换方向找（回到引导器/父进程那条线）。")
        return 2
    print("结论: 两种方式都卡或都不稳定（os._exit %d/%d，TerminateProcess %d/%d）——"
          % (h_os, rounds, h_tm, rounds))
    print("      说明卡点不在退出方式上，得另找。")
    return 3


if __name__ == "__main__":
    sys.exit(main())
