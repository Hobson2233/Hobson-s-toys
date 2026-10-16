# -*- coding: utf-8 -*-
"""给真实的设置窗口截图（在 Tk 自己的事件循环里调度，不另起进程）。

为什么要这个：
    前几层测试验的都是**逻辑** —— 逻辑全绿，界面也可能排得乱七八糟
    （按钮压住输入框、文字被裁掉、密码框窄到看不清）。这一层只能靠眼睛，
    所以留个工具把真实窗口拍下来。

为什么不另起进程抓：
    受控会话里新起的进程拿不到交互桌面，枚举窗口会「找不到可见窗口」。
    改成在同一进程里、用 Tk 的 after() 在事件循环内截图，就绕开了这个问题。

⚠️ 隐私：**截图里不能出现真实学号/密码**。
    截图里的文字是**光栅化像素**，不是字符串 —— 所以「解开 PNG 的 IDAT 再搜学号」
    这种自动检查天生搜不到（实测连窗口标题「校园网自动登录」都搜不出来），
    它报出来的「干净」是**假阴性**，等于没查。
    既然查不出来，就只能从**数据源头**保证：跑之前先把数据目录整个换到一个
    临时目录、塞进假账号（`sandbox()`），这样窗口里根本没有真东西可拍。
    另外「点显示」那一步用的是假密码 `demo-1234`。

用法：
    python ui_shot.py            # 输出到 ui_shots/
"""
import os
import sys
import tempfile
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C
import shot
import proc_tree

OUT = os.path.join(HERE, "ui_shots")
PWD_DEMO = "demo-1234"          # 假密码：演示「显示」按钮用，不能是真密码

# 截图用的假数据。学号用明显编造的号段，一眼能看出不是真的。
DEMO_DIR = os.path.join(tempfile.gettempdir(), "campus_ui_shot")
DEMO_UID_1 = "2026000001"
DEMO_UID_2 = "2026000002"
DEMO_ACCOUNTS = {
    # ⚠️ `school` 是「这个账号属于哪所学校」（2026-10-12 加）。
    #    故意让两个账号分属不同学校 —— 第 6 张图拍的就是「属于别的学校」那个警示标签，
    #    否则那条分支永远不会被人看见。
    DEMO_UID_1: {"passwd": PWD_DEMO, "lastSuccess": "2026-01-01 09:00:00",
                 "lastAttempt": "2026-01-01 09:00:00", "lastResult": "ok",
                 "school": "zua"},
    DEMO_UID_2: {"passwd": "demo-5678", "lastSuccess": "2026-01-01 08:30:00",
                 "lastAttempt": "2026-01-01 08:30:00", "lastResult": "ok",
                 "school": "zjxu"},
}


def sandbox():
    """把数据目录换到临时目录，并写入假账号。**必须在 run_gui() 之前调用。**

    `campus_login` 在**导入期**就把 CONFIG_FILE 等算好了（模块级常量），
    所以光改 `_DATA_DIR` 不够 —— 四个常量也得一起重指。
    """
    os.makedirs(DEMO_DIR, exist_ok=True)
    C._DATA_DIR[:] = [DEMO_DIR]
    C.CONFIG_FILE = os.path.join(DEMO_DIR, "config.json")
    C.ACCOUNTS_FILE = os.path.join(DEMO_DIR, "accounts.json")
    C.LOG_FILE = os.path.join(DEMO_DIR, "campus-login.log")
    C.RESULT_FILE = os.path.join(DEMO_DIR, "last-result.json")

    # ⚠️ 只写「用户真的会写的东西」—— 别拿 `dict(C.DEFAULTS)` 打底。
    #    那样会往 demo 的 config.json 里塞进 4 个「化石键」，
    #    而 load_config 现在会把它们过滤掉、set_school 还会把它们清掉，
    #    于是截图里会多出一段「清掉了老版本留下的旧参数」的提示，
    #    而那不是要演示的常态（来龙去脉见 campus_login._fossil_keys）。
    cfg = {"userId": DEMO_UID_1, "passwd": PWD_DEMO}
    C.save_config(cfg)
    C.save_accounts({"accounts": DEMO_ACCOUNTS, "lastOnline": DEMO_UID_1})
    print("沙箱数据目录: %s（已写入假账号 %s / %s）"
          % (DEMO_DIR, DEMO_UID_1, DEMO_UID_2))
    # 自检：确认真的换了地方，别拍完才发现写回了真实数据目录
    real = os.path.join(C._shell_folder(C.CSIDL_COMMON_APPDATA) or "", C.APP_NAME)
    if os.path.normcase(os.path.abspath(C.CONFIG_FILE)).startswith(os.path.normcase(real)):
        raise RuntimeError("沙箱没生效，CONFIG_FILE 仍指向真实数据目录: %s" % C.CONFIG_FILE)


