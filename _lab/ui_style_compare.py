# -*- coding: utf-8 -*-
"""把两张界面截图并排拼成一张「改前 / 改后」对比图。

为什么需要：
    单看改后的截图，很难说清「到底变了什么」—— 人的记忆不可靠，
    而本项目里每次界面改版都值得留一张能一眼看懂的对比图。

⚠️ 两张图必须**同宽**（都是同一个界面截出来的），否则拼出来的对比是错的
   —— 宽度不同意味着某个东西整体缩放过了，那对比的就不是「改了什么」。

⚠️ 不用 PIL（本环境没装）。用的是 `png_zoom.py` 里那套纯标准库的
   PNG 读写（zlib + struct），和 `ui_shot.py` / `shot.py` 同一个来源。

用法：
    python _lab/ui_style_compare.py 改前.png 改后.png 输出.png
输出：
    并排图（左边改前、右边改后），两张之间留一条白缝 + 一道灰分隔线。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import png_zoom as Z          # noqa: E402

GAP = 16                      # 两张之间的白缝宽度（像素）
LINE_W = 2                    # 白缝正中间那道分隔线的宽度
WHITE = (255, 255, 255)
DIVIDER = (190, 190, 190)


def load(path):
    w, h, nch, px = Z.read_png(path)
    return w, h, Z.to_rgb(w, h, nch, px)


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    before, after, out = argv[1], argv[2], argv[3]
    bw, bh, bp = load(before)
    aw, ah, ap = load(after)
    if bw != aw:
        print("!! 两张图宽度不同（%d vs %d）—— 没法并排比。"
              "宽度不同说明有一张整体缩放过，那样比出来的结论是错的。" % (bw, aw))
        return 1

    W = bw * 2 + GAP
    H = max(bh, ah)
    canvas = bytearray(W * H * 3)
    # 先整块刷白（两张高度不同时，短的那张底下就是白的）
    for i in range(0, len(canvas), 3):
        canvas[i:i + 3] = bytes(WHITE)

    def blit(src, sh, x0):
        for y in range(sh):
            row = y * bw * 3
            dst = (y * W + x0) * 3
            canvas[dst:dst + bw * 3] = src[row:row + bw * 3]

    blit(bp, bh, 0)
    blit(ap, ah, bw + GAP)

    # 分隔线：画在缝的正中间，从上到下贯满整幅高度
    lx = bw + (GAP - LINE_W) // 2
    for y in range(H):
        dst = (y * W + lx) * 3
        for k in range(LINE_W):
            canvas[dst + k * 3:dst + k * 3 + 3] = bytes(DIVIDER)

    Z.write_png(out, W, H, bytes(canvas))
    print("已写出 %s  (%dx%d)  左=改前 %dx%d，右=改后 %dx%d"
          % (out, W, H, bw, bh, aw, ah))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
