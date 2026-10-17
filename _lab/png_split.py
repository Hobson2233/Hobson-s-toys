# -*- coding: utf-8 -*-
"""把验证码字符画**按字符切开**，一个字符一块，方便逐个认。

    python png_split.py <in.png> [阈值]

比整张一起看可靠得多：整张 130 列挤在一行里，肉眼数不清列号，
很容易把「第 3 个字符的左半边」当成「第 2 个字符的右半边」。

⚠️ 建过 Tk 窗口的脚本收尾必须 exit_hard()（红线 26）。
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tkinter as tk                                     # noqa: E402
import proc_tree                                         # noqa: E402


def _rgb(img, x, y):
    v = img.get(x, y)
    if isinstance(v, str):
        v = v.split()
    return int(v[0]), int(v[1]), int(v[2])


def _run(src, thr, top):
    root = tk.Tk()
    root.withdraw()
    try:
        img = tk.PhotoImage(file=src)
        w, h = img.width(), img.height()
        ink = [[False] * w for _ in range(h)]
        for y in range(top, h):
            for x in range(w):
                r, g, b = _rgb(img, x, y)
                # 白底不算；顶部那条灰蓝色横线（r≈g≈b 或整体偏暗蓝）跳过
                if r > thr and g > thr and b > thr:
                    continue
                ink[y][x] = True
        cols = [any(ink[y][x] for y in range(top, h)) for x in range(w)]
        groups, cur = [], None
        for x in range(w):
            if cols[x]:
                if cur is None:
                    cur = [x, x]
                else:
                    cur[1] = x
            else:
                if cur is not None:
                    groups.append(cur)
                    cur = None
        if cur is not None:
            groups.append(cur)
        # 把挨得很近的（间隔 < 4 列）合并 —— 一个字符的笔画之间可能有空隙
        merged = []
        for g in groups:
            if merged and g[0] - merged[-1][1] <= 4:
                merged[-1][1] = g[1]
            else:
                merged.append(list(g))
        out = ["%s  %dx%d  阈值=%d  跳过前 %d 行"
               % (src, w, h, thr, top),
               "切出 %d 块，列范围：%s"
               % (len(merged), [(a, b) for a, b in merged])]
        for i, (x0, x1) in enumerate(merged):
            out.append("")
            out.append("=== 第 %d 块  列 %d~%d  宽 %d ===" % (i + 1, x0, x1, x1 - x0 + 1))
            for y in range(top, h):
                line = "".join("#" if ink[y][x] else "." for x in range(x0, x1 + 1))
                if line.strip("."):
                    out.append("%02d %s" % (y, line))
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
    top = int(argv[2]) if len(argv) > 2 else 6
    try:
        print(_run(src, thr, top))
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