fields = []                     # 记下 run_gui 建的那个 PasswordField
apps = []                       # 记下 run_gui 建的那个 LoginApp（第 6 张图要用）
_orig_init = C.PasswordField.__init__
_orig_app_init = C.LoginApp.__init__
_orig_mainloop = tk.Tk.mainloop


def _init(self, *a, **k):
    _orig_init(self, *a, **k)
    fields.append(self)


def _app_init(self, *a, **k):
    _orig_app_init(self, *a, **k)
    apps.append(self)


def grab(widget, name):
    """拍一个 Tk 顶层窗口，存成 PNG。返回是否成功。"""
    try:
        widget.update_idletasks()
        hwnd = shot.hwnd_of(widget)
        data, w, h = shot.capture(hwnd, foreground=False, settle=0.25)
        p = os.path.join(OUT, name + ".png")
        with open(p, "wb") as f:
            f.write(data)
        print("  已保存 %-22s %dx%d  %d 字节" % (name + ".png", w, h, len(data)))
        return True
    except Exception as e:
        print("  截图失败 %-18s %s: %s" % (name, type(e).__name__, e))
        return False


def find_button(w, text):
    """在控件树里按文字找按钮/导航项（run_gui 里它们都是局部变量，拿不到引用）。

    ⚠️ 2026-10-04 起按钮**不再都是 tk.Button**：「使用说明」和三个主按钮换成了
    Canvas 手绘的圆角按钮（见 campus_login.round_button）。它们把文字挂在
    `_rb_text` 属性上，这里一并认。
    ⚠️ 2026-10-16 起「使用说明」**不再是按钮**，而是左侧导航栏里的一项
    （`campus_login.nav_button`）。它同样挂 `_rb_text`，所以这里一个字都不用改
    —— 但下面 `open_help` 得改成「切页」而不是「等一个弹窗」。

    为什么要专门说这一句：漏认的后果是**静默的** —— 找不到就只打印一行
    「没找到」，后面的截图流程照样跑完，看起来像成功了，其实第 5 张图是空的。
    """
    for c in w.winfo_children():
        try:
            if isinstance(c, tk.Button) and c.cget("text") == text:
                return c
            if getattr(c, "_rb_text", None) == text:
                return c
        except Exception:
            pass
        r = find_button(c, text)
        if r:
            return r
    return None


def find_page(w, name):
    """按导航项的文字切到那一页。返回是否切成功。"""
    b = find_button(w, name)
    if b is None or not callable(getattr(b, "invoke", None)):
        return False
    b.invoke()
    w.update_idletasks()
    return True


