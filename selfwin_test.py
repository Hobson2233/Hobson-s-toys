# -*- coding: utf-8 -*-
"""「上网数据」面板（`campus_login.SelfServiceWindow`）的**界面自检**。

为什么非要有这个文件（2026-10-12）：
    面板写完之后只过了 `py_compile` 和 AST 扫描 —— 那两样只能证明「语法对、
    没有明显坏味道」，**证明不了「窗口真的建得起来」**。而 tkinter 有一整类
    失败是**不抛异常**的，只能靠真的建一遍才看得见：

      · 🔴 `PhotoImage` 没留引用 → 图片被 GC 掉，Label 一片空白，**不报错**
      · `round_panel(pad_x/pad_y < radius)` → 圆角被白方块糊掉，**不报错**
      · 控件被摆到窗口外 / 挤没了 → **不报错**
      · `_entry` 指向已经 destroy 掉的控件 → 只有特定点击顺序才炸

    所以这一档**真的 `tk.Tk()` 建窗口**，而且**真的把事件循环转起来**
    （`root.update()` 轮询），让它走完「取验证码 → 画界面 → 提交 → 画数据」。

🔴 为什么归 `env` 档，不归 `fast` 档：
    它要真桌面（真建窗口、真跑事件循环）。项目里所有建真窗口的脚本
    一律归 env 档（`pwd_gui_test.py` / `layout_probe.py` / `ui_shot.py`…）。

    ⚠️ 收尾**必须**用 `proc_tree.exit_hard()`，不能用 `os._exit()` ——
       后者在 Windows 上要等 Tcl/Tk 的 DLL 卸载（11~12 秒，且不可靠），
       受限会话等不及会把进程组 SIGTERM 掉，退出码就变成「被信号杀死」。
       2026-10-12 就是在这儿绕了一大圈（一度误判成「建 Toplevel 就会被杀」）。

🔴 **不联网**：把 `self_service.SelfService` 整个换成假的（`FakeSvc`），
    假对象只回放固定数据。而且测试期间把 `socket.connect` 换成「一调就炸」——
    所以「不联网」是**被强制**的，不是靠自觉。末尾还会断言
    **所有请求都不是在 Tk 主线程上发的**（发在主线程会把界面冻住）。

🔴 **不碰真实数据**：先 `datasafe.sandbox()`。面板出错时会走 `log()` 写日志，
    不沙箱就会写到真实数据目录里。收尾还有 `datasafe.assert_untouched()`。

用法：
    python selfwin_test.py
"""
import gc
import json
import os
import socket
import struct
import sys
import threading
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import datasafe                # noqa: E402  （import 它就会 import campus_login）
import proc_tree               # noqa: E402
import self_service as S       # noqa: E402

C = datasafe.C                 # campus_login 模块（和 datasafe 里那个是同一个）

FAILS = []

# ---------------------------------------------------------------- 假验证码图

CAPTCHA_W, CAPTCHA_H = 130, 40          # 和学校真实验证码同尺寸（实测）


