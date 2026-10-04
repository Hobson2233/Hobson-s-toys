# -*- coding: utf-8 -*-
"""量一下设置窗口的布局：内容有没有被窗口裁掉、控件有没有重叠。

为什么需要：
    「按钮压住输入框」「底部控件够不到」这类问题逻辑测试永远发现不了 ——
    逻辑全绿，界面依然可能是坏的。截图能看出来，但看不出**差多少**、
    也不知道在别的屏幕尺寸上会不会更糟。这个脚本把关键尺寸打出来。

做法和 ui_shot.py 一样：在 Tk 自己的事件循环里量（另起进程拿不到桌面）。

🔴 数据安全（2026-10-04 加）：
    这个脚本跑的是**真实**的 `run_gui()`，而 run_gui 启动时会走
    `load_all() → mutate_accounts() → save_accounts()` —— **它会写真实 accounts.json**。
    原来这里没有沙箱，属于「意图只读、行为写盘」。现在先 `datasafe.sandbox()`
    （`copy_real=True`：量到的仍然是真实状态的界面，但真身一个字节都不动）。

用法：
    python layout_probe.py
"""
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C
import datasafe
import proc_tree

_orig_mainloop = tk.Tk.mainloop
_orig_init = C.PasswordField.__init__
fields = []


def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    fields.append(self)


def walk(w):
    yield w
    for c in w.winfo_children():
        for x in walk(c):
            yield x


PROBLEMS = []


def _label_of(w):
    """取控件的「文字」，取不到就返回空串。

    ⚠️ 为什么不能只认 `tk.Button`/`Label`/`Checkbutton`（2026-10-04 修）：
        界面美化后主按钮和账号行的「选用 / 删除」都换成了 **Canvas 手绘的
        圆角按钮**（见 campus_login.round_button）。它们 `winfo_class()` 是
        "Canvas"，走不到 `cget("text")` 那条分支 —— 于是下面「最靠下的 6 个控件」
        那张表里，它们会显示成 `Canvas ''`。
        这个退化是**静默的**：表照样打得出来，只是最该被认出来的那个控件
        （被裁掉的往往正是最底下那排按钮）成了无名氏，排查时白看。
        Canvas 的 `create_text` 内容查不出来，所以按钮把文字挂在 `_rb_text` 上。
    ⚠️ 同理，圆角复选框（`round_check`）把文字挂在 `_ck_text` 上 —— 也要认。
    """
    try:
        if w.winfo_class() in ("Button", "Label", "Checkbutton"):
            return w.cget("text")
    except Exception:
        pass
    return getattr(w, "_rb_text", "") or getattr(w, "_ck_text", "")


