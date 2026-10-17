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


def shift(color, n):
    """把 `#rrggbb` 的每个通道各减 n。用来构造「只差 n 级」的近色。

    🔴 为什么**算出来**而不是写死字面量（2026-10-17 换 Fluent 时改的）：
        原来这一节写的是 `#ebeff4` / `#e7ebf1` / `#e6eaf0` 三个字面量，
        它们是「当时那个 BG」减 3 / 7 / 8 —— 换配色时它们**不再和 BG 相关**，
        于是「差 3 级必须拦下」这条断言会**静默变成测别的东西**：
        新 BG `#f3f3f3` 减那些老值，差的是 8 / 12 / 13，前两条直接放行，
        报出来是「门槛失效」，真实原因却是「测试数据过期了」。
        算出来之后，无论 BG 换成什么，这一节量的都还是**阈值本身**。
    """
    s = color.lstrip("#")
    return "#" + "".join("%02x" % max(0, int(s[i:i + 2], 16) - n) for i in (0, 2, 4))


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
        btn("使用说明", C.BTN_SOFT_ON_BG, C.BG),
        btn("切换到此账号", C.BLUE, C.CARD),
        btn("仅保存", C.BTN_SOFT, C.CARD),
        btn("退出当前账号", C.DANGER_SOFT, C.CARD),
        btn("选用", C.BTN_SOFT, C.CARD),
        btn("删除", C.DANGER_SOFT, C.CARD),
        # 账号行上「这个账号属于别的学校」那个警示片（2026-10-12 加）。
        # 它的底色是**最浅的一档**（WARN_SOFT 落在白卡上），
        # 所以必须放进这棵树里 —— 它是最可能踩到阈值的一个。
        btn("郑州航空工业管理学院", C.WARN_SOFT, C.CARD),
    )
    hit, msg = verdict(root)
    check("真实配色的一整屏按钮 -> 放行", hit, False)
    check("结论里报了按钮个数", "7 个按钮" in msg, True)

    print()
    print("=== 3. 负向对照：把按钮弄隐形，必须每一项都被拦下 ===")
    # ① 就是本次实际踩到的那个 bug
    hit, msg = verdict(Fake(btn("使用说明", C.BG, C.BG)))
    check("静止态与背景**完全相同** -> 拦下", hit, True)
    check("  且消息里点名是哪个按钮", "使用说明" in msg, True)
    check("  且消息里带上两个颜色", C.BG in msg, True)
    # ② 只差 3 级：肉眼同样分不出，`==` 判不出来，必须靠阈值拦
    hit, _ = verdict(Fake(btn("近色按钮", shift(C.BG, 3), C.BG)))
    check("静止态只差 3 级 -> 拦下（阈值在起作用）", hit, True)
    # ③ 差 7 级：正好在阈值之下
    hit, _ = verdict(Fake(btn("差 7 级", shift(C.BG, 7), C.BG)))
    check("静止态差 7 级（阈值 8 之下）-> 拦下", hit, True)
    # ④ 边界：正好 8 级，不该拦（否则阈值就成了摆设，早晚误伤）
    hit, _ = verdict(Fake(btn("差 8 级", shift(C.BG, 8), C.BG)))
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
          C._pick_disabled_bg(C.BG, C.BG), C._DISABLED_FALLBACK)
    check("撞上 CARD -> 换成兜底灰",
          C._pick_disabled_bg(C.CARD, C.CARD), C._DISABLED_FALLBACK)
    check("本身就能用 -> 原样不动",
          C._pick_disabled_bg("#dfe5ee", C.BG), "#dfe5ee")
    check("兜底灰落在 BG 上确实分得开",
          C._color_delta(C._DISABLED_FALLBACK, C.BG) >= C._CONTRAST_MIN, True)
    check("兜底灰落在 CARD 上确实分得开",
          C._color_delta(C._DISABLED_FALLBACK, C.CARD) >= C._CONTRAST_MIN, True)
    # 🔴 2026-10-17：这条原来写的是 `check(..., C.BG, "#eef1f6")` —— 把 BG 的
    #    字面量钉在测试里。换 Fluent 时 BG 变成 #f3f3f3，这条**立刻失败**，
    #    而它想守的其实不是「BG 等于某个数」，是「`round_button` 的
    #    `disabled_bg` 默认值 == BG」（不然上面第 1 项测的是空气）。
    #    所以改成直接读签名 —— 这样它测的是**那个不变量本身**，
    #    BG 换成什么都不会误报，而真把默认值写死成别的颜色时照样会响。
    import inspect
    _dflt = inspect.signature(C.round_button).parameters["disabled_bg"].default
    check("round_button 的 disabled_bg 默认值 == BG（否则第 1 项测的是空气）",
          _dflt, C.BG)

    print()
    print("=== 7. 新加的语义浅底必须过阈值（不然「看得见」只是嘴上说说）===")
    # 🔴 这一节存在的理由：`round_chip` 也挂 `_rb_bg`，所以每一块圆角小标签
    #    都会被 ui_contrast_check 查一遍。新加一个浅底颜色时，凭肉眼挑一个
    #    「看着挺明显」的值是不够的 —— 差 4~5 级肉眼就分不出了。
    for _name, _soft, _page in (("WARN_SOFT 在白卡上", C.WARN_SOFT, C.CARD),
                                 ("WARN_SOFT 在页面底上", C.WARN_SOFT, C.BG),
                                 ("BLUE_SOFT 在白卡上", C.BLUE_SOFT, C.CARD),
                                 ("OK_SOFT 在白卡上", C.OK_SOFT, C.CARD),
                                 ("DANGER_SOFT 在白卡上", C.DANGER_SOFT, C.CARD),
                                 # 2026-10-17 加：导航项也是「按钮」，只是它
                                 # **故意不挂 `_rb_bg`**（未选中态本来就该透明，
                                 # 挂了会被判成隐形而误报），所以 `ui_contrast_check`
                                 # 管不到它 —— 那两档颜色就由这里兜住。
                                 ("NAV_HOVER 在导航窗格上", C.NAV_HOVER, C.NAV_BG),
                                 ("NAV_ACTIVE 在导航窗格上", C.NAV_ACTIVE, C.NAV_BG),
                                 ("次要按钮 BTN_SOFT 在白卡上", C.BTN_SOFT, C.CARD),
                                 ("次要按钮 BTN_SOFT_ON_BG 在页面底上",
                                  C.BTN_SOFT_ON_BG, C.BG)):
        _d = C._color_delta(_soft, _page)
        check("%-18s 色差 %d >= %d" % (_name, _d, C._CONTRAST_MIN),
              _d >= C._CONTRAST_MIN, True)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 这条门槛确实会拦人，而且不依赖 Tk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