def _png(w, h, rgb_rows):
    """手搓一张真 PNG（纯标准库，不依赖 PIL）。

    ⚠️ 必须是**真的 PNG 字节**：我们要验的正是「Tk 能不能从字节解出图」，
       喂一张假图等于什么都没验。
    """
    raw = b"".join(b"\x00" + bytes(r) for r in rgb_rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _make_captcha():
    rows = []
    for y in range(CAPTCHA_H):
        row = []
        for x in range(CAPTCHA_W):
            row += [40, 60, 90] if ((x // 8 + y // 8) % 2 == 0) else [235, 240, 248]
        rows.append(row)
    return _png(CAPTCHA_W, CAPTCHA_H, rows)


CAPTCHA_PNG = _make_captcha()

# ---------------------------------------------------------------- 假数据层

FAKE_DATA = {
    "status": "正常",
    "plan": "校园网电信融合套餐",
    "area": "电信",
    "expire": "2038-01-01",
    "billing_day": "2026-10-06",
    "charge_type": "1",
    "balance": 12.5,
    "flow": {"today": 123.5, "month": 18454.78, "total": 99000.0,
             "limit": "100 GB"},
    "time": {"today": 3.25, "month": 120.5, "total": 999.0, "limit": "不限"},
    "devices": [{"ip": "10.1.2.3", "mac": "AA-BB-CC-DD-EE-FF",
                 "since": "2026-10-12 08:00", "os": "Windows",
                 "nas": "NAS1", "kind": "PC"}],
}


class FakeSvc(object):
    """`self_service.SelfService` 的替身。**一个字节都不出网。**

    行为由类属性 `script` 逐条测试现改，这样同一个替身能把
    「会话在 / 不在 / 登录成功 / 验证码错 / 密码错」几条路都走一遍。
    """

    made = 0                  # 被建了几个（用来确认真的换成了替身）
    off_main = True           # 所有请求是否都发生在**非**主线程
    last_login = None         # 最后一次 login 的实参
    script = {}

    def __init__(self, base=None, timeout=None):
        FakeSvc.made += 1
        self.base = base
        self.calls = []

    def _note(self, name):
        self.calls.append(name)
        if threading.current_thread() is threading.main_thread():
            FakeSvc.off_main = False

    def has_session(self, account):
        self._note("has_session")
        mode = FakeSvc.script.get("has_session", "no")
        if mode == "raise":
            raise S.SelfError("net", "假装连不上校园网")
        return mode == "yes"

    def fetch_captcha(self):
        self._note("fetch_captcha")
        if FakeSvc.script.get("fetch_captcha") == "raise":
            raise S.SelfError("net", "假装取验证码失败")
        return CAPTCHA_PNG, False

    def login(self, account, password, code):
        self._note("login")
        FakeSvc.last_login = (account, password, code)
        mode = FakeSvc.script.get("login", "ok")
        if mode == "captcha":
            raise S.SelfError("captcha", "验证码错误")
        if mode == "login":
            raise S.SelfError("login", "账号或密码不正确")
        if mode == "net":
            raise S.SelfError("net", "假装连不上校园网")
        if mode == "boom":
            # **非** SelfError —— 专门用来验「后台任务崩了界面也得看得见」
            raise RuntimeError("假装代码写错了")

    def load(self, account):
        self._note("load")
        return json.loads(json.dumps(FAKE_DATA))    # 深拷贝，防被测代码改到


# ---------------------------------------------------------------- 小工具

def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-56s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    ok = bool(cond)
    print("  [%s] %-56s %s" % ("OK" if ok else "失败", name, detail))
    if not ok:
        FAILS.append(name)


def all_widgets(w):
    out = []
    stack = [w]
    while stack:
        x = stack.pop()
        out.append(x)
        try:
            stack.extend(x.winfo_children())
        except Exception:
            pass
    return out


def texts_of(w):
    """界面上所有看得见的文字。

    ⚠️ `round_chip` / `round_button` 是 Canvas 画的，文字**不在** `cget("text")`
       里，而是挂在 `_rb_text` 上（见 round_chip 的说明）。只查 `cget` 的话
       按钮文字会被整个漏掉 —— 那样「确定按钮还在不在」这条断言就永远是假绿。
    """
    out = []
    for c in all_widgets(w):
        t = None
        try:
            t = c.cget("text")
        except Exception:
            pass
        if not t:
            t = getattr(c, "_rb_text", None)
        if t:
            out.append(str(t))
    return out


def images_of(w):
    """界面上所有挂了图片的控件 → [(控件, Tk 里的图片名)]。"""
    out = []
    for c in all_widgets(w):
        try:
            v = c.cget("image")
        except Exception:
            continue
        if v:
            out.append((c, str(v)))
    return out


def tk_image_size(root, name):
    """直接问 Tk：这个图片对象还在不在、多大。

    🔴 这是**唯一**能验出「PhotoImage 被 GC 掉」的办法 ——
       对象没了的话 Python 侧看不出任何异常，但 Tk 里那个 image 已经被销毁，
       这里会抛 TclError。只断言 `w._cap_big is not None` 是**不够的**。
    """
    try:
        return (int(root.tk.call("image", "width", name)),
                int(root.tk.call("image", "height", name)))
    except Exception:
        return None


def pump(root, seconds=6.0, until=None):
    """把事件循环转起来，直到 `until()` 成立或超时。返回是否等到。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            root.update()
        except Exception:
            break
        if until is not None and until():
            return True
        time.sleep(0.02)
    return bool(until is None or until())


def has_text(win, *needles):
    t = texts_of(win)
    return all(any(n in x for x in t) for n in needles)


# ---------------------------------------------------------------- 测试

def test_available():
    """`self_service_available()`：只有郑航有自助服务。"""
    print("\n① 入口只在郑航出现")
    check("郑航（默认）", C.self_service_available({"school": "zua"}), True)
    check("嘉兴大学", C.self_service_available({"school": "zjxu"}), False)
    check("学校字段是垃圾值 → 退回郑航默认",
          C.self_service_available({"school": "???"}), True)
    check("没配学校 → 默认郑航", C.self_service_available({}), True)


def test_panel_session_miss(root):
    """走「没有会话 → 取验证码 → 画界面 → 验证码错 → 3 次闸门 → 关窗」。"""
    print("\n② 面板：没有会话 → 验证码界面")
    FakeSvc.script = {"has_session": "no", "login": "ok"}
    FakeSvc.made = 0
    FakeSvc.off_main = True

    w = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230",
                            "not-a-real-pwd", "http://202.196.169.246")
    ok = pump(root, 6.0, lambda: w._cap_big is not None)
    check_true("验证码界面画出来了（等到了图）", ok, "")

    # --- 🔴 图片必须真的活着 ---
    check_true("_cap_big 有引用", w._cap_big is not None, "")
    check("原图尺寸", (w._cap_small.width(), w._cap_small.height()),
          (CAPTCHA_W, CAPTCHA_H))
    check("放大 4 倍后的尺寸", (w._cap_big.width(), w._cap_big.height()),
          (CAPTCHA_W * 4, CAPTCHA_H * 4))

    # 界面上确实有一个 Label 挂着这张图
    imgs = images_of(w.win)
    check("挂图的控件数", len(imgs), 1)
    if imgs:
        check("挂的正是放大后的那张", imgs[0][1], str(w._cap_big))

    # 🔴 GC 之后图还得在（这才是「没留引用」那个坑的真检测）
    gc.collect()
    root.update()
    check("GC 之后 Tk 里的图片仍在（没被回收）",
          tk_image_size(root, str(w._cap_big)), (CAPTCHA_W * 4, CAPTCHA_H * 4))
    check_true("GC 之后界面还挂着图", len(images_of(w.win)) == 1, "")

    # --- 🔴 窗口必须宽到放得下验证码图 ---
    #     这是 2026-10-12 截图量出来的 bug：正文卡片是 `round_panel` 返回的
    #     **Canvas**，而 Canvas 的 `reqwidth` 只等于它自己的 `width` 选项、
    #     **不跟着里面塞的图长** ⇒ 窗口停在标题行撑出来的 ~406px，
    #     卡片可用宽度只有 350px，图右边 1/3 被**静默裁掉**。
    #     用户根本读不全 4 个字符，功能等于不可用，而 Tk 一声不吭。
    #     ⚠️ 所以这条只能**量**：截图里缺一角的棋盘格看着还是棋盘格。
    root.update()
    avail = w.win.winfo_width() - 2 * C.OUTER_PAD - 2 * 14
    check_true("窗口真的被映射了（否则下面那条是假绿）",
               w.win.winfo_width() > 100,
               "（实测宽 %d）" % w.win.winfo_width())
    check_true("窗口宽到放得下验证码图（没被裁）",
               avail >= w._cap_big.width(),
               "（可用 %d vs 图 %d）" % (avail, w._cap_big.width()))

    # --- 输入框 ---
    check_true("有输入框", w._entry is not None, "")
    if w._entry is not None:
        w._entry.insert(0, "a1b2")
        check("输入框里读得到刚打的字", w._entry.get(), "a1b2")

    # --- 提交 → 登录成功 → 数据界面 ---
    w._submit()
    ok = pump(root, 6.0, lambda: "账号状态" in texts_of(w.win))
    check_true("提交后走到了数据界面", ok, "")
    check("发给学校的验证码就是刚输入的那个",
          FakeSvc.last_login[2] if FakeSvc.last_login else None, "a1b2")
    check("登录用的账号", FakeSvc.last_login[0] if FakeSvc.last_login else None,
          "2507210230")

    # --- 关窗 ---
    w.close()
    check_true("close 之后标记为已关", w.closed, "")
    w.close()                                   # 幂等：再来一次不许炸
    check_true("close 可以重复调用", True, "")
    w._put("data", FAKE_DATA)                   # 关了之后不该再进队列
    check_true("关窗后新结果不再入队", w.q.empty(), "")
    return w


def test_panel_captcha_wrong(root):
    """验证码错 → 允许重试（换一张），且**新图必须真的换掉旧的**。"""
    print("\n③ 验证码错：可以重试，且不能拿旧图糊弄")
    FakeSvc.script = {"has_session": "no", "login": "captcha"}
    w = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230", "p",
                            "http://202.196.169.246")
    pump(root, 6.0, lambda: w._cap_big is not None)
    w._entry.insert(0, "zzzz")
    w._submit()
    ok = pump(root, 6.0, lambda: "验证码不对" in " ".join(texts_of(w.win)))
    check_true("提示了「验证码不对」", ok, "")
    check("登录只发了 1 次（验证码错也算 1 次，防锁号）", w.tries, 1)
    check_true("还能继续试（tries 没到上限）", w.tries < C.SELF_LOGIN_MAX, "")
    check_true("「确定」按钮还在（允许重试）",
               "确定" in texts_of(w.win), "")
    check_true("有「换一张」", "换一张" in texts_of(w.win), "")

    # 🔴 换新图失败时**不许**继续显示旧图，而且**必须照样说清为什么失败**。
    #    这条路只有走 `_job_login` 才到得了：验证码错 → 顺手取新图 → 新图没取到
    #    → png 是 None。原来的写法会「提示整个消失」（note 被当成标志位用，
    #    见 `_handle` 里那段说明），所以这里钉两件事：① 不留旧图；② 提示还在。
    FakeSvc.script = {"has_session": "no", "login": "captcha"}
    w2 = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230", "p",
                             "http://202.196.169.246")
    pump(root, 6.0, lambda: w2._cap_big is not None)
    old2 = str(w2._cap_big)          # 先正常拿到一张，后面才知道「旧的是哪张」
    FakeSvc.script["fetch_captcha"] = "raise"      # 从现在起取不到新图
    w2._entry.insert(0, "zzzz")
    w2._submit()
    ok = pump(root, 6.0, lambda: "新的没取到" in " ".join(texts_of(w2.win)))
    check_true("取不到新图时明说「显示不出来」", ok, "")
    check_true("取不到新图时不留旧图引用", w2._cap_big is None,
               "（旧引用 = %s）" % ("还在" if w2._cap_big else "已清"))
    check_true("旧图在界面上也确实没了", len(images_of(w2.win)) == 0, "")
    check_true("旧图名已经不在任何控件上",
               all(n != old2 for _c, n in images_of(w2.win)), "")
    check_true("提示没被吞掉（这条原来就是 bug）",
               any("验证码不对" in x for x in texts_of(w2.win)), "")
    w2.close()
    w.close()


def test_panel_login_fail(root):
    """密码错 → **不许重试**，把「确定」收起来。"""
    print("\n④ 密码错：不许重试")
    FakeSvc.script = {"has_session": "no", "login": "login"}
    w = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230", "wrong",
                            "http://202.196.169.246")
    pump(root, 6.0, lambda: w._cap_big is not None)
    w._entry.insert(0, "a1b2")
    w._submit()
    ok = pump(root, 6.0, lambda: "登录不上自助服务" in " ".join(texts_of(w.win)))
    check_true("提示了「登录不上自助服务」", ok, "")
    check_true("把「确定」按钮收起来了（不给重试）",
               "确定" not in texts_of(w.win), "")
    check_true("说清了原因不是验证码",
               any("这不是验证码的问题" in x for x in texts_of(w.win)), "")
    w.close()


def test_panel_tries_gate(root):
    """3 次闸门：到了上限只剩「换一张」。"""
    print("\n⑤ 3 次闸门")
    FakeSvc.script = {"has_session": "no", "login": "captcha"}
    w = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230", "p",
                            "http://202.196.169.246")
    pump(root, 6.0, lambda: w._cap_big is not None)
    w.tries = C.SELF_LOGIN_MAX
    w._draw_captcha(CAPTCHA_PNG)
    t = texts_of(w.win)
    check_true("「确定」不见了", "确定" not in t, "")
    check_true("「换一张」还在", "换一张" in t, "")
    check_true("说清了为什么停下（怕被锁号）",
               any("锁" in x for x in t), "")
    w._entry.insert(0, "a1b2")
    w._submit()                                  # 到了上限就不该再发请求
    check("到上限后不再发登录请求", w.tries, C.SELF_LOGIN_MAX)
    w.close()


def test_panel_net_fail(root):
    """网络不通 → 有中文提示，不是空白窗口。"""
    print("\n⑥ 网络不通")
    FakeSvc.script = {"has_session": "raise"}
    w = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230", "p",
                            "http://202.196.169.246")
    ok = pump(root, 6.0, lambda: "读不到数据" in " ".join(texts_of(w.win)))
    check_true("提示了「读不到数据」", ok, "")
    check_true("把原始错误也带上了（好排查）",
               any("假装连不上" in x for x in texts_of(w.win)), "")
    w.close()


def test_data_render(root):
    """数据界面：该显示的都在，不该显示的（套餐制下的余额）不在。

    ⚠️ 这里**故意不转事件循环**，直接调 `_draw_data()` 手画 ——
       要的是「给定这份数据，界面长什么样」，和线程/队列无关。
       也因此**不能在画之前 `close()`**：`close()` 会把 Toplevel 销毁掉，
       之后 `_draw_data` 里那句 `self.body.winfo_children()` 直接抛 TclError。
    """
    print("\n⑦ 数据界面")
    FakeSvc.script = {"has_session": "no"}
    w = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230", "p",
                            "http://202.196.169.246")

    w._draw_data(json.loads(json.dumps(FAKE_DATA)))
    t = texts_of(w.win)
    joined = " | ".join(t)
    for needle in ("账号状态", "正常", "校园网电信融合套餐", "2038-01-01",
                   "2026-10-06", "12.50 元", "123.50 MB", "18.02 GB",
                   "96.68 GB", "3.25 小时", "100 GB", "10.1.2.3",
                   "AA-BB-CC-DD-EE-FF", "在线设备（1 台）"):
        check_true("界面上有 %r" % needle, needle in joined, "")
    check_true("说了这是只读、不留东西",
               any("只读" in x for x in t) and any("不会保存" in x for x in t),
               "")

    # 套餐制（chargeType=0）下**不许**显示余额 —— 那会是一个恒为 0 的假数字
    d2 = json.loads(json.dumps(FAKE_DATA))
    d2["charge_type"] = "0"
    d2["balance"] = 0.0
    w._draw_data(d2)
    t2 = " | ".join(texts_of(w.win))
    check_true("套餐制下不显示「余额」", "余额" not in t2, "")
    check_true("套餐制下也不显示那个 0 元", "0.00 元" not in t2, "")

    # 没有在线设备时要说一句，不能留一片空白
    d3 = json.loads(json.dumps(FAKE_DATA))
    d3["devices"] = []
    w._draw_data(d3)
    check_true("没设备时明说「当前没有设备在线」",
               any("当前没有设备在线" in x for x in texts_of(w.win)), "")

    # 敏感字段就算被硬塞进来也不该显示（纵深防御：取数层已经过滤过一次）
    d4 = json.loads(json.dumps(FAKE_DATA))
    d4["idCardNum"] = "SENTINEL_IDCARD"
    d4["accountMobile"] = "SENTINEL_PHONE"
    w._draw_data(d4)
    t4 = " | ".join(texts_of(w.win))
    check_true("界面不显示身份证号", "SENTINEL_IDCARD" not in t4, "")
    check_true("界面不显示手机号", "SENTINEL_PHONE" not in t4, "")
    w.close()


def test_off_main_thread():
    """🔴 所有网络请求都必须在**非主线程**上发 —— 发在主线程会把界面冻住。"""
    print("\n⑨ 请求不在 Tk 主线程上发")
    check_true("所有请求都在子线程里", FakeSvc.off_main,
               "" if FakeSvc.off_main else "（有请求跑在主线程上）")
    check_true("替身真的被用上了（真对象一次都没建）", FakeSvc.made > 0,
               "（建了 %d 个）" % FakeSvc.made)


def test_job_crash_visible(root):
    """🔴 后台任务抛**非预期**异常时，必须变成界面上的提示，不许静默卡住。

    为什么单列一条：`_job_*` 只接 `SelfError`，别的异常会直接从线程里漏出去，
    表现是「界面永远停在正在登录…」。而 `self.password` 那个 bug 正是这么藏的 ——
    只跑 py_compile / AST 扫描，或者只断言「代码没崩」，都看不出来。
    """
    print("\n⑩ 后台任务崩了也要看得见")
    FakeSvc.script = {"has_session": "no", "login": "boom"}
    w = C.SelfServiceWindow(root, _tk(), *_font(root), "2507210230", "p",
                            "http://202.196.169.246")
    pump(root, 6.0, lambda: w._cap_big is not None)
    w._entry.insert(0, "a1b2")
    w._submit()
    ok = pump(root, 6.0, lambda: "内部错误" in " ".join(texts_of(w.win)))
    check_true("异常变成了界面上的提示（没静默卡住）", ok, "")
    check_true("提示里带上了异常类型，好排查",
               any("RuntimeError" in x for x in texts_of(w.win)), "")
    check_true("没有停在「正在登录…」",
               not any("正在登录" in x for x in texts_of(w.win)), "")
    w.close()


def test_main_window_chip():
    """主界面状态卡上的「上网数据」圆角片：**在、文字对、高度和邻居一模一样**。

    🔴 高度这条是重点：内容高度是 827px、上限 906px，多 1px 都可能压线。
       这个圆角片是塞进状态卡右侧的，只要它不比旁边那个高，
       整张卡的高度就一个像素都不变。所以这里直接拿两个控件比高度 ——
       哪天有人只改了一个，这条立刻红。
    """
    print("\n⑩ 主界面上的入口")
    app = C.LoginApp(smoke=True)
    try:
        check_true("郑航下「上网数据」入口存在", app.btn_self is not None, "")
        check_true("「测一下速度」也还在", app.btn_probe is not None, "")
        if app.btn_self is not None:
            check("圆角片上的字", getattr(app.btn_self, "_rb_text", None),
                  "上网数据")
            check("和「测一下速度」同高（保证内容高度不变）",
                  app.btn_self.winfo_reqheight(), app.btn_probe.winfo_reqheight())
            check("两个片是同一个父容器（同一行）",
                  str(app.btn_self.master), str(app.btn_probe.master))
            check_true("能点（挂上了左键事件）",
                       bool(app.btn_self.bind("<Button-1>")), "")

        # 🔴 界面铁律：**主按钮固定三个**。新入口是状态卡里的圆角片，
        #    不许混进那三个里去。`set_busy` 里那个元组就是这条铁律的实现
        #    （`(btn_switch, btn_save, btn_out, btn_probe)`），所以直接验行为：
        #    忙碌时那三个必须变灰、而「上网数据」**不该**跟着变
        #    （它的忙碌拦截是 `on_self_service` 自己做的）。
        main3 = (app.btn_switch, app.btn_save, app.btn_out)
        check("三个主按钮互不相同", len({id(x) for x in main3}), 3)
        check_true("新入口不是那三个主按钮之一", app.btn_self not in main3, "")
        app.set_busy(True)
        try:
            check("忙碌时三个主按钮都灰了",
                  [str(w.cget("state")) for w in main3],
                  ["disabled", "disabled", "disabled"])
            if app.btn_self is not None:
                check("忙碌时「上网数据」没被算进主按钮（自己拦截）",
                      str(app.btn_self.cget("state")), "normal")
        finally:
            app.set_busy(False)
    finally:
        app.root.destroy()


# ---------------------------------------------------------------- 收尾

def _tk():
    import tkinter as tk
    return tk


def _mark(msg):
    """排障用落点。收尾卡住时这是唯一线索（stdout 可能还没刷出来）。

    ⚠️ 别删：它的用途是**证明「脚本真的跑完了」**。退出码可疑时，
       `%TEMP%/selfwin_marks.txt` 里有没有最后一行就是判据 ——
       2026-10-12 正是靠它把「脚本没跑完」和「跑完了但退出码被信号改写」分开的。
    """
    try:
        import tempfile
        with open(os.path.join(tempfile.gettempdir(), "selfwin_marks.txt"),
                  "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _mark_reset():
    try:
        import tempfile
        os.remove(os.path.join(tempfile.gettempdir(), "selfwin_marks.txt"))
    except OSError:
        return          # 文件本来就不在，正常


def _font(root):
    import tkinter.font as tkfont
    return C._ui_font(root, tkfont)


def _no_net_guard():
    """把出网的路全掐了。**「不联网」是强制的，不是靠自觉。**"""
    real = (socket.socket.connect, socket.create_connection)

    def boom(*a, **k):
        raise AssertionError("界面自检不许联网，却试图连 %r" % (a[:1],))

    socket.socket.connect = boom
    socket.create_connection = boom
    return real


def _restore_net(real):
    socket.socket.connect, socket.create_connection = real


def main():
    print("=" * 78)
    print("SelfServiceWindow（上网数据面板）界面自检")
    print("真的建窗口 / 不联网 / 不碰真实数据")
    print("=" * 78)

    d = datasafe.sandbox(copy_real=False, tag="selfwin")
    print("沙箱数据目录：%s" % d)
    _mark_reset()

    real_svc = S.SelfService
    S.SelfService = FakeSvc
    real_net = _no_net_guard()
    rc = 0
    try:
        test_available()
        root = _tk().Tk()
        # 🔴 **不能 withdraw**：面板是 `transient(root)` 的，主窗一隐藏，
        #    Windows 就不映射子窗 —— 于是 `winfo_width()` 永远是 1，
        #    下面那条「窗口够不够宽」的断言就成了假绿（量到 1 也算过）。
        #    所以这里让它真的显示出来（很小的一块，测试期间闪一下而已）。
        root.geometry("320x140+30+30")
        root.update()
        try:
            test_panel_session_miss(root)
            test_panel_captcha_wrong(root)
            test_panel_login_fail(root)
            test_panel_tries_gate(root)
            test_panel_net_fail(root)
            test_data_render(root)
            test_job_crash_visible(root)
        finally:
            root.destroy()
        _mark("第一棵 Tk 根已销毁")
        test_off_main_thread()
        test_main_window_chip()
        _mark("主界面那棵 Tk 根已销毁")
        print("\n%s" % datasafe.assert_untouched())
    finally:
        S.SelfService = real_svc
        _restore_net(real_net)

    print()
    print("=" * 78)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        rc = 1
    else:
        print("全部通过")
    return rc


if __name__ == "__main__":
    _rc = main()
    _mark("main() 返回 rc=%s" % _rc)
    # 🔴 必须走 `proc_tree.exit_hard()`，**不能**用 `os._exit()`。
    #
    #    2026-10-12 实测踩出来的：这个脚本建过真 Tk 窗口，用 `os._exit(rc)`
    #    时进程会**卡在退出上十几秒**，受限会话等不及就把整个进程组 SIGTERM 掉
    #    —— 于是「测试全过」被报成「被信号杀死」，退出码完全不可信。
    #    当时一度误判成「建过 Toplevel 就会被杀」，其实不是：
    #    `tk.Tk()` 建几棵都没事，**只要加载过 Tcl/Tk 再 os._exit 就会这样**。
    #
    #    根因（`proc_tree.exit_hard` 的文档里早就写了）：`os._exit()` 在 Windows 上
    #    走 `ExitProcess`，它会等所有已加载 DLL 的 `DLL_PROCESS_DETACH` 跑完，
    #    而 Tcl/Tk 的清理**又慢又不可靠**（实测 11~12 秒）。
    #    `exit_hard()` 走 `TerminateProcess`，**根本不执行 DLL 卸载** —— 毫秒级。
    #    （同一条路上 `ui_shot.py` 也是这么收尾的，所以它拿得到干净的退出码。）
    proc_tree.exit_hard(_rc)
