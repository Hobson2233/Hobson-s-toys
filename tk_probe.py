# -*- coding: utf-8 -*-
"""探测 tkinter Entry 到底绑了哪些事件 —— 用来确认有没有绕过 <KeyPress> 的入口。

为什么需要探：
    我们的密码框靠 <KeyPress> 拦下所有输入、自己维护真实密码。但 Tk 的 Entry
    还有别的改内容的途径（右键菜单的「粘贴」、Shift+Insert 等），它们**不产生
    KeyPress**，会直接把文本塞进控件。那样 self.real 就和显示内容脱节了，
    用户看着密码框里字是对的，点按钮却登录失败。

    与其猜 Tk 有没有绑这些，不如直接把 bindtags 打出来看。
"""
import sys


def main():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    e = tk.Entry(root)

    print("bindtags:", e.bindtags())
    print()

    print("--- 实例绑定（我们自己加的）---")
    seqs = e.bind()
    if not seqs:
        print("  (无)")
    for s in seqs:
        print("  %s" % s)
    print()

    print("--- class 'Entry' 绑定（Tk 自带）---")
    cls = e.bind_class("Entry")
    if not cls:
        print("  (无)")
    for s in cls:
        print("  %s" % s)
    print()

    print("--- 我们关心的几个入口 ---")
    targets = ["<Button-3>", "<Button-2>", "<<Paste>>", "<<Cut>>", "<<Clear>>",
               "<Shift-Insert>", "<Control-v>", "<<SelectAll>>"]
    for s in targets:
        w = bool(e.bind(s))          # 实例绑定
        c = bool(e.bind_class("Entry", s))   # 类绑定
        print("  %-16s 实例=%-5s 类=%-5s" % (s, w, c))
    print()

    # <<Paste>> 到底会不会被类绑定插进控件
    print("--- 实测 <<Paste>> 行为 ---")
    root.clipboard_clear()
    root.clipboard_append("PASTED")
    try:
        e.event_generate("<<Paste>>")
        root.update()
        print("  触发 <<Paste>> 后控件内容 = %r" % e.get())
        print("  -> 类绑定%s直接改控件内容" % ("会" if e.get() else "不会"))
    except Exception as ex:
        print("  event_generate 失败: %s: %s" % (type(ex).__name__, ex))

    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
