# -*- coding: utf-8 -*-
"""窗口几何的测试：窗口绝不能比屏幕还高（否则底部控件够不到）。

为什么值得单独测：
    这段算错了在受控会话里很难复现 —— 要改屏幕分辨率才能看到。
    但它的后果是**功能性的**：1366x768、或 1080p 开 150% 缩放（虚拟化后
    只剩 720 高）这类屏幕上，窗口会比屏幕高，底部「开机自启」够不到，
    用户就**没法开启开机自启**。

用法：
    python geometry_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C

FAILS = []

# 实测出来的「内容需要高度」（本机 1707x1067 屏幕：holder 772 + 两侧留白 36）。
# 注意这是**传给 window_geometry 的输入**，不含 EXTRA_H —— 那个由函数自己加。
CONTENT_H = 808
# 于是在装得下的屏幕上，最终窗口高度应该是 CONTENT_H + EXTRA_H(40)
EXPECTED_H = 848

# 常见的屏幕尺寸。虚拟化后的坐标 —— DPI 缩放会把它变小，
# 所以 1080p@150% 实际只有 1280x720。
SCREENS = [
    ("本机 1707x1067", 1707, 1067),
    ("1080p 100%", 1920, 1080),
    ("1080p 125%", 1536, 864),
    ("1080p 150%", 1280, 720),
    ("1366x768 100%", 1366, 768),
    ("1280x800 100%", 1280, 800),
    ("1024x600 上网本", 1024, 600),
    ("2560x1440 100%", 2560, 1440),
    ("3840x2160 200%", 1920, 1080),
]


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-44s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def main():
    print("=== 1. 任何屏幕上，窗口都不能比屏幕还高/还宽 ===")
    bad = []
    for label, sw, sh in SCREENS:
        w, h, x, y = C.window_geometry(sw, sh, CONTENT_H)
        if h > sh:
            bad.append((label, "高", h, sh))
        if w > sw:
            bad.append((label, "宽", w, sw))
        print("     %-18s %4dx%-4d  ->  窗口 %4dx%-4d  位置 +%d+%d"
              % (label, sw, sh, w, h, x, y))
    check("全部装得下屏幕", bad, [])

    print()
    print("=== 2. 窗口必须完整落在屏幕内（含位置）===")
    out = []
    for label, sw, sh in SCREENS:
        w, h, x, y = C.window_geometry(sw, sh, CONTENT_H)
        if x < 0 or y < 0 or x + w > sw or y + h > sh:
            out.append((label, x, y, w, h))
    check("没有跑出屏幕的", out, [])

    print()
    print("=== 3. 装得下的时候，不该被压小（保持原来的观感）===")
    w, h, x, y = C.window_geometry(1707, 1067, CONTENT_H)
    check("本机高度 = 内容需要的高度 + 余量", h, EXPECTED_H)
    check("本机宽度 = 屏幕的 44%%（在 820..1180 之间）", w, 820)

    print()
    print("=== 4. 屏幕太矮时必须压到屏幕内，而不是硬撑 ===")
    w, h, x, y = C.window_geometry(1280, 720, CONTENT_H)
    check("720 高的屏幕：窗口高 <= 720", h <= 720, True)
    check("720 高的屏幕：具体值 = 720-80", h, 640)

    print()
    print("=== 5. 最小尺寸也要按屏幕收口 ===")
    for label, sw, sh in SCREENS:
        mw, mh = C.minsize_for(sw, sh)
        if mw > sw or mh > sh:
            FAILS.append("minsize " + label)
            print("  [失败] %-16s minsize %dx%d 超出屏幕 %dx%d"
                  % (label, mw, mh, sw, sh))
    check("各屏幕的 minsize 都在屏幕内", [f for f in FAILS if f.startswith("minsize")], [])
    check("本机 minsize 保持原来的 700x520", C.minsize_for(1707, 1067), (700, 520))

    print()
    print("=== 6. 极端小屏不能算出负数或 0 ===")
    bad = []
    for sw, sh in ((800, 600), (640, 480), (400, 300)):
        w, h, x, y = C.window_geometry(sw, sh, CONTENT_H)
        if w <= 0 or h <= 0 or x < 0 or y < 0:
            bad.append((sw, sh, w, h, x, y))
    check("小屏也算得出正的尺寸和位置", bad, [])

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 任何屏幕上窗口都不会超出屏幕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
