# -*- coding: utf-8 -*-
"""反复启动打包后的 exe，确认 headless 路径每次都能干净退出。

背景（两件事，别搞混）：

1. 曾经出现 --guitest 结果已落盘、进程却不退出，卡 10 分钟以上。
   后来用「改动之前的旧二进制也照样卡」这个对照实验证明：
   在受控会话里**创建 Tk 窗口本身就不稳定**，不是代码问题。
   所以这里只压 headless 参数（--selftest / --auto），不测 --guitest。

2. 真正会让这个脚本自己挂死的是 subprocess 的管道陷阱：
   onefile 的 bootloader 父进程会再 fork 一个子进程，子进程继承 stdout/stderr
   的管道写端；父进程被 kill 后子进程还活着，写端就一直不关。
   而 subprocess.run 在超时分支里会调 communicate() 去收尾 —— 于是**永远等不到
   EOF**，设了 timeout 也照样挂死。
   => 一律用 DEVNULL，没有管道可等，进程一退就返回。

3. 还有一个同类问题：subprocess.run(timeout=...) 超时后**只 kill 启动器父进程**，
   子进程会变成孤儿。这里改用 proc_tree.run_exe，超时会把整棵树收掉。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proc_tree  # noqa: E402
import paths

EXE = paths.default_exe()
FLAGS = ["--selftest", "--auto"]
ROUNDS = 5

bad = 0
total = 0
for flag in FLAGS:
    for i in range(1, ROUNDS + 1):
        total += 1
        t = time.time()
        rc, timed_out = proc_tree.run_exe(EXE, [flag], timeout=60)
        dt = time.time() - t
        if timed_out:
            bad += 1
            print("%-11s 第 %d 次: **超时 60s 未退出**" % (flag, i))
        else:
            ok = rc == 0
            if not ok:
                bad += 1
            print("%-11s 第 %d 次: %-12s %.1fs"
                  % (flag, i, "OK" if ok else "退出码 %s" % rc, dt))
        sys.stdout.flush()

print()
print("共 %d 次，结论: %s" % (total, "全部干净退出" if bad == 0 else "%d 次异常" % bad))
sys.exit(1 if bad else 0)
