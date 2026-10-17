# -*- coding: utf-8 -*-
"""把 PNG 放大 N 倍（最近邻），纯 tkinter 实现 —— 本机没装 PIL。

    python png_zoom.py <in.png> <out.png> [倍数]

用途：验证码太小、看不清时先放大再看。
⚠️ 建过 Tk 窗口的脚本收尾必须 exit_hard()（红线 26）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                # repo 根（proc_tree 在那儿）

import tkinter as tk                                     # noqa: E402
import proc_tree                                         # noqa: E402


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    src, dst = argv[0], argv[1]
    n = int(argv[2]) if len(argv) > 2 else 8
    root = tk.Tk()
    root.withdraw()
    try:
        img = tk.PhotoImage(file=src)
        print("原图 %dx%d，放大 %d 倍 → %dx%d"
              % (img.width(), img.height(), n, img.width() * n, img.height() * n))
        big = img.zoom(n, n)
        big.write(dst, format="png")
        print("已写出：%s" % dst)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        proc_tree.exit_hard(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
