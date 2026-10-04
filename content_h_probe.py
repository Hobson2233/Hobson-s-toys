# -*- coding: utf-8 -*-
"""量出设置窗口的**内容需要高度**（`reqh`）。

为什么要单独量（2026-10-04）：
    `reqh = holder.winfo_reqheight() + 2 * OUTER_PAD` 是 run_gui 里的局部变量，
    外面拿不到。而它直接决定窗口高度 —— 改了竖向间距就必须同步更新
    `geometry_test.py` 里的 `CONTENT_H`，否则那个测试拿一个过时的数字
    自娱自乐，看着全绿，实际早就和界面脱节了。

做法：把 `window_geometry` 包一层，把它收到的 `reqh` 记下来。

⚠️ **这个数是随运行状态变的**（2026-10-04 实测，别再从注释里推断）：
    账号数量、账号行有没有「上次成功」、开机自启警告显不显示，都会改变内容高度。
    完整矩阵见 `content_h_matrix.py`，实测范围 **807 .. 1165**（差 358px）。
    所以 `geometry_test.CONTENT_H` 只是**一个样本输入**，不是「唯一真值」。
    下面会把每一段的高度也打出来，好让这个数字**能解释**，
    而不是一个只能整份替换的魔数。

🔴 数据安全（2026-10-04 真出过事）：
    这个脚本会跑 `run_gui()`，而 `run_gui` 启动时走 `load_all() → mutate_accounts()
    → save_accounts()` —— 也就是说**它会写真实数据**。所以必须先 `datasafe.sandbox()`，
    用 `copy_real=True` 把真实数据复制到临时目录再跑：
    量出来的仍然是「这台机器真实状态」的界面，但一个字节都不会写回真身。
    （不这么做的话，一旦同时打桩了 `load_accounts`，就会把打桩用的假账号
      写进真实 accounts.json —— 这是 2026-10-04 实际发生过的。）

用法：
    python content_h_probe.py
"""
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C      # noqa: E402
import datasafe               # noqa: E402
import proc_tree              # noqa: E402

seen = {}
_orig_wg = C.window_geometry


def spy(sw, sh, reqh):
    seen["reqh"] = reqh
    seen["sw"], seen["sh"] = sw, sh
    return _orig_wg(sw, sh, reqh)


def find_tree(root):
    """按**结构**找到内容区那几层：(holder, inner)。对不上就返回 (None, None)。

    形状是 root -> outer -> canvas -> holder -> inner。
    不按名字猜 —— 名字是局部变量，外面根本看不到。
    """
    try:
        outer = root.winfo_children()[0]
        canvas = next(c for c in outer.winfo_children()
                      if c.winfo_class() == "Canvas")
        holder = canvas.winfo_children()[0]      # create_window 的对象是子控件
        return holder, holder.winfo_children()[0]
    except Exception:
        return None, None


def _text_of(w, depth=0):
    """递归找控件里第一句有字的文字。

    ⚠️ 必须**递归**（2026-10-04 踩到）：卡片里不是直接摆 Label，中间还夹着
       body / left / right 这些布局 Frame。只看直接子控件的话，除了标题行
       之外**每张卡都取不到名字**，拆解表变成一排空白 —— 那种表还不如没有，
       因为它看着「量过了」，其实什么也没说。
    ⚠️ 圆角按钮的文字查不出来（Canvas 的 create_text 不是控件），
       只能读自绘时挂在属性上的 `_rb_text`。漏了这条，按钮在表里就是无名氏。
    ⚠️ 圆角复选框同理，挂在 `_ck_text` 上（`round_check` 返回的是个 Frame，
       但文字在它内部的 Label 上，正常递归也能取到；挂着是为了万一改成
       纯 Canvas 实现时不至于静默丢名字）。
    """
    if depth > 4:
        return ""
    t = getattr(w, "_rb_text", None) or getattr(w, "_ck_text", None)
    if t:
        return str(t)
    try:
        if w.winfo_class() in ("Label", "Checkbutton"):
            t = w.cget("text")
            if t:
                return str(t)
    except Exception:
        pass
    for c in w.winfo_children():
        t = _text_of(c, depth + 1)
        if t:
            return t
    return ""


