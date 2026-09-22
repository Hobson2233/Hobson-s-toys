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

# 实测出来的「内容需要高度」。0.3.6 收紧了竖向间距，从 808 降到约 812 附近
# （holder 783 + 两侧外留白 28 = 811）。
# 注意这是**传给 window_geometry 的输入**，0.3.6 起 EXTRA_H 已归零 ——
# 所以最终窗口高度**就等于**这个值（在没有被屏幕上限收口时）。
CONTENT_H = 811
# 于是装得下的屏幕上，最终窗口高度应该是 CONTENT_H + EXTRA_H(0)
EXPECTED_H = CONTENT_H

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
    # 0.3.6 起有两个上限：比例 sh*0.85 和绝对 sh-80，取更严的。
    # 720 高时 0.85*720=612 < 720-80=640，所以是**比例**在生效（612）。
    # 断言改成「等于两者中更严的那个」，而不是钉死在 sh-80 上 ——
    # 否则以后调 SCREEN_H_RATIO 又要来改这一行，而它其实没测到什么。
    check("720 高的屏幕：具体值 = min(sh*0.85, sh-80)", h, min(int(720 * C.SCREEN_H_RATIO), 720 - C.SCREEN_MARGIN_H))
    # 反向确认：确实比旧行为更矮（旧版是 640，比例上限把它压到 612）
    check("720 高的屏幕：比旧的 sh-80(640) 更矮", h < 720 - C.SCREEN_MARGIN_H, True)

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
    print("=== 7. 16:10 屏幕：窗口不能占满屏高（0.3.6 的改进点）===")
    # 霍布森的本机是 2560x1600（16:10）。程序 DPI 不感知，150% 缩放下
    # Tk 只看到 1707x1067。旧版这里会算出 963px 窗口（占屏高 90%），
    # 上下几乎没余量。改成比例上限后应该明显收下来。
    w, h, x, y = C.window_geometry(1707, 1067, CONTENT_H)
    ratio = h / 1067.0
    print("     本机 2560x1600@150%% -> Tk 看得 1707x1067 -> 窗口高 %d，占屏高 %.1f%%"
          % (h, ratio * 100))
    check("占屏高 <= SCREEN_H_RATIO", ratio <= C.SCREEN_H_RATIO + 1e-9, True)
    check("占屏高 <= 85%（旧版是 90%）", ratio <= 0.85, True)
    # 上下必须留得出标题栏 + 任务栏
    check("上方留给标题栏 >= 20px", y >= 20, True)
    check("下方留白 >= 20px", 1067 - (y + h) >= 20, True)

    print()
    print("=== 8. EXTRA_H 归零后，窗口高正好等于内容需要的高度 ===")
    # 这是「那 40px 是不是白送的」的回归测试。Tk 的 geometry 高度就是客户区，
    # 再 +40 只会变成卡片底部空白 —— 实测客户区 851 里有 68px 富余全是这个。
    for hh in (600, 700, 811, 900):
        w, h, x, y = C.window_geometry(2000, 2000, hh)   # 屏幕足够大，不受上限影响
        if h != hh:
            FAILS.append("EXTRA_H 归零 %d" % hh)
            print("  [失败] 内容 %d -> 窗口高 %d（应当相等）" % (hh, h))
    check("大屏上窗口高 == 传入的 reqh（EXTRA_H 已归零）", C.EXTRA_H, 0)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 任何屏幕上窗口都不会超出屏幕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
