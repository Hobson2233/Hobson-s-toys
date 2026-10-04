# -*- coding: utf-8 -*-
"""验证「圆角按钮配色自检」这条门槛真的会拦人，而且不依赖 Tk。

背景：
    界面美化时把「使用说明」按钮的静止底色写成了 `#eef1f6` —— 那正是页面底色
    BG 本身。按钮于是和背景完全融成一片，截图里只剩四个灰字，用户根本不会去点。
    这类错**不抛任何异常**：逻辑自检全绿、窗口照常显示、`--guitest` 也是 GUI_OK，
    只有把截图放大若干倍才看得出来。

    `ui_contrast_check()` 就是为了把这种错变成一次响亮的构建失败。但它必须
    自己先被证明「会拦人」—— 否则它就是个永远通过的空转，而
    **「永远通过」和「通过」在输出上一模一样**。

为什么不建真窗口：
    同 wiring_gate_test.py 的理由 —— 受控会话里建 Tk 时好时坏
    （同一份代码实测 0.9s / 14s / 直接挂住）。而「什么算同色」这个判定
    才是门槛的核心，必须可靠，所以用假控件（duck typing）复刻那棵控件树。
    真界面里那一次由 `--guitest` 跑（会往日志写「圆角按钮配色自检」）。

用法：
    python ui_contrast_test.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C

FAILS = []


class Fake(object):
    """够用的假控件：ui_contrast_check 只用到 winfo_children() 和 _rb_* 属性。

    圆角按钮就是叶子（没有子控件）；容器用来验证「深层也能找到」。
    """

    def __init__(self, *children, **attrs):
        self._children = list(children)
        for k, v in attrs.items():
            setattr(self, k, v)

    def winfo_children(self):
        return self._children


def btn(text, bg, parent_bg, disabled_bg="#dfe5ee"):
    """一个假圆角按钮。属性名与 round_button 里挂的完全一致。"""
    return Fake(_rb_text=text, _rb_bg=bg, _rb_parent_bg=parent_bg,
                _rb_disabled_bg=disabled_bg)


def verdict(root):
    """跑一次自检，返回 (是否拦下, 消息)。

    「拦下」的判据是**抛了 AssertionError** —— 只有 AssertionError 才会让
    --guitest 落成 GUI_FAIL。别的异常类型不算数（可能只是代码写错了）。
    """
    try:
        return False, C.ui_contrast_check(root)
    except AssertionError as e:
        return True, str(e)


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-46s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def main():
    print("=== 1. 色差算法本身 ===")
    check("同一个颜色差 0", C._color_delta("#eef1f6", "#eef1f6"), 0)
    check("#ffffff 与 #000000 差 255", C._color_delta("#ffffff", "#000000"), 255)
    check("大小写不影响", C._color_delta("#EEF1F6", "#eef1f6"), 0)
    check("取的是最大单通道差（不是平均）",
          C._color_delta("#ff0000", "#fe0101"), 1)
    check("解析不了的返回 None", C._color_delta("white", "#ffffff"), None)
    check("不是 7 位的返回 None", C._color_delta("#fff", "#ffffff"), None)

    print()
    print("=== 2. 正常界面不会被误伤 ===")
    root = Fake(
        btn("使用说明", "#dbe1eb", C.BG),
        btn("切换到此账号", C.BLUE, C.CARD),
        btn("仅保存", "#f1f5f9", C.CARD),
        btn("退出当前账号", C.DANGER_SOFT, C.CARD),
        btn("选用", "#f1f5f9", C.CARD),
        btn("删除", "#fef6f6", C.CARD),
    )
    hit, msg = verdict(root)
    check("真实配色的一整屏按钮 -> 放行", hit, False)
    check("结论里报了按钮个数", "6 个按钮" in msg, True)

    print()
    print("=== 3. 负向对照：把按钮弄隐形，必须每一项都被拦下 ===")
    # ① 就是本次实际踩到的那个 bug
    hit, msg = verdict(Fake(btn("使用说明", C.BG, C.BG)))
    check("静止态与背景**完全相同** -> 拦下", hit, True)
    check("  且消息里点名是哪个按钮", "使用说明" in msg, True)
    check("  且消息里带上两个颜色", C.BG in msg, True)
    # ② 只差 3 级：肉眼同样分不出，`==` 判不出来，必须靠阈值拦
    hit, _ = verdict(Fake(btn("近色按钮", "#ebeff4", C.BG)))
    check("静止态只差 3 级 -> 拦下（阈值在起作用）", hit, True)
    # ③ 差 7 级：正好在阈值之下
    hit, _ = verdict(Fake(btn("差 7 级", "#e7ebf1", C.BG)))
    check("静止态差 7 级（阈值 8 之下）-> 拦下", hit, True)
    # ④ 边界：正好 8 级，不该拦（否则阈值就成了摆设，早晚误伤）
    hit, _ = verdict(Fake(btn("差 8 级", "#e6eaf0", C.BG)))
    check("静止态正好差 8 级 -> 放行（边界不误伤）", hit, False)
    # ⑤ 禁用态隐形同样算缺陷 —— 「禁用」不该等于「消失」
    hit, msg = verdict(Fake(btn("禁用隐形", C.BLUE, C.BG,
                               disabled_bg=C.BG)))
    check("禁用态与背景同色 -> 拦下", hit, True)
    check("  且消息里点明是禁用态", "禁用态" in msg, True)

    print()
    print("=== 4. 空转必须报错，不能算通过 ===")
    hit, msg = verdict(Fake(Fake(Fake())))
    check("一个按钮都找不到 -> 拦下（不是放行）", hit, True)
    check("  且说明它正在空转", "空转" in msg, True)

    print()
    print("=== 5. 深层嵌套的按钮也要找得到 ===")
    deep = Fake(Fake(Fake(Fake(btn("埋在第三层", C.BG, C.BG)))))
    hit, msg = verdict(deep)
    check("第三层的隐形按钮 -> 拦下", hit, True)
    check("  且点名正确", "埋在第三层" in msg, True)

    print()
    print("=== 6. 禁用态的自动兜底（_pick_disabled_bg）===")
    check("撞上 BG -> 换成兜底灰",
          C._pick_disabled_bg("#eef1f6", C.BG), C._DISABLED_FALLBACK)
    check("撞上 CARD -> 换成兜底灰",
          C._pick_disabled_bg("#ffffff", C.CARD), C._DISABLED_FALLBACK)
    check("本身就能用 -> 原样不动",
          C._pick_disabled_bg("#dfe5ee", C.BG), "#dfe5ee")
    check("兜底灰落在 BG 上确实分得开",
          C._color_delta(C._DISABLED_FALLBACK, C.BG) >= C._CONTRAST_MIN, True)
    check("兜底灰落在 CARD 上确实分得开",
          C._color_delta(C._DISABLED_FALLBACK, C.CARD) >= C._CONTRAST_MIN, True)
    check("两个默认色**确实**是同一个值（否则第 1 项测的是空气）",
          C.BG, "#eef1f6")

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 这条门槛确实会拦人，而且不依赖 Tk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
