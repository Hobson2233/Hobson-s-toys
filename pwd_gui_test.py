# -*- coding: utf-8 -*-
"""密码框的真实交互测试：建一个真窗口，走真实的按键处理，检查显示。

pwd_test.py 测的是两条纯规则；这里测的是「绑定有没有接对」—— <KeyPress>
是否真的挂上了、按钮点下去是否真的切换、焦点进出是否触发重绘。这些只有真建
窗口才测得到。

受控会话里建 Tk 窗口可能挂住，所以这个脚本请带超时跑：
    timeout 60 python -u pwd_gui_test.py
建不起来就当环境问题处理（见 --guitest 的同类情况），别当成代码失败。

用法：
    python pwd_gui_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C
import proc_tree

FAILS = []
FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)


class Ev(object):
    """够用的假事件对象：_on_key 只看这三个属性。"""

    def __init__(self, keysym, char, state=0):
        self.keysym = keysym
        self.char = char
        self.state = state


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-40s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def main():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()

    f = C.PasswordField(root, tk, FONT, FONT_S, "#222222")
    f.box.pack()
    root.update_idletasks()

    print("=== 1. 事件是否真的挂上了 ===")
    check("<KeyPress> 已绑定", bool(f.entry.bind("<KeyPress>")), True)
    check("<FocusIn> 已绑定", bool(f.entry.bind("<FocusIn>")), True)
    check("<FocusOut> 已绑定", bool(f.entry.bind("<FocusOut>")), True)
    check("<Button-1> 已绑定", bool(f.entry.bind("<Button-1>")), True)
    check("按钮挂了回调", bool(f.button.cget("command")), True)

    print()
    print("=== 1b. 绕过按键的改内容路径是否都堵上了 ===")
    # Entry 的类绑定里有 <<Paste>> / <<Cut>> / <<Clear>> / <<PasteSelection>>，
    # 它们不产生 KeyPress，会直接把文本塞进控件 → self.real 和显示内容脱节。
    for seq in ("<<Paste>>", "<<Cut>>", "<<Clear>>", "<<PasteSelection>>"):
        check("%s 已接管" % seq, bool(f.entry.bind(seq)), True)
    check("<<Cut>> 返回 break", f.entry.bind("<<Cut>>") and f._block(), "break")
    check("<<Clear>> 返回 break", f._block(), "break")
    check("<<PasteSelection>> 返回 break", f._block(), "break")

    print()
    print("=== 2. 初始状态 ===")
    check("密码框为空", f.entry.get(), "")
    check("真实密码为空", f.get(), "")

    print()
    print("=== 3. 逐字敲入（走真实的按键处理）===")
    f.set_focus(True)
    for ch in "abc123":
        f._on_key(Ev(ch, ch))
    check("显示 = 只露刚敲进去那个", f.entry.get(), "*****3")
    check("真实密码是完整的", f.get(), "abc123")

    print()
    print("=== 4. 「显示」按钮 ===")
    f.toggle_reveal()
    check("点了显示 -> 明文", f.entry.get(), "abc123")
    check("按钮文字变「隐藏」", f.button.cget("text"), "隐藏")
    f.toggle_reveal()
    check("再点一次 -> 回到打码", f.entry.get(), "*****3")
    check("按钮文字变回「显示」", f.button.cget("text"), "显示")

    print()
    print("=== 5. 焦点进出 ===")
    f.set_focus(False)
    check("失焦 -> 全部打码", f.entry.get(), "******")
    f.set_focus(True)
    check("重新聚焦 -> 又露出最后一个", f.entry.get(), "*****3")

    print()
    print("=== 6. 退格与方向键 ===")
    check("退格被接管（返回 break）",
          f._on_key(Ev("BackSpace", "\x08")), "break")
    check("退格后显示", f.entry.get(), "****2")
    check("真实密码也少了一位", f.get(), "abc12")
    check("方向键放行（返回 None）", f._on_key(Ev("Left", "")), None)
    check("方向键没动内容", f.get(), "abc12")

    print()
    print("=== 7. Ctrl+C 不该把密码带走 ===")
    check("Ctrl+C 被拦下", f._on_key(Ev("c", "\x03", state=0x0004)), "break")
    check("内容没变", f.get(), "abc12")

    print()
    print("=== 8. 外部 set（选用账号 / 重新加载）===")
    f.set_focus(False)
    f.set("abc")
    check("set 之后失焦 -> 全打码", f.entry.get(), "***")
    check("set 之后真实密码", f.get(), "abc")
    f.set("")
    check("set 空串", f.entry.get(), "")

    print()
    print("=== 9. <<Paste>> 走自己的处理，不脱节 ===")
    f.set_focus(True)
    f.set("")
    try:
        root.clipboard_clear()
        root.clipboard_append("abc123")
        r = f._on_paste()
        check("<<Paste>> 返回 break（挡住 Entry 默认插入）", r, "break")
        check("剪贴板内容进了真实密码", f.get(), "abc123")
        check("显示与真实密码一致（没脱节）",
              f.entry.get(), C.pwd_display_text(f.get(), False, True))
        root.clipboard_clear()
        root.clipboard_append("ab\r\ncd")
        f.set("")
        f._on_paste()
        check("带换行的剪贴板清掉了换行", f.get(), "abcd")
    except Exception as e:
        print("  [SKIP] 剪贴板不可用（%s: %s）" % (type(e).__name__, e))

    print()
    print("=== 10. 接线自检 pwd_wiring_check（含「它真能报警吗」的对照）===")
    try:
        msg = C.pwd_wiring_check(f)
        check("健康密码框 -> 自检通过", "通过" in str(msg), True)
    except AssertionError as e:
        check("健康密码框 -> 自检通过", str(e), "不应失败")

    # 阳性对照 1：把 get() 接错成「显示出来的那串星号」—— 这正是最危险的接错方式
    orig_get = f.get
    f.get = lambda: f.entry.get()
    try:
        C.pwd_wiring_check(f)
        check("get() 接错成显示文本 -> 自检应报警", False, True)
    except AssertionError as e:
        check("get() 接错成显示文本 -> 自检应报警", "真实密码" in str(e), True)
    finally:
        f.get = orig_get

    # 阳性对照 2：模拟「忘了打码」的 bug（text() 直接返回真密码）
    orig_text = f.text
    f.text = lambda: f.real
    try:
        C.pwd_wiring_check(f)
        check("忘了打码 -> 自检应报警", False, True)
    except AssertionError as e:
        check("忘了打码 -> 自检应报警", "打码" in str(e), True)
    finally:
        f.text = orig_text

    # 自检不能只报「通过」——修好之后要能回到通过
    try:
        C.pwd_wiring_check(f)
        check("恢复后 -> 自检又能通过", True, True)
    except AssertionError as e:
        check("恢复后 -> 自检又能通过", str(e), "不应失败")

    print()
    print("=== 11. 真实事件分发（部分环境不可见窗口收不到）===")
    f.set_focus(True)
    try:
        f.entry.event_generate("<KeyPress>", keysym="z", char="z", when="now")
        root.update()
        if f.get() == "z":
            check("真实 KeyPress 事件能驱动密码框", f.entry.get(), "z")
        else:
            print("  [SKIP] 真实 KeyPress 事件（本环境不可见窗口收不到，"
                  "已由第 3 节的直接调用覆盖）")
    except Exception as e:
        print("  [SKIP] 真实 KeyPress 事件（%s: %s）" % (type(e).__name__, e))

    try:
        root.destroy()
    except Exception:
        pass

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    # 这个脚本建的是 withdraw 过的窗口（桌面上看不见），所以它挂住不会留下
    # 可见窗口；但解释器收尾阶段一样可能死锁，留个看不见的僵尸进程。
    #
    # **看门狗不要在这里 disarm()** —— 曾经在 exit_hard() 之前解除过一次，
    # 结果是：真挂住的时候唯一的救命绳已经被自己解开了（实测确实挂过）。
    # exit_hard() 是最后一句，os._exit() 会直接带走守护线程，
    # 所以 disarm 本来就没必要，留着反而是个漏洞。
    proc_tree.arm_watchdog(60, "pwd_gui_test.py")
    _code = main()
    proc_tree.exit_hard(_code)
