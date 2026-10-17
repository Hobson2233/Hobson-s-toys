# -*- coding: utf-8 -*-
"""把 PNG 打成字符画（按颜色分开），用来读小验证码。

    python png_ascii.py <in.png> [阈值]

⚠️ 建过 Tk 窗口的脚本收尾必须 exit_hard()（红线 26）。
⚠️ `exit_hard()` 走 os._exit —— **异常和缓冲都会被吞掉**。
   第一次写这个脚本就踩了两次：先是输出没了，再是异常没了（屏幕上只剩表头一行）。
   所以这里把活儿全包在 `_run()` 里，异常自己打出来，最后才 exit_hard。
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tkinter as tk                                     # noqa: E402
import proc_tree                                         # noqa: E402


def _rgb(img, x, y):
    """取一个像素的 (r, g, b)。

    ⚠️ `PhotoImage.get()` 返回的是**元组** `(r, g, b)`，不是字符串 ——
       按字符串写 `.split()` 会抛 AttributeError，而这个异常被 exit_hard 吞掉。
    """
    v = img.get(x, y)
    if isinstance(v, str):
        v = v.split()
    return int(v[0]), int(v[1]), int(v[2])


def _run(src, thr):
    root = tk.Tk()
    root.withdraw()
    try:
        img = tk.PhotoImage(file=src)
        w, h = img.width(), img.height()
        out = ["%s  %dx%d  阈值=%d" % (src, w, h, thr)]
        for y in range(h):
            row = []
            for x in range(w):
                r, g, b = _rgb(img, x, y)
                if r > thr and g > thr and b > thr:
                    row.append(".")
                elif abs(r - g) < 18 and abs(g - b) < 18:
                    row.append("#")          # 灰 / 黑
                elif r > g and r > b:
                    row.append("R")
                elif g >= r and g >= b:
                    row.append("G")
                elif b > r and b > g:
                    row.append("B")
                else:
                    row.append("?")
            out.append("%02d %s" % (y, "".join(row)))
        return "\n".join(out)
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    src = argv[0]
    thr = int(argv[1]) if len(argv) > 1 else 200
    try:
        print(_run(src, thr))
        sys.stdout.flush()
        code = 0
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        code = 1
    proc_tree.exit_hard(code)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