def describe(root):
    print("=" * 62)
    print("屏幕: %dx%d" % (root.winfo_screenwidth(), root.winfo_screenheight()))
    print("窗口 geometry: %s" % root.winfo_geometry())
    print("客户区: %dx%d" % (root.winfo_width(), root.winfo_height()))
    print("内容需要高度 winfo_reqheight: %d" % root.winfo_reqheight())
    print("   （注意：内容进了 canvas 之后这个值不再反映内容高度，仅供参考）")
    print("最小尺寸 minsize: %s" % (root.minsize(),))

    client_h = root.winfo_height()
    root_y = root.winfo_rooty()
    print("-" * 62)

    # 找出复选框（开机自启那个）和密码框按钮
    #
    # ⚠️ 2026-10-04 起复选框**不再是 `tk.Checkbutton`**：换成了 Canvas 自绘的
    #    圆角复选框（`campus_login.round_check`，返回一个挂了 `_ck_text` 的 Frame）。
    #    只认类名的话这一整节会**一条都不打印** —— 而「什么都没打印」和
    #    「本来就没有复选框」在输出上一模一样，正是最该避免的那种假绿灯。
    #    所以这里两个条件都认，并且**找不到就记进 PROBLEMS**。
    found_chk = 0
    for w in walk(root):
        try:
            cls = w.winfo_class()
        except Exception:
            continue
        ck = getattr(w, "_ck_text", None)
        if cls != "Checkbutton" and not ck:
            continue
        found_chk += 1
        text = ck if ck else w.cget("text")
        top = w.winfo_rooty() - root_y
        bottom = top + w.winfo_height()
        ok = bottom <= client_h
        if not ok:
            PROBLEMS.append("复选框 %r 被裁掉 %d px" % (text, bottom - client_h))
        print("复选框 文字=%r（%s）" % (text, cls))
        print("    y=%d..%d  客户区高=%d  -> %s"
              % (top, bottom, client_h,
                 "可见" if ok else "**被裁掉 %d px**" % (bottom - client_h)))
    if not found_chk:
        PROBLEMS.append("一个复选框都没找到 —— 要么界面没建起来，"
                        "要么它不再挂 _ck_text 了，这一节正在空转")

    # 密码框：输入区 + 「显示」按钮，检查是否重叠
    if fields:
        f = fields[0]
        e, b = f.entry, f.button
        print("-" * 62)
        print("密码框: 输入区 x=%d..%d (宽 %d)"
              % (e.winfo_rootx() - root.winfo_rootx(),
                 e.winfo_rootx() - root.winfo_rootx() + e.winfo_width(),
                 e.winfo_width()))
        print("        「显示」按钮 x=%d..%d (宽 %d)"
              % (b.winfo_rootx() - root.winfo_rootx(),
                 b.winfo_rootx() - root.winfo_rootx() + b.winfo_width(),
                 b.winfo_width()))
        gap = (b.winfo_rootx() - root.winfo_rootx()) - \
              (e.winfo_rootx() - root.winfo_rootx() + e.winfo_width())
        if gap < 0:
            PROBLEMS.append("密码框输入区与「显示」按钮重叠 %d px" % -gap)
        if e.winfo_width() < 120:
            PROBLEMS.append("密码框输入区过窄（%d px）" % e.winfo_width())
        print("        两者间距: %d px  -> %s" % (gap, "不重叠" if gap >= 0 else "**重叠**"))

    # 最后几个控件的底边，看哪个最先被裁
    print("-" * 62)
    rows = []
    for w in walk(root):
        try:
            if not w.winfo_ismapped():
                continue
            top = w.winfo_rooty() - root_y
            rows.append((top + w.winfo_height(), top, w.winfo_class(),
                         _label_of(w)))
        except Exception:
            pass
    rows.sort(reverse=True)
    print("最靠下的 6 个控件（底边 y / 顶边 y / 类型 / 文字）:")
    for bottom, top, cls, txt in rows[:6]:
        flag = "" if bottom <= client_h else "  <== 超出客户区 %d px" % (bottom - client_h)
        print("   %5d / %5d  %-12s %r%s" % (bottom, top, cls, txt, flag))

    print("-" * 62)
    if PROBLEMS:
        print("结论: **有问题**")
        for p in PROBLEMS:
            print("   - %s" % p)
    else:
        print("结论: 通过 —— 控件都在窗口内，没有重叠")
    print("=" * 62)


def _mainloop(self, *a, **k):
    self.after(2600, lambda: describe(self))
    self.after(3400, self.quit)
    _orig_mainloop(self, *a, **k)


def main():
    # 兜底看门狗：卡住也不把窗口留在用户桌面上
    proc_tree.arm_watchdog(60, "layout_probe.py")
    C.PasswordField.__init__ = _init
    tk.Tk.mainloop = _mainloop
    try:
        # 必须在 run_gui 之前：把真实数据复制进临时目录，之后所有写入都落在那里
        datasafe.sandbox(copy_real=True, tag="layout_probe")
        C.run_gui()
    except Exception as e:
        print("run_gui 异常: %s: %s" % (type(e).__name__, e))
    finally:
        C.PasswordField.__init__ = _orig_init
        tk.Tk.mainloop = _orig_mainloop
    # ⚠️ 这句必须在 describe() **之后**才打印得出来，而 describe() 是在
    #    2600ms 的事件里跑完的 —— 所以它影响不了那份「结论」（那份已经印完了），
    #    只能影响退出码。别把它 append 进 PROBLEMS 假装结论里会有，
    #    那是个假的补救（结论早印完了，没人会再读一次）。
    dirty = None
    try:
        print(datasafe.assert_untouched())
    except RuntimeError as e:
        print(e)
        dirty = e
    # **不要 disarm()** —— exit_hard() 就在下一句，os._exit() 会带走守护线程。
    # 提前解除等于把唯一的救命绳解开了：真挂住时就没人救。
    # 有结论就给出退出码，方便脚本化判断（注意：这个脚本要建 Tk 窗口，
    # 在受控会话里可能挂住，所以别拿它当构建门槛，只当诊断工具）
    proc_tree.exit_hard(1 if (PROBLEMS or dirty) else 0)


if __name__ == "__main__":
    main()
