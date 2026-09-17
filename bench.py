# -*- coding: utf-8 -*-
"""给 exe 测启动耗时和内存峰值，用于优化前后对比。

用法：
    python bench.py [次数] [参数...]
    python bench.py 5 --selftest

说明：
    - 用 psutil 轮询被测进程的内存（psutil 只在测量端用，不会打进 exe）
    - 峰值取 WorkingSet（和任务管理器看到的一致）
    - 每次跑完等一会儿再跑下一次，避免相互影响
"""
import os
import sys
import time

import paths

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proc_tree  # noqa: E402

EXE = paths.default_exe()

# 单次采样的最长等待。超了就强制收掉整棵树，并把这次标成「数据不可用」。
MAX_WAIT = 60


def human(n):
    for u in ("B", "K", "M"):
        if n < 1024:
            return "%.1f%s" % (n, u)
        n /= 1024.0
    return "%.1fG" % n


def bench(flag, rounds):
    import psutil
    times, peaks = [], []
    for i in range(rounds):
        t0 = time.time()
        # proc_tree.start：DEVNULL（onefile 的管道陷阱会挂死，绝不用 capture_output）
        # + 放进 kill-on-close 的 job。**收尾必须收整棵树** —— 原来这里是
        # `p.wait(timeout=30)` 超时后 `p.kill()`，而 kill 只杀启动器父进程，
        # 会留下真正持有窗口的子进程当孤儿。
        p, job = proc_tree.start([EXE, flag])
        peak = 0
        proc = None
        # 采样循环必须有截止时间：原来是 `while p.poll() is None` 死等，
        # 被测程序一卡住整个 bench 就永远不返回（后面那句 p.wait(timeout=30)
        # 其实够不到 —— 循环没退出就轮不到它）。
        deadline = t0 + MAX_WAIT
        try:
            proc = psutil.Process(p.pid)
            while p.poll() is None and time.time() < deadline:
                try:
                    # onefile 会 fork 子进程，父子都要算
                    mem = proc.memory_info().rss
                    for ch in proc.children(recursive=True):
                        try:
                            mem += ch.memory_info().rss
                        except Exception:
                            pass
                    peak = max(peak, mem)
                except Exception:
                    pass
                time.sleep(0.01)
        except Exception:
            pass
        finished = p.poll() is not None
        # 正常结束或超时都走同一条收尾：关 job 句柄，整棵树一起没，并复查
        proc_tree.shutdown(p, job, verbose=True)
        dt = time.time() - t0
        if not finished:
            print("      **超过 %ds 还没结束 —— 已强制收掉整棵树，这次数据不可用**"
                  % MAX_WAIT)
        times.append(dt)
        peaks.append(peak)
        print("  第 %d 次: %.2fs  峰值内存 %s" % (i + 1, dt, human(peak)))
        sys.stdout.flush()
        time.sleep(0.5)

    times.sort()
    peaks.sort()
    med_t = times[len(times) // 2]
    med_m = peaks[len(peaks) // 2]
    print()
    print("  %s -> 中位耗时 %.2fs（最快 %.2f / 最慢 %.2f）  中位峰值内存 %s"
          % (flag, med_t, times[0], times[-1], human(med_m)))
    return med_t, med_m


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    flags = sys.argv[2:] or ["--selftest"]
    if not os.path.isfile(EXE):
        print("找不到 exe:", EXE)
        return 1
    print("exe: %s  %s  %s"
          % (EXE, human(os.path.getsize(EXE)),
             time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(EXE)))))
    print("次数: %d  参数: %s" % (rounds, flags))
    print()
    for f in flags:
        bench(f, rounds)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
