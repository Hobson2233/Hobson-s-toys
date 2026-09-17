# -*- coding: utf-8 -*-
"""只验证桌面上已有的 exe，不重新打包。

用法：
    python verify_exe.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 数据目录逻辑只留一份（在 build.py 里），否则这里会去旧位置找标记文件，
# 把「程序换了目录」误报成「构建失败」。
from build import _data_dir as data_dir  # noqa: E402
import proc_tree  # noqa: E402
import paths

EXE = paths.default_exe()


def main():
    if not os.path.isfile(EXE):
        print("找不到 exe:", EXE)
        return 1
    print("exe:", EXE, os.path.getsize(EXE), "字节",
          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(EXE))))

    data = data_dir()
    print("数据目录:", data, "存在" if os.path.isdir(data) else "不存在")
    started = time.time()
    bad = 0

    for flag, marker, want, must in (
            ("--selftest", "selftest.txt", "版本", True),
            ("--guitest", "guitest.txt", "GUI_OK", False)):
        t0 = time.time()
        # 别用 subprocess.run(timeout=...)：超时它只杀启动器父进程，
        # 子进程会带着窗口变成孤儿。run_exe 收整棵树。
        rc, timed_out = proc_tree.run_exe(EXE, [flag], timeout=60)
        if timed_out:
            print("%s -> **超时 60s 未退出**（%s）"
                  % (flag, "必须通过" if must else "环境相关"))
            if must:
                bad += 1
            continue
        elapsed = time.time() - t0
        f = os.path.join(data, marker)
        text, fresh = "", False
        if os.path.isfile(f):
            fresh = os.path.getmtime(f) >= started - 1
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        ok = fresh and (want in text)
        print("%s -> 退出码 %s / %.1fs / 标记 %s / %s"
              % (flag, rc, elapsed, "新" if fresh else "旧或缺失",
                 "OK" if ok else "失败"))
        if not ok:
            print("  标记内容:", text[:300].replace("\n", " | "))
            if must:
                bad += 1

    print()
    print("结论:", "通过" if bad == 0 else "%d 项失败" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