def _breakdown(root):
    """把内容区每一段的高度打出来。

    为什么要拆（2026-10-04）：总量会随运行状态变（账号数、自启警告、提示消息），
    只报一个总数的话，下次它变了根本不知道为什么变，只能整份替换那个魔数。
    """
    holder, inner = find_tree(root)
    if holder is None:
        print("拆解            （量不出来：控件树形状和预期不符）")
        return
    print("内容区分段（holder 需要 %d px，内层 %d px）:"
          % (holder.winfo_reqheight(), inner.winfo_reqheight()))
    total = 0
    for card in inner.winfo_children():
        h = card.winfo_reqheight()
        total += h
        print("    %4d px  %-10s %s"
              % (h, card.winfo_class(),
                 _text_of(card).replace("\n", " ")[:30]))
    # 卡片之间还有 pady（card() 的默认 pady=(0,9)、标题行是 (2,10)），
    # 所以各段高度之和**不等于** holder —— 差值就是这些间距。一并报出来，
    # 免得下次看到「加起来对不上」又去怀疑测量。
    print("    各段合计 %d，间距 %d（holder %d）"
          % (total, holder.winfo_reqheight() - total, holder.winfo_reqheight()))
    # 账号行数：账号卡高度几乎完全由它决定，是最常见的「为什么又变高了」。
    #
    # ⚠️ 这里必须按**结构**一层层往下走，不能图省事取「最后一个子控件」
    #    （2026-10-04 第一版就是那么写的，结果数出来是 2 —— 那是
    #      body 里的「小标题 + 列表容器」两个控件，根本不是账号行数）。
    #    路径：卡片 box → body → [小标题, list_box] → list_inner → 各行。
    acct = [w for w in inner.winfo_children() if "已保存的账号" in _text_of(w)]
    if not acct:
        print("    账号行数        （量不出来：没找到账号卡）")
        return
    try:
        body = acct[0].winfo_children()[0]
        list_box = body.winfo_children()[-1]
        list_inner = list_box.winfo_children()[0]
        rows = list_inner.winfo_children()
    except Exception as e:
        print("    账号行数        （量不出来：%s）" % e)
        return
    # ⚠️ 不能按类名数行：分隔线也是 `tk.Frame(list_inner, bg=LINE, height=1)`，
    #    和账号行同类。但排布规律是死的 —— 行, 分隔线, 行, 分隔线, 行…，
    #    所以 N 行会产生 2N-1 个子控件，反推 N = (子控件数 + 1) // 2。
    #
    # 🔴 **空列表会被这个公式数成「1 行」**（2026-10-04 实际被骗到）：
    #    空列表时 list_inner 里是**一个 Label**「暂无保存的账号」，子控件数 = 1，
    #    公式给出 (1+1)//2 = 1 —— 看着像「有 1 个账号」，而真相是**一个都没有**。
    #    这个假读数特别毒：它和我当时的预期（真实数据里确实有 1 个账号）**正好吻合**，
    #    于是我把它当成「数据没问题」的证据，而实际上探针读的是另一个目录
    #    （见 datasafe.real_dir() 的说明），界面里根本是空的。
    #    所以这里必须先认那句话，再套公式。
    n = len(rows)
    if n == 1 and "暂无保存的账号" in _text_of(rows[0]):
        print("    账号行数 0（列表里只有一句「暂无保存的账号」——"
              "**注意别按子控件数反推成 1**）")
        return
    print("    账号行数 %d（列表里 %d 个子控件 = %d 行 + %d 条分隔线）"
          % ((n + 1) // 2, n, (n + 1) // 2, max(n - (n + 1) // 2, 0)))


_orig_mainloop = tk.Tk.mainloop


def _report(self):
    print("屏幕            %dx%d" % (seen.get("sw", 0), seen.get("sh", 0)))
    print("内容需要高度    reqh = %s" % seen.get("reqh"))
    print("窗口 geometry   %s" % self.winfo_geometry())
    print("客户区          %dx%d" % (self.winfo_width(), self.winfo_height()))
    cap = int(seen.get("sh", 0) * C.SCREEN_H_RATIO)
    r = seen.get("reqh") or 0
    print("屏幕比例上限    %d  ->  内容%s"
          % (cap, "装得下（不滚动）" if r <= cap else "装不下（会出现滚动条）"))
    print()
    _breakdown(self)


def _mainloop(self, *a, **k):
    self.after(2600, lambda: _report(self))
    self.after(3400, self.quit)
    _orig_mainloop(self, *a, **k)


def install():
    """装好打桩。**必须在 run_gui 之前调用。**"""
    C.window_geometry = spy
    tk.Tk.mainloop = _mainloop


def uninstall():
    C.window_geometry = _orig_wg
    tk.Tk.mainloop = _orig_mainloop


def main():
    try:
        # 必须在 run_gui 之前：把真实数据复制进临时目录，之后所有写入都落在那里
        datasafe.sandbox(copy_real=True, tag="content_h_probe")
        install()
        C.run_gui()
    finally:
        uninstall()
    try:
        print()
        print(datasafe.assert_untouched())
    except RuntimeError as e:
        print(e)
        return 1
    return 0


# ⚠️ 这里**必须**有 `if __name__` 守卫（2026-10-04 踩到）：
#    原来 run_gui() 和 exit_hard() 直接写在模块级 —— 于是 `import content_h_probe`
#    就等于「跑一遍探针然后硬退进程」。我拿它当库复用时，后面写的打桩代码
#    一行都没执行，输出看起来还正常（只是数字不对），静默骗了我一次。
#    探针也应该可以被 import 复用（比如只想用它的 _breakdown）。
if __name__ == "__main__":
    proc_tree.exit_hard(main())