def schedule(root):
    """在事件循环里按时间排好几次截图。"""
    f = fields[0] if fields else None

    def initial():
        print("[1] 初始状态（密码是配置里读出来的，打码显示）")
        grab(root, "01_初始状态")

    def demo_type():
        # 换成假密码再演示，免得把真实密码拍进图里
        if f:
            f.set(PWD_DEMO)
            f.set_focus(True)
        print("[2] 正在输入 -> 显示 %r" % (f.entry.get() if f else "?"))

    def shot_typing():
        grab(root, "02_正在输入只露最后一位")

    def do_reveal():
        if f:
            f.toggle_reveal()
        print("[3] 点了「显示」-> 显示 %r，按钮文字 %r"
              % (f.entry.get() if f else "?", f.button.cget("text") if f else "?"))

    def shot_reveal():
        grab(root, "03_点了显示看全部")

    def hide_again():
        if f:
            f.toggle_reveal()

    def shot_hidden():
        grab(root, "04_再点一次盖回去")

    def open_help():
        # ⚠️ 2026-10-16：「使用说明」从**弹窗**变成了**左侧导航栏里的一页**，
        #    所以这里是「切页」而不是「点按钮等窗口」。
        ok = find_page(root, "使用说明")
        print("[5] 使用说明（导航栏）: %s" % ("切过去了" if ok else "**没找到**"))

    def shot_help():
        # 页在同一个窗口里 —— 拍 root 本身就是拍它
        grab(root, "05_使用说明页")
        find_page(root, "设置")          # 切回去，后面那张要拍设置页

    def switch_school():
        """第 6 张：切到嘉兴大学 —— 账号行上「属于别的学校」的警示标签就会出现。

        ⚠️ 走的是**真的** `on_school()`（不是自己拼个界面状态）：它要写 config.json、
           要重画账号列表、还要弹提示消息，这几步都得被真跑一遍才拍得到。
        """
        a = apps[0] if apps else None
        print("[6] 切到嘉兴大学（演示账号行上的「别的学校」警示标签）")
        if not a:
            print("  **没拿到 LoginApp 实例**，跳过")
            return
        a.on_school("zjxu")

    def shot_switched():
        grab(root, "06_切到嘉兴后账号行的警示标签")

    root.after(2600, initial)
    root.after(3200, demo_type)
    root.after(3600, shot_typing)
    root.after(4200, do_reveal)
    root.after(4600, shot_reveal)
    root.after(5200, hide_again)
    root.after(5600, shot_hidden)
    root.after(6200, open_help)
    root.after(6800, shot_help)
    root.after(7600, switch_school)
    root.after(8200, shot_switched)
    root.after(9000, root.quit)


def _mainloop(self, *a, **k):
    schedule(self)
    _orig_mainloop(self, *a, **k)


def main():
    os.makedirs(OUT, exist_ok=True)
    # 兜底：这个脚本要建真窗口，卡住就会把窗口留在用户桌面上（还显示成「未响应」）。
    # 到点还没收尾就硬退，绝不留窗口。
    proc_tree.arm_watchdog(90, "ui_shot.py")
    C.PasswordField.__init__ = _init
    C.LoginApp.__init__ = _app_init
    tk.Tk.mainloop = _mainloop
    # **不要在这里声明 DPI 感知**：程序本身（打包出来的 exe）是 DPI 不感知的，
    # Windows 会给它一个「虚拟化」的坐标空间。声明感知后 Tk 看到的是物理像素，
    # 窗口几何跟着变大（实测 820x811 变成 1148x1135），截出来就不是用户看到的样子了。
    print("开始：打开真实设置窗口，按顺序截图")
    try:
        sandbox()
        C.run_gui()
    except Exception as e:
        print("run_gui 异常: %s: %s" % (type(e).__name__, e))
    finally:
        C.PasswordField.__init__ = _orig_init
        C.LoginApp.__init__ = _orig_app_init
        tk.Tk.mainloop = _orig_mainloop
    print("完成，输出目录: %s" % OUT)
    # **不要 disarm()** —— exit_hard() 就在下一句，os._exit() 会带走守护线程。
    # 提前解除等于把唯一的救命绳解开了：真挂住时就没人救。
    proc_tree.exit_hard(0)


if __name__ == "__main__":
    main()
