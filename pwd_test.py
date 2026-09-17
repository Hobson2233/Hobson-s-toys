# -*- coding: utf-8 -*-
"""密码框「显示规则」和「按键处理」的测试。

这两段逻辑被特意抽成了不碰 Tk 的纯函数，所以能在受控会话里可靠地跑 ——
而它们恰恰是最容易写错的地方：星号多一个少一个、Ctrl+C 把密码带走、
粘贴带进换行、退格在选中状态下该清空还是退一格……

用法：
    python pwd_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C

FAILS = []
PW = "abc123"          # 样例密码：长度 6、末位是 3，便于肉眼核对星号个数


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-40s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def main():
    print("=== 1. 显示规则 pwd_display_text ===")
    check("空密码", C.pwd_display_text(""), "")
    check("空密码（有焦点）", C.pwd_display_text("", focused=True), "")
    check("没在输入 -> 全部打码", C.pwd_display_text(PW), "******")
    check("正在输入 -> 只露最后一个", C.pwd_display_text(PW, focused=True), "*****3")
    check("点了显示 -> 明文", C.pwd_display_text(PW, reveal=True), PW)
    check("点了显示且没焦点 -> 仍明文",
          C.pwd_display_text(PW, reveal=True, focused=False), PW)
    check("单个字符 + 有焦点 -> 就是它自己",
          C.pwd_display_text("a", focused=True), "a")
    check("空密码 + 点了显示", C.pwd_display_text("", reveal=True), "")

    print()
    print("=== 2. 长度不变式（星号数错了光标就会跑偏）===")
    bad = []
    for n in range(1, 9):
        s = "x" * n
        if len(C.pwd_display_text(s)) != n:
            bad.append(("全打码", n))
        if len(C.pwd_display_text(s, focused=True)) != n:
            bad.append(("露尾", n))
        if C.pwd_display_text(s, focused=True).count("*") != n - 1:
            bad.append(("星号数", n))
    check("长度 1..8 的显示串长度与星号数都正确", bad, [])

    print()
    print("=== 3. 按键处理 pwd_apply_key ===")
    check("输入字符", C.pwd_apply_key("", "a", "a"), ("a", True))
    check("继续输入", C.pwd_apply_key("ab", "c", "c"), ("abc", True))
    check("中文也能进", C.pwd_apply_key("", "??", "密"), ("密", True))
    check("退格", C.pwd_apply_key("abc", "BackSpace", "\x08"), ("ab", True))
    check("退格（空密码不报错）", C.pwd_apply_key("", "BackSpace", "\x08"), ("", True))
    check("退格（有选中 -> 清空）",
          C.pwd_apply_key("abc", "BackSpace", "\x08", selected=True), ("", True))
    check("Delete 无效", C.pwd_apply_key("abc", "Delete", ""), ("abc", True))
    check("方向键放行", C.pwd_apply_key("abc", "Left", ""), ("abc", False))
    check("Tab 放行", C.pwd_apply_key("abc", "Tab", "\t"), ("abc", False))
    check("回车放行（不会进密码）", C.pwd_apply_key("abc", "Return", "\r"), ("abc", False))

    print()
    print("=== 4. 剪贴板 ===")
    check("Ctrl+V 追加", C.pwd_apply_key("ab", "v", "\x16", ctrl=True, clip="cd"),
          ("abcd", True))
    check("Ctrl+V 去掉换行",
          C.pwd_apply_key("", "v", "\x16", ctrl=True, clip="ab\r\ncd"), ("abcd", True))
    check("Ctrl+V 空剪贴板", C.pwd_apply_key("ab", "v", "\x16", ctrl=True, clip=None),
          ("ab", True))
    check("Ctrl+C 被拦下且不动内容（别把密码带走）",
          C.pwd_apply_key("abc", "c", "\x03", ctrl=True), ("abc", True))
    check("Ctrl+X 被拦下", C.pwd_apply_key("abc", "x", "\x18", ctrl=True), ("abc", True))
    check("Ctrl+A 放行（允许全选）",
          C.pwd_apply_key("abc", "a", "\x01", ctrl=True), ("abc", False))

    print()
    print("=== 5. Ctrl 与 AltGr 的区分（欧洲键盘打 @ 这类字符）===")
    # Windows 上 AltGr（右 Alt）在 Tk 里报告成 Ctrl+Alt。只看 Ctrl 位会把用户
    # 正常输入的字符当快捷键吃掉，那些字符就永远打不进密码框。
    # 真快捷键产生的一定是不可打印字符，所以用「char 可打印与否」来区分。
    check("AltGr+Q 打出 @ -> 当作正常输入",
          C.pwd_apply_key("ab", "q", "@", ctrl=True), ("ab@", True))
    check("AltGr+E 打出 € -> 当作正常输入",
          C.pwd_apply_key("", "e", "€", ctrl=True), ("€", True))
    check("真 Ctrl+V（不可打印）仍走粘贴",
          C.pwd_apply_key("ab", "v", "\x16", ctrl=True, clip="cd"), ("abcd", True))
    check("真 Ctrl+C（不可打印）仍被拦",
          C.pwd_apply_key("ab", "c", "\x03", ctrl=True), ("ab", True))
    check("Ctrl 配方向键不产生字符 -> 仍被拦",
          C.pwd_apply_key("ab", "Left", "", ctrl=True), ("ab", True))

    print()
    print("=== 6. 连着敲一串密码，看每一步显示 ===")
    real, shown = "", []
    for ch in PW:
        real, _ = C.pwd_apply_key(real, ch, ch)
        shown.append(C.pwd_display_text(real, focused=True))
    check("逐字输入的显示序列", shown,
          ["a", "*b", "**c", "***1", "****2", "*****3"])
    check("输完的真实密码", real, PW)
    check("输完失焦 -> 全部打码", C.pwd_display_text(real, focused=False), "******")

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
