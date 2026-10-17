# -*- coding: utf-8 -*-
"""「账号中心」面板（`campus_login.SelfServiceWindow`）的**界面自检**。

为什么非要有这个文件（2026-10-12）：
    面板写完之后只过了 `py_compile` 和 AST 扫描 —— 那两样只能证明「语法对、
    没有明显坏味道」，**证明不了「窗口真的建得起来」**。而 tkinter 有一整类
    失败是**不抛异常**的，只能靠真的建一遍才看得见：

      · 🔴 `PhotoImage` 没留引用 → 图片被 GC 掉，Label 一片空白，**不报错**
      · `round_panel(pad_x/pad_y < radius)` → 圆角被白方块糊掉，**不报错**
      · 控件被摆到窗口外 / 挤没了 → **不报错**
      · 容器（Canvas）比内容窄 → 内容被**静默裁掉**，只能量尺寸才发现

    所以这一档**真的 `tk.Tk()` 建窗口**，而且**真的把事件循环转起来**
    （`root.update()` 轮询），让它走完「画界面 → 点同步 → 验证码 → 数据 → 重画」。

🔴 2026-10-12 第二版：面板改成了**离线优先**（打开不联网、点「同步数据」才要
    验证码），所以这个文件也跟着改了 —— 现在它同时钉两件事：
      · **打开面板时一次网络都不能发**（发一次就意味着用户又要读验证码）；
      · 点同步之后整条链路（验证码 → 登录 → 合并写盘 → 重画）真的走通。

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

🔴 **不碰真实数据**：先 `datasafe.sandbox()`（面板会写本地流量历史）。
    收尾还有 `datasafe.assert_untouched()`。

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
import self_history as H       # noqa: E402
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

# 这三天的数**照抄实测那份 dump.json**（2026-10-09~11），
# 包括「上行 + 下行 == 合计」这个已经验过的性质。
FAKE_DAYS = [
    {"date": "2026-10-09", "flow": 3906.4059, "up": 683.3043,
     "down": 3223.1016, "hours": 22.0},
    {"date": "2026-10-10", "flow": 3463.7843, "up": 1186.5343,
     "down": 2277.2500, "hours": 18.0},
    {"date": "2026-10-11", "flow": 3936.1761, "up": 973.9739,
     "down": 2962.2022, "hours": 20.0},
]
# 今天：**上下行是 None**（服务端不给，实测）
FAKE_TODAY = {"date": "2026-10-12", "flow": 2428.691523,
              "up": None, "down": None, "hours": 14.002222}

# --- A 档（2026-10-16 加）：上网详单 / 停机状态 / 套餐概览 ---
# 数据**照实测那份 dump.json 的形状**（227 条里的前两条，最新在前）。
# ⚠️ 合计那几个数**故意**用实测真值：详单合计 114180.32 + 501650.28 = 615830.6 MB
#    = 601.40 GB，而账号卡的「累计」是 690876 MB = 674.68 GB —— **两个口径**，
#    测试里专门有一条断言这两个数**不相等且都在**（红线 35）。
FAKE_BILL = {
    "sessions": [
        {"start": "2026-10-14 15:05:53", "end": "2026-10-15 03:06:01",
         "seconds": 43208, "up": 207.28497, "down": 2871.914942, "fee": 0.0,
         "cause": "空闲超时", "device": "手机", "os": "Android 1.x",
         "mac": "84:9E:56:40:69:1D", "ip": "10.15.116.156"},
        {"start": "2026-10-13 08:03:56", "end": "2026-10-13 12:03:59",
         "seconds": 14403, "up": 12.5, "down": 88.25, "fee": 0.0,
         "cause": "用户主动下线", "device": "电脑", "os": "Windows",
         "mac": "84:9E:56:40:69:1D", "ip": "10.15.116.156"},
    ],
    "summary": {"total": 227, "fetched": 2, "up": 114180.32,
                "down": 501650.28, "fee": 0.0, "seconds": 9756240},
}
FAKE_STOP = {"page_type": "0", "state": "正常", "reason": "", "oper": "",
             "date": ""}
FAKE_PACKAGE = {"state": "1", "available_flow": 0.0, "balance": 0.0}

# --- A 档第二批（2026-10-16 加）：详细资料 / 扣费 / 缴费 / 可购套餐 ---
# ⚠️ 故意在 text 里留**空值**（expirationDate / proxyCtrlStopTime …）：
#    验「空字段不画成空行」—— 画一排「-」会让卡片看着像坏了。
FAKE_BASE = {
    "text": {"openingDate": "2024-09-01", "effectiveDate": "2024-09-01",
             "expirationDate": "", "firstUsetime": "2024-09-01 10:00:00",
             "latestEditDate": "2026-10-01 08:00:00",
             "lastmonthlyChargetime": "2026-10-06 00:00:00",
             "authResult": "成功", "operationAuthResult": "",
             "operationAuthTime": "", "groupChangeDate": "",
             "groupChangeFlag": "", "proxyCtrlResult": "未发现",
             "proxyCtrlStopTime": "", "shutdownReason": "", "stopDate": "",
             "flowtimeflag": ""},
    "num": {"accumulateFlow": 690876.003675, "accumulateTime": 3359.498325,
            "accumulateMoney": 0.0, "currMonthFlow": 18454.782751,
            "currMonthTime": 104.017223, "lastMonthFlow": 22000.0,
            "lastMonthTime": 130.0, "currAccumulateFlow": None,
            "remainflow": None, "remaintime": None, "loginCount": 42,
            "webLoginFailTimes": 0, "proxyCtrlTimes": 0},
    "bind": [{"clear_type": "3", "mac": "84:9E:56:40:69:1D",
              "kind": "a", "os": "Windows"}],
    "bind_count": 1,
}
FAKE_FEE = {
    "rows": [{"kind": "月租", "plan": "校园网电信融合套餐", "area": "电信",
              "money": 0.0, "month": "2026-10", "time": "2026-10-06 00:00:00"}],
    "total": 1,
    "sum": {"fix": 0.0, "month": 0.0, "bus": 0.0, "flowtime": 0.0},
}
FAKE_CHARGE = {
    "rows": [{"kind": "充值", "money": 50.0, "flag": "1",
              "time": "2026-09-01 12:00:00", "sub_code": "",
              "carrier": "电信", "plan": "", "area": "电信",
              "state": "已支付", "created": "", "handled": ""}],
    "total": 1,
}
FAKE_BUSPKG = {"rows": [], "total": 0}

# 踢人要的四个字段全在这里（`getonline.do` 的 rows）。
# ⚠️ `billingId` / `serverIp` 是**每次登录**才有的，所以界面那份（`load()`
#    带回来的）故意没有 —— `_kick` 里是**现取一次**再踢。
FAKE_TARGET = {"accountId": "2025000000",
               "accountMac": "84:9E:56:40:69:1D",
               "accountIp": "10.15.116.156",
               "onlineTime": "2026-10-12 09:37:49",
               "osInfo": "Windows", "nasId": "ZZHK-ZAX-BRAS",
               "terminalType": "a", "billingId": "bee1f27f",
               "serverIp": "10.0.1.10"}

FAKE_DATA = {
    "status": "正常",
    "plan": "校园网电信融合套餐",
    "area": "电信",
    "expire": "2038-01-01",
    "billing_day": "2026-10-06",
    "charge_type": "1",
    "balance": 12.5,
    "flow": {"today": 2428.691523, "month": 18454.782751,
             "total": 690876.003675, "limit": "不限制"},
    "time": {"today": 14.002222, "month": 104.017223,
             "total": 3355.498325, "limit": "不限制"},
    "days": FAKE_DAYS,
    "summary": {"today": FAKE_TODAY,
                "total": {"flow": 690876.003675, "hours": 3355.498325},
                "month": {"flow": 18454.782751, "hours": 104.017223},
                "limit": {"flow": "不限制", "hours": "不限制"}},
    "devices": [{"ip": "10.15.116.156", "mac": "84:9E:56:40:69:1D",
                 "since": "2026-10-12 09:37:49", "os": "Windows",
                 "nas": "ZZHK-ZAX-BRAS", "kind": "a"}],
    # --- A 档（2026-10-16 加）---
    "bill": FAKE_BILL,
    "stop": FAKE_STOP,
    "package": FAKE_PACKAGE,
    # --- A 档第二批（2026-10-16 加）---
    "base": FAKE_BASE,
    "fee": FAKE_FEE,
    "charge": FAKE_CHARGE,
    "buspkg": FAKE_BUSPKG,
    "center_error": "",
}


class FakeSvc(object):
    """`self_service.SelfService` 的替身。**一个字节都不出网。**

    行为由类属性 `script` 逐条测试现改，这样同一个替身能把
    「会话在 / 不在 / 登录成功 / 验证码错 / 密码错」几条路都走一遍。
    """

    made = 0                  # 被建了几个（用来确认真的换成了替身）
    off_main = True           # 所有请求是否都发生在**非**主线程
    last_login = None         # 最后一次 login 的实参
    calls = []                # 所有请求的名字（**用来验「打开面板时一次都没发」**）
    posts = []                # 写操作层的 `(path, payload)`（B 档，逐字段断言用）
    script = {}

    def __init__(self, base=None, timeout=None):
        FakeSvc.made += 1
        self.base = base

    def _note(self, name):
        FakeSvc.calls.append(name)
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

    def load(self, account, today=None):
        self._note("load")
        return json.loads(json.dumps(FAKE_DATA))    # 深拷贝，防被测代码改到

    def full(self, account, today=None, start=None, end=None):
        """`load()` + A 档。**被测代码现在走的是这条**（见 `_job_load`）。

        `script["full"] == "center_fail"` 时模拟「A 档没读到」——
        形状和真 `self_service.full()` 一致：三项给 None、原因进 `center_error`。
        专门用来验「A 档挂了不能假装成『这个账号没有记录』」。
        """
        self._note("full")
        d = json.loads(json.dumps(FAKE_DATA))
        if FakeSvc.script.get("full") == "center_fail":
            d["bill"] = None
            d["stop"] = None
            d["package"] = None
            # A 档第二批也一起挂掉 —— 验「四项都没读到时界面明说」
            d["base"] = None
            d["fee"] = None
            d["charge"] = None
            d["buspkg"] = None
            d["center_error"] = "详单接口抽风"
        return d

    # ------------------------------------------------------ 写操作层（B 档）

    def _post(self, path, payload=None):
        """`self_actions.SelfActions` 借它发写请求。**不联网。**

        ⚠️ 这里**不区分读写**：被测的是「点了按钮之后发了什么」，
           所以照实记 `(path, payload)`，由测试逐字段断言。
        ⚠️ `script["_post"]` 控制行为：`net` / `session` / `denied` / `boom`。
           默认 `"ok"`（返回 `errmsg`）。
        """
        self._note("_post")
        FakeSvc.posts.append((path, payload))
        mode = FakeSvc.script.get("_post", "ok")
        if mode == "net":
            raise S.SelfError("net", "假装网络断了")
        if mode == "session":
            raise S.SelfError("session", "假装会话过期")
        if mode == "denied":
            raise S.SelfError("other", "学校说：设备已离线")
        if mode == "boom":
            raise RuntimeError("假装代码写错了")
        if path == "getonline.do":
            rows = [] if FakeSvc.script.get("no_online") else [FAKE_TARGET]
            return {"errmsg": "ok", "rows": rows}
        return {"errmsg": "操作成功"}


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
    """界面上所有看得见的文字。**包括 Canvas 上画的**。

    ⚠️ 两层都得捞，少一层就是「假绿」：

      · `round_chip` / `round_button` 是 Canvas 画的，文字**不在**
        `cget("text")` 里，而是挂在 `_rb_text` 上（见 round_chip 的说明）。
        只查 `cget` 的话按钮文字会被整个漏掉。
      · 🔴 图表（柱顶数值、日期刻度、空状态提示）是 `create_text` 画的 ——
        那**根本不是控件**，`winfo_children()` 遍历不到。不捞这一层，
        「柱顶有没有标值」这类断言就会**永远为假却也不报错**
        （2026-10-12 实测：`本机还没有流量数据` 那句就是这么漏掉的）。
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
        try:
            for item in c.find_all():
                if c.type(item) == "text":
                    t2 = c.itemcget(item, "text")
                    if t2:
                        out.append(str(t2))
        except Exception:
            pass          # 不是 Canvas（find_all 会抛）—— 正常
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


def canvas_items(w):
    """界面上所有 Canvas 里画了几个图元（柱状图的柱子就是图元）。"""
    n = 0
    for c in all_widgets(w):
        try:
            n += len(c.find_all())
        except Exception:
            pass
    return n


def biggest_canvas_in(w):
    """界面里**要求的宽度最大**的那个 Canvas。

    ⚠️ 为什么要挑最大而不是按类型找：柱状图是 `tk.Canvas`，而卡片本身
       （`round_panel`）也是 `tk.Canvas`，`round_button` / `round_chip` 还是
       `tk.Canvas`。按类型找会挑到一个按钮上去，量出来当然"不超宽"——
       于是「图表被裁」这条断言变成永远通过的空转。图表的画布宽度是
       **显式设成 `self.cw`** 的，所以它一定是最大的那个。
    """
    best, bw = None, -1
    for c in all_widgets(w):
        try:
            rq = c.winfo_reqwidth()
        except Exception:
            continue
        if c.winfo_class() == "Canvas" and rq > bw:
            best, bw = c, rq
    return best


def row_value(w, name):
    """找一行 `_row`（左名右值），返回**右边的值**。找不到返回 `None`。

    🔴 为什么不直接 `"正常" in texts_of(w.win)`：那种断言几乎永远为真
       （界面上到处都有「正常」），等于没测。这里靠**结构**定位：
       `_row` 造的是一个恰好两个子 Label 的 Frame，左边是名字、右边是值。
    """
    for f in all_widgets(w):
        try:
            kids = f.winfo_children()
        except Exception:
            continue
        if len(kids) != 2:
            continue
        try:
            a, b = kids[0].cget("text"), kids[1].cget("text")
        except Exception:
            continue
        if str(a) == name:
            return str(b)
    return None


def row_fg(w, name):
    """那一行**值**的颜色（验语义色用）。找不到返回 `None`。"""
    for f in all_widgets(w):
        try:
            kids = f.winfo_children()
        except Exception:
            continue
        if len(kids) != 2:
            continue
        try:
            if str(kids[0].cget("text")) == name:
                return str(kids[1].cget("fg"))
        except Exception:
            continue
    return None


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


def open_panel(root, account="2025000000", host_w=None, host_h=None):
    """开一个面板（**不跑它自己的网络流程** —— 打开面板本来就不联网）。

    🔴 2026-10-16：面板从「独立 Toplevel」改成「主窗口里的一页」，
       所以这里也得按**真实路径**给它一块 Frame（`host=`），
       而不是让它自己建窗口。用 `pack_propagate(False)` + 显式宽度，
       把「正文有多宽」固定下来 —— 两列排版、wraplength 全是从这个宽度
       推出来的，宽度不定的话测出来的是随机的。
    """
    # 🔴 用 `place` 而不是 `pack`，而且**尺寸写死**。原因：
    #    · `pack` 会把一块块 846x760 的 host 排队塞进一个 900x800 的窗口，
    #      排到后面的一分不到空间（宽度量出来是 1），面板就退回兜底宽度 ——
    #      报出来是「没分两列」，根因却在十几个用例之前，极难查。
    #    · 多个用例会**同时在用两块面板**（比如 ⑤ 里的 w 和 w2），
    #      所以也不能「建新的就把旧的删掉」。
    #    `place` 让它们各自叠在同一位置、互不抢空间，两个问题一起没了。
    host = _tk().Frame(root, bg=C.BG)
    host.place(x=0, y=0, width=host_w or HOST_W, height=host_h or HOST_H)
    # ⚠️ 必须先把 host 真正布局出来再建面板：面板构造时就要量正文宽度
    #    （分几列 / 换行多宽都靠它）。不 update 的话量到的是 1px，
    #    面板会退回兜底宽度 —— 于是「两列排版」这一整段测的就是兜底路径，
    #    等于假绿。
    host.update_idletasks()
    return C.SelfServiceWindow(root, _tk(), *_font(root), account,
                               "not-a-real-pwd", "http://202.196.169.246",
                               host=host)


def close_panel(w):
    """关面板，并把它那块测试用 host 一起拆掉。

    ⚠️ 面板自己**管不到 host** —— host 是测试（或主窗口）给的，
       `w.close()` 只销毁面板自己那一层。不拆的话截图工具会把好几块
       面板叠在同一位置（新的盖住旧的，看不出来，但 `winfo_*` 会互相干扰）。
    """
    try:
        host = w.win.master
    except Exception:
        host = None
    w.close()
    if host is not None:
        try:
            host.destroy()
        except Exception:
            pass


# 测试用的「主窗口内容区」尺寸。**不是随便挑的**：按真实主窗口算出来的
# —— 窗口宽 `window_geometry(1707, 1067, 827)` = 1160，
# 减去 shell 两侧留白(28) + 导航栏(168) + 缝(16) = 948。
#
# ⚠️ 948 / 780 是**设计像素**，而 `place()` 要的是**物理像素** ⇒ 过 `C.dp()`。
#    开高 DPI 感知之后（2026-10-16）150% 的屏上两者差 1.5 倍：
#    「够不够分两列」的门槛是 `dp(760)=1140`，这里只给 948 的话**永远单列**，
#    ⑳ 那一段两列排版的断言就全成了假绿（跑了，但测的是单列）。
HOST_W = C.dp(948)
HOST_H = C.dp(780)


def reset_fake():
    FakeSvc.script = {}
    FakeSvc.made = 0
    FakeSvc.off_main = True
    FakeSvc.last_login = None
    FakeSvc.calls = []
    FakeSvc.posts = []


def find_button(w, text):
    """按 `_rb_text` 找按钮。`round_button` / `round_chip` 都把字挂在这个属性上
    （Canvas 的 `create_text` 内容查不出来 —— 见那个工厂函数的说明）。

    ⚠️ 同名按钮可能有好几个（比如每个绑定槽位一个「解绑」），这里返回**第一个**。
    """
    for c in all_widgets(w.win):
        if getattr(c, "_rb_text", None) == text:
            return c
    return None


def reset_history():
    """把沙箱里的本地历史清干净（每个用例开始前调）。"""
    try:
        os.remove(C.SELF_FLOW_FILE)
    except OSError:
        pass


def seed_history(account="2025000000", days=None):
    """往沙箱里的本地历史塞几天数据，返回写进去的 store。"""
    store, _ = H.load(C.SELF_FLOW_FILE)
    H.merge(store, account, days if days is not None else FAKE_DAYS[:2])
    H.save(C.SELF_FLOW_FILE, store)
    return store


def _tk():
    import tkinter as tk
    return tk


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


# ---------------------------------------------------------------- 测试

def test_available():
    """`self_service_available()`：只有郑航有自助服务。"""
    print("\n① 入口只在郑航出现")
    check("郑航（默认）", C.self_service_available({"school": "zua"}), True)
    check("嘉兴大学", C.self_service_available({"school": "zjxu"}), False)
    check("学校字段是垃圾值 → 退回郑航默认",
          C.self_service_available({"school": "???"}), True)
    check("没配学校 → 默认郑航", C.self_service_available({}), True)


def test_spacing_scale():
    """列表三档间距的顺序不能乱（见 `SelfServiceWindow` 的「列表间距规范」）。

    🔴 为什么要专门测这个（2026-10-17）：用户反馈「很多地方可读性很差，
       文字的行与行之间没有隔开」。排查下来真因**不是字号**，是间距
       散落成一堆 `dp(1)` / `dp(2)` 的字面量，而且**有的列表有分隔线、
       有的没有** —— 于是「会话的第二行」和「下一条会话的第一行」贴在一起。

    立了三档规矩之后，规矩本身也需要一道门槛：
      · 三个数字都很小，改错了**看代码看不出来**（dp(1) 和 dp(4) 长得一样）
      · 只有截图对比才明显 —— 而那正是最容易漏掉的回归
      · 顺序一乱，分组感就没了：`ITEM_GAP` 比 `GROUP_IN` 还小的话，
        两条记录之间比一条记录内部还紧，等于把「组」切错了
    """
    print("\n— 列表三档间距（同一条记录内部 / 单行记录 / 两条记录之间）")
    W = C.SelfServiceWindow
    g, r, i = W.GROUP_IN, W.ROW_GAP, W.ITEM_GAP
    print("    GROUP_IN=%d  ROW_GAP=%d  ITEM_GAP=%d" % (g, r, i))
    check_true("三档都是正数", g > 0 and r > 0 and i > 0, "(%d, %d, %d)" % (g, r, i))
    check_true("顺序：组内 < 单行 < 组间", g < r < i, "%d < %d < %d" % (g, r, i))
    # 只满足「组间 > 组内」还不够：差 1px 在 150% 屏上是 1.5 物理像素，
    # 肉眼分不出来，「两行一组」的分组感就不成立。至少要差一倍。
    check_true("组间至少是组内的两倍（否则分组感不成立）",
               i >= g * 2, "%d >= %d×2" % (i, g))


def test_open_offline_empty(root):
    """🔴 打开面板**一次网络都不发**（没历史时也一样）。"""
    print("\n② 打开面板：不联网、不要验证码（本机没历史）")
    reset_fake()
    reset_history()
    w = open_panel(root)
    pump(root, 0.6)

    check("打开面板时**一个请求都没发**", FakeSvc.calls, [])
    # 面板构造时会 new 一个取数对象（拿它的方法备用），但**一次都不许调** ——
    # 「一个请求都没发」才是这条要钉的东西（上面那行）。
    check("构造了取数对象但没用过", FakeSvc.made, 1)
    check_true("没有数据（还没同步过）", w.data is None, "")
    check_true("没有显示验证码", w._cap_big is None, "")
    t = texts_of(w.win)
    check_true("明说本机还没有数据",
               any("本机还没有流量数据" in x for x in t), "")
    check_true("引导用户点「同步数据」",
               any("同步数据" in x for x in t), "")
    check_true("图上说清了怎么开始",
               any("以后不登录也能看" in x for x in t), "")
    w.close()


def test_open_offline_with_history(root):
    """🔴 有本地历史时，打开面板**直接画图**，同样不联网。"""
    print("\n③ 打开面板：有本地历史 → 直接画图（不联网）")
    reset_fake()
    reset_history()
    seed_history()
    w = open_panel(root)
    pump(root, 0.6)

    check("打开面板时**一个请求都没发**", FakeSvc.calls, [])
    t = texts_of(w.win)
    check_true("画了「数据流量」卡片", any("数据流量" in x for x in t), "")
    check_true("有「按天」切换", any("按天" in x for x in t), "")
    check_true("有「按月」切换", any("按月" in x for x in t), "")
    check_true("柱状图上真的画了东西", canvas_items(w.win) > 5,
               "（图元 %d 个）" % canvas_items(w.win))
    check_true("显示了月份导航", any("2026 年 10 月" in x for x in t), "")
    check_true("显示了「上次同步」的说明",
               any("上次同步" in x for x in t), "")
    check_true("汇总里有「共计」", any("共计" in x for x in t), "")
    check_true("汇总里有「上行」", any("上行" in x for x in t), "")
    check_true("汇总里有「下行」", any("下行" in x for x in t), "")
    check_true("汇总里有「日均」", any("日均" in x for x in t), "")
    check_true("没有账号卡（没同步过，那些数拿不到）",
               not any("账号状态" in x for x in t), "")
    check_true("没有设备卡", not any("在线设备" in x for x in t), "")

    # 🔴 正文必须**真的放得下图表**。这条看着像废话，其实抓的是一个静默 bug：
    #    正文在滚动区里，holder 不绑 `<Configure>` 撑到 canvas 宽度的话，
    #    它会停在卡片 Canvas 的默认宽度（378px），而图表画布更宽 ——
    #    **右边一截被裁掉**（实测：6 天的柱子只画出来 4 根），Tk 一声不吭。
    #    ⚠️ 光数「柱子有几根」抓不到它（都画了，只是看不见），**必须量宽度**。
    #
    # ⚠️ 2026-10-16：分两列之后判据**不是**「正文 ≥ 图表宽」，而是
    #    「**列宽** == 卡片实际宽度 == 图表画布宽度」—— 图表现在是按列宽
    #    现算的（`self.cw`），只要这三者一致就说明画布没被裁。
    root.update()
    check_true("正文宽度够画整张图（不然柱子会被裁）",
               w.body.winfo_width() >= 400,
               "（实测 %d）" % w.body.winfo_width())
    if w.cols:
        colw = w.cols[0].winfo_width()
        # ⚠️ 给 1px 容差：`winfo_width()` 是布局算完之后**取整**的结果，
        #    和我们自己算的 `int(colw)` 可能差 1。卡死相等的话，
        #    这条断言会因为「屏幕缩放变了」这种无关原因变红。
        check_true("卡片可用宽度 = 列宽 - 卡片内边距",
                   abs(w.cw - (colw - 2 * C._CARD_PAD_X)) <= 1,
                   "（cw=%d，列宽=%d）" % (w.cw, colw))
        cv = biggest_canvas_in(w.win)
        check_true("图表画布宽度 == 卡片可用宽度（没被裁）",
                   cv is not None and cv.winfo_reqwidth() <= w.cw + 2,
                   "（画布 %s / 可用 %d）"
                   % (cv.winfo_reqwidth() if cv else "?", w.cw))
    w.close()


def test_sync_full_flow(root):
    """🔴 点「同步数据」→ 验证码 → 登录 → 合并写盘 → 重画。"""
    print("\n④ 同步：验证码 → 登录 → 数据 → 写进本地")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "no", "login": "ok"}
    w = open_panel(root)
    w._today = lambda: "2026-10-12"          # 固定「今天」，测试才确定性
    pump(root, 0.4)

    check("打开时仍然没发请求", FakeSvc.calls, [])
    w.start_sync()
    ok = pump(root, 6.0, lambda: w._cap_big is not None)
    check_true("点同步后才去取验证码", ok, "")
    check("确实发了取验证码的请求", "fetch_captcha" in FakeSvc.calls, True)
    check("同步中按钮被禁用（防连点把 3 次闸门耗光）",
          str(w.btn_sync.cget("state")), "disabled")

    # 验证码图必须真的活着
    check("原图尺寸", (w._cap_small.width(), w._cap_small.height()),
          (CAPTCHA_W, CAPTCHA_H))
    # ⚠️ 倍数用 `C.CAPTCHA_ZOOM` 而**不是**写死的 4 —— 它走 `dp()`
    #    （2026-10-16 加高 DPI 之后，150% 屏上是 6 倍 = 780x240）。
    #    写死 4 的话这条会红，而它红得「有道理」：界面确实变了。
    check("放大后的尺寸", (w._cap_big.width(), w._cap_big.height()),
          (CAPTCHA_W * C.CAPTCHA_ZOOM, CAPTCHA_H * C.CAPTCHA_ZOOM))
    gc.collect()
    root.update()
    check("GC 之后 Tk 里的图片仍在（没被回收）",
          tk_image_size(root, str(w._cap_big)),
          (CAPTCHA_W * C.CAPTCHA_ZOOM, CAPTCHA_H * C.CAPTCHA_ZOOM))

    # 输入并提交
    w._entry.insert(0, "a1b2")
    w._submit()
    ok = pump(root, 6.0, lambda: "账号状态" in texts_of(w.win))
    check_true("同步完切到了数据界面", ok, "")
    check("发给学校的验证码就是刚输入的那个",
          FakeSvc.last_login[2] if FakeSvc.last_login else None, "a1b2")
    check("登录用的账号", FakeSvc.last_login[0] if FakeSvc.last_login else None,
          "2025000000")
    check_true("同步完按钮恢复可用", str(w.btn_sync.cget("state")) == "normal", "")

    # 🔴 数据必须真的写进了本地
    check_true("本地历史文件写出来了", os.path.isfile(C.SELF_FLOW_FILE), "")
    store, why = H.load(C.SELF_FLOW_FILE)
    check("读回来没有报错", why, "")
    got = H.days_of(store, "2025000000")
    check("本地存了 4 天（3 天明细 + 今天）", len(got), 4)
    check("日期都对", [d["date"] for d in got],
          ["2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"])
    check("今天的上下行是 None（服务端不给）", got[-1]["up"], None)
    check_true("记下了「上次同步」时间", bool(H.updated_at(store, "2025000000")), "")
    check_true("界面上说了新增/更新了几天",
               "新增 4 天" in getattr(w, "note", ""), getattr(w, "note", ""))

    # 再同步一次（同样的数据）⇒ 不该重复「新增」
    FakeSvc.script = {"has_session": "yes", "login": "ok"}
    w.start_sync()
    ok = pump(root, 6.0, lambda: "新增 0 天" in getattr(w, "note", ""))
    check_true("同样的数据再同步一次 ⇒ 新增 0 天", ok, getattr(w, "note", ""))
    check("第二次走了「会话还在」这条路（没再要验证码）",
          "has_session" in FakeSvc.calls, True)
    check("第二次没有登录", FakeSvc.last_login[2], "a1b2")   # 还是第一次那个
    w.close()


def test_sync_captcha_wrong(root):
    """验证码错 → 允许重试（换一张），且**新图必须真的换掉旧的**。"""
    print("\n⑤ 验证码错：可以重试，且不能拿旧图糊弄")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "no", "login": "captcha"}
    w = open_panel(root)
    w._today = lambda: "2026-10-12"
    w.start_sync()
    pump(root, 6.0, lambda: w._cap_big is not None)
    w._entry.insert(0, "zzzz")
    w._submit()
    ok = pump(root, 6.0, lambda: "验证码不对" in " ".join(texts_of(w.win)))
    check_true("提示了「验证码不对」", ok, "")
    check("登录只发了 1 次（验证码错也算 1 次，防锁号）", w.tries, 1)
    check_true("还能继续试（tries 没到上限）", w.tries < C.SELF_LOGIN_MAX, "")
    check_true("「确定」按钮还在（允许重试）", "确定" in texts_of(w.win), "")
    check_true("有「换一张」", "换一张" in texts_of(w.win), "")
    check_true("有「取消」（能退回看历史）", "取消" in texts_of(w.win), "")

    # 🔴 换新图失败时**不许**继续显示旧图，而且**必须照样说清为什么失败**。
    #    这条路只有走 `_job_login` 才到得了：验证码错 → 顺手取新图 → 新图没取到
    #    → png 是 None。原来的写法会「提示整个消失」（note 被当成标志位用）。
    reset_fake()
    FakeSvc.script = {"has_session": "no", "login": "captcha"}
    w2 = open_panel(root)
    w2._today = lambda: "2026-10-12"
    w2.start_sync()
    pump(root, 6.0, lambda: w2._cap_big is not None)
    old2 = str(w2._cap_big)          # 先正常拿到一张，后面才知道「旧的是哪张」
    FakeSvc.script["fetch_captcha"] = "raise"      # 从现在起取不到新图
    w2._entry.insert(0, "zzzz")
    w2._submit()
    ok = pump(root, 6.0, lambda: "新的没取到" in " ".join(texts_of(w2.win)))
    check_true("取不到新图时明说「没取到」", ok, "")
    check_true("取不到新图时不留旧图引用", w2._cap_big is None,
               "（旧引用 = %s）" % ("还在" if w2._cap_big else "已清"))
    check_true("旧图在界面上也确实没了", len(images_of(w2.win)) == 0, "")
    check_true("旧图名已经不在任何控件上",
               all(n != old2 for _c, n in images_of(w2.win)), "")
    check_true("提示没被吞掉（这条原来就是 bug）",
               any("验证码不对" in x for x in texts_of(w2.win)), "")
    w2.close()

    # 取消能退回历史视图
    w._cancel()
    pump(root, 0.4)
    check_true("「取消」之后回到历史视图（不再是验证码界面）",
               "确定" not in texts_of(w.win), "")
    check_true("取消之后同步按钮恢复可用",
               str(w.btn_sync.cget("state")) == "normal", "")
    w.close()


def test_sync_login_fail(root):
    """密码错 → **不许重试**，把「确定」收起来。"""
    print("\n⑥ 密码错：不许重试")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "no", "login": "login"}
    w = open_panel(root)
    w.start_sync()
    pump(root, 6.0, lambda: w._cap_big is not None)
    w._entry.insert(0, "a1b2")
    w._submit()
    ok = pump(root, 6.0, lambda: "登录不上自助服务" in " ".join(texts_of(w.win)))
    check_true("提示了「登录不上自助服务」", ok, "")
    check_true("把「确定」按钮收起来了（不给重试）",
               "确定" not in texts_of(w.win), "")
    check_true("说清了原因不是验证码",
               any("这不是验证码的问题" in x for x in texts_of(w.win)), "")
    check_true("有「返回」", "返回" in texts_of(w.win), "")
    w.close()


def test_sync_tries_gate(root):
    """3 次闸门：到了上限只剩「换一张」。"""
    print("\n⑦ 3 次闸门")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "no", "login": "captcha"}
    w = open_panel(root)
    w.start_sync()
    pump(root, 6.0, lambda: w._cap_big is not None)
    w.tries = C.SELF_LOGIN_MAX
    w._draw_captcha(CAPTCHA_PNG)
    t = texts_of(w.win)
    check_true("「确定」不见了", "确定" not in t, "")
    check_true("「换一张」还在", "换一张" in t, "")
    check_true("说清了为什么停下（怕被锁号）", any("锁" in x for x in t), "")
    w._entry.insert(0, "a1b2")
    w._submit()                                  # 到了上限就不该再发请求
    check("到上限后不再发登录请求", w.tries, C.SELF_LOGIN_MAX)
    w.close()


def test_sync_net_fail(root):
    """网络不通 → 有中文提示，而且**本机历史不受影响**。"""
    print("\n⑧ 网络不通")
    reset_fake()
    reset_history()
    seed_history()
    FakeSvc.script = {"has_session": "raise"}
    w = open_panel(root)
    w.start_sync()
    ok = pump(root, 6.0, lambda: "读不到数据" in " ".join(texts_of(w.win)))
    check_true("提示了「读不到数据」", ok, "")
    check_true("把原始错误也带上了（好排查）",
               any("假装连不上" in x for x in texts_of(w.win)), "")
    check_true("说了本机历史不受影响",
               any("本机已经存下来的流量历史不受影响" in x
                   for x in texts_of(w.win)), "")
    check_true("有「返回」能回到历史视图", "返回" in texts_of(w.win), "")
    w._cancel()
    pump(root, 0.4)
    check_true("返回之后又看得到历史了",
               any("数据流量" in x for x in texts_of(w.win)), "")
    w.close()


def test_job_crash_visible(root):
    """🔴 后台任务抛**非预期**异常时，必须变成界面上的提示，不许静默卡住。

    为什么单列一条：`_job_*` 只接 `SelfError`，别的异常会直接从线程里漏出去，
    表现是「界面永远停在正在登录…」。而 `self.password` 那个 bug 正是这么藏的 ——
    只跑 py_compile / AST 扫描，或者只断言「代码没崩」，都看不出来。
    """
    print("\n⑨ 后台任务崩了也要看得见")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "no", "login": "boom"}
    w = open_panel(root)
    w.start_sync()
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


def test_clear_history(root):
    """清历史：**只清当前账号**，别的账号不动。"""
    print("\n⑩ 清掉本机历史（只清当前账号）")
    reset_fake()
    reset_history()
    store, _ = H.load(C.SELF_FLOW_FILE)
    H.merge(store, "2025000000", FAKE_DAYS)
    H.merge(store, "2026999999", [{"date": "2026-10-01", "flow": 1.0}])
    H.save(C.SELF_FLOW_FILE, store)

    w = open_panel(root)
    w.ask = lambda *a: True                  # 别弹模态框（会把自动化挂住）
    pump(root, 0.4)
    w.clear_history()
    pump(root, 0.4)

    store2, _ = H.load(C.SELF_FLOW_FILE)
    check("当前账号的历史清掉了", H.days_of(store2, "2025000000"), [])
    check("**别的账号**的历史还在", len(H.days_of(store2, "2026999999")), 1)
    check_true("界面提示清完了", "已清掉" in getattr(w, "note", ""),
               getattr(w, "note", ""))

    # 用户点「否」时不能清
    store3, _ = H.load(C.SELF_FLOW_FILE)
    H.merge(store3, "2025000000", FAKE_DAYS)
    H.save(C.SELF_FLOW_FILE, store3)
    w2 = open_panel(root)
    w2.ask = lambda *a: False
    pump(root, 0.4)
    w2.clear_history()
    store4, _ = H.load(C.SELF_FLOW_FILE)
    check("点「否」就不清", len(H.days_of(store4, "2025000000")), 3)

    # 没有历史时不该弹确认框
    w3 = open_panel(root, account="2026000000")
    called = []
    w3.ask = lambda *a: called.append(1) or True
    pump(root, 0.4)
    w3.clear_history()
    check("没历史时连确认框都不弹", called, [])
    check_true("并明说没有历史", "没有" in getattr(w3, "note", ""), getattr(w3, "note", ""))
    for x in (w, w2, w3):
        x.close()


def test_chart_views(root):
    """图表：按天/按月切换 + 月份导航。"""
    print("\n⑪ 图表：按天 / 按月 / 月份导航")
    reset_fake()
    reset_history()
    store, _ = H.load(C.SELF_FLOW_FILE)
    H.merge(store, "2025000000", [
        {"date": "2026-09-28", "flow": 3762.25, "up": 393.57, "down": 3368.68, "hours": 24.0},
        {"date": "2026-09-29", "flow": 32812.71, "up": 6170.45, "down": 26642.26, "hours": 16.0},
    ] + FAKE_DAYS)
    H.save(C.SELF_FLOW_FILE, store)

    w = open_panel(root)
    pump(root, 0.6)
    t = texts_of(w.win)
    check_true("默认是「按天」", w.view, "")
    check("默认看的是最近那个月", w.ym, "2026-10")
    check_true("柱顶标了值（有 G 结尾的短格式）",
               any(x.endswith("G") for x in t), "")
    check_true("有月份导航的左右箭头",
               "‹" in t or "›" in t, "")
    check_true("柱顶标签里有「日均」那条线",
               any("日均" in x for x in t), "")

    # 切到 9 月
    w.shift_month(-1)
    pump(root, 0.4)
    check("往回一个月", w.ym, "2026-09")
    check_true("界面上写的是 9 月",
               any("2026 年 9 月" in x for x in texts_of(w.win)), "")
    # 9 月只有两天
    check("9 月只有 2 根柱子", len(w._series(H.days_of(w.store, w.account))), 2)

    # 切到按月
    w.set_view("month")
    pump(root, 0.4)
    check("切到按月", w.view, "month")
    check("按月有 2 个月", len(w._series(H.days_of(w.store, w.account))), 2)
    check_true("按月视图下没有月份导航（按月本来就不用选月）",
               not any("2026 年 10 月" in x for x in texts_of(w.win)), "")
    check_true("按月时柱子的标签是「9月」「10月」",
               any("9月" in x for x in texts_of(w.win))
               and any("10月" in x for x in texts_of(w.win)), "")
    # 切回来
    w.set_view("day")
    pump(root, 0.4)
    check("切回按天", w.view, "day")
    w.close()


def test_devices_layout(root):
    """在线设备：**设备类型优先**，其次是上线时间，其余写小字。"""
    print("\n⑫ 在线设备：设备类型优先")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "yes"}
    w = open_panel(root)
    w._today = lambda: "2026-10-12"
    w.start_sync()
    ok = pump(root, 6.0, lambda: "在线设备" in " ".join(texts_of(w.win)))
    check_true("同步之后出现了设备卡", ok, "")
    t = texts_of(w.win)
    check_true("有设备类型（Windows）", "Windows" in t, "")
    check_true("有上线时间", any("上线" in x for x in t), "")
    check_true("IP / MAC 也在（小字那行）",
               any("10.15.116.156" in x for x in t), "")
    check_true("设备卡标题带了台数",
               any("在线设备（1 台）" in x for x in t), "")

    # 设备类型为空时**不许猜**（terminalType 是 "a" 这种代码，含义没验证过）
    FakeSvc.script = {"has_session": "yes"}
    saved = json.loads(json.dumps(FAKE_DATA))
    saved["devices"][0]["os"] = ""
    # ⚠️ 猴补的是 `full`（不是 `load`）—— 2026-10-16 起 `_job_load` 走的是
    #    `self_service.full()`。补错方法的话这个替身根本不生效，
    #    而断言仍然会「绿」得莫名其妙（这里当时就是漏改，靠 ⑫ 报红才发现）。
    orig_full = FakeSvc.full
    FakeSvc.full = lambda self, account, today=None, start=None, end=None: \
        json.loads(json.dumps(saved))
    w2 = open_panel(root)
    w2._today = lambda: "2026-10-12"
    w2.start_sync()
    pump(root, 6.0, lambda: "在线设备" in " ".join(texts_of(w2.win)))
    t2 = texts_of(w2.win)
    check_true("设备类型空 → 写「未知设备」，不拿 terminalType 猜",
               "未知设备" in t2, "")
    check_true("也没有把那个代码 a 当成设备类型显示",
               not any(x.strip() == "a" for x in t2), "")
    FakeSvc.full = orig_full
    w.close()
    w2.close()


def test_off_main_thread():
    """🔴 所有网络请求都必须在**非主线程**上发 —— 发在主线程会把界面冻住。"""
    print("\n⑬ 请求不在 Tk 主线程上发")
    check_true("所有请求都在子线程里", FakeSvc.off_main,
               "" if FakeSvc.off_main else "（有请求跑在主线程上）")
    check_true("替身真的被用上了（真对象一次都没建）", FakeSvc.made > 0,
               "（建了 %d 个）" % FakeSvc.made)


def text_widget_in(page):
    """某一页里那个 `tk.Text`（「使用说明」的正文就是它）。没有就返回 None。"""
    for c in all_widgets(page):
        if c.winfo_class() == "Text":
            return c
    return None


def cards_in(col):
    """某一列里装了几张卡。每张卡是 `round_panel` 返回的一个 Canvas。"""
    return [c for c in col.winfo_children() if c.winfo_class() == "Canvas"]


def test_two_columns(root):
    """⑳ 横向排版：**宽了分两列、窄了自动退回单列**。

    🔴 为什么两种宽度都要测：只测宽的那一遍会漏掉「窄屏还硬分两列」——
       那时每列只有三四百像素，说明文字折成一堆短行、图表细成毛刺，
       而且**不报任何错**，只是难看。反过来只测窄的会漏掉「宽了没分列」，
       用户抱怨的正是这个（「不用这么竖」）。

    🔴 还要验**页脚说明在两列下面**。混用 `pack(side="left")` 和默认的
       `pack(side="top")` 时，Tk 会把后来的那个排到**右边剩下的区域**里去，
       而不是下面 —— 而且不抛异常，只是位置不对（得靠量 y 坐标才发现）。
    """
    print("\n⑳ 横向排版：宽了分两列、窄了回单列")

    def build(host_w):
        reset_fake()
        reset_history()
        FakeSvc.script = {"has_session": "yes", "login": "ok"}
        w = open_panel(root, host_w=host_w)
        pump(root, 0.4)
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.8)
        return w

    # ---------- 宽：两列 ----------
    w = build(HOST_W)
    try:
        check_true("正文宽度 >= 门槛（%.0f）" % w.MIN_2COL_W,
                   w._body_w() >= w.MIN_2COL_W, "（实测 %d）" % w._body_w())
        check("分成了 2 列", len(w.cols), 2)
        check_true("多了一层「放两列」的容器", w.colrow is not None, "")
        check("卡片可用宽度 = (正文 - 列缝)/2 - 卡片内边距", w.cw,
              int((w._body_w() - w.COL_GAP) / 2.0) - 2 * C._CARD_PAD_X)
        check("左列 3 张卡（账号 / 图表 / 资料）",
              len(cards_in(w.cols[0])), 3)
        check("右列 5 张卡（设备 / 详单 / 费用 / 套餐 / 链接）",
              len(cards_in(w.cols[1])), 5)
        # 两列高度别差太多（差太多的话短的那列底下会空一大块）
        h0, h1 = w.cols[0].winfo_reqheight(), w.cols[1].winfo_reqheight()
        ratio = float(max(h0, h1)) / max(1, min(h0, h1))
        check_true("两列高度大致相当（比值 %.2f <= 1.60）" % ratio,
                   ratio <= 1.60, "（左 %d / 右 %d）" % (h0, h1))
        # 🔴 页脚必须在两列**下面**，不是在右边那块空地里
        root.update()
        foot = w.body.winfo_children()[-1]
        check_true("页脚说明排在两列下方（不是被挤到右半边）",
                   foot.winfo_y() >= w.colrow.winfo_reqheight() - 4,
                   "（页脚 y=%d，两列高=%d）"
                   % (foot.winfo_y(), w.colrow.winfo_reqheight()))
    finally:
        close_panel(w)

    # ---------- 窄：单列 ----------
    w = build(600)
    try:
        check_true("正文宽度 < 门槛", w._body_w() < w.MIN_2COL_W,
                   "（实测 %d）" % w._body_w())
        check("没分列（退回单列）", len(w.cols), 0)
        check("卡片直接铺在正文里（8 张卡都在同一列）",
              len(cards_in(w.body)), 8)
        check("单列时卡片可用宽度 = 正文宽 - 卡片内边距", w.cw,
              w._body_w() - 2 * C._CARD_PAD_X)
    finally:
        close_panel(w)


def test_main_window_pages():
    """主界面：**左侧导航 + 右侧内容**，一个窗口里进出所有页。

    🔴 这一段的由来（2026-10-16）。用户原话：
        「每加一个新功能就要加一个窗口，那用户同时看好几个功能
          不就全屏都是窗口了」。
       所以面板从独立 Toplevel 改成了主窗口里的一页。这里钉三件事：

      1. **导航项真的在、点得动**（挂的是 `_rb_text` + `<Button-1>` +
         `invoke`，和 `round_chip` / `round_button` 同一套 —— 测试不用记两套写法）。
      2. **点一下就真的换页**（`self.page` 变了、别的页 `pack_forget` 了）。
         光验「控件存在」是不够的：`show_page` 里写错一个 key，
         表现就是「点了没反应」，而且不报任何错。
      3. **切页不销毁别的页**（账号中心里挂着内存会话，销毁了要重新读验证码）。
    """
    print("\n⑭ 主界面：左侧导航 + 一个窗口多个页面")
    # 沙箱里塞一个假账号：不给的话 `_ensure_center` 会走「没填账号密码」那条
    # 提示，而它弹的是**真模态框** —— 无人值守的测试会一直等在那里。
    C.save_config({"userId": "2025000000", "passwd": "x"})
    app = C.LoginApp(smoke=True)
    # 🔴 兜底：任何模态框都自动「否」。**不能省** —— 弹一个框就挂住整档测试，
    #    报出来却是「超时」，和真实原因差十万八千里。
    app.messagebox = C._SilentBox()
    try:
        check("页面清单", [k for k, _l, _i in app.PAGES],
              ["settings", "center", "help"])
        check_true("郑航下有「账号中心」这一项", "center" in app.nav_btns, "")
        check_true("「设置」「使用说明」也在",
                   "settings" in app.nav_btns and "help" in app.nav_btns, "")
        check("「测一下速度」还在（删的是状态卡上那个重复入口）",
              app.btn_probe is not None, True)
        check("状态卡上不再有重复的「账号中心」圆角片",
              getattr(app, "btn_self", None), None)

        # --- 导航项长什么样 ---
        setb = app.nav_btns["settings"]
        check("导航项文字", getattr(setb, "_rb_text", None), "设置")
        check_true("能点（挂了左键事件）", bool(setb.bind("<Button-1>")), "")
        check_true("有 invoke()（和 round_chip/round_button 对齐）",
                   callable(getattr(setb, "invoke", None)), "")

        # --- 切页 ---
        check("一开始在设置页", app.page, "settings")
        check("设置页在布局里（`winfo_manager` 非空 = 真的 pack 上了）",
              app.page_settings.winfo_manager(), "pack")
        app.nav_btns["help"].invoke()
        check("点「使用说明」→ 切过去了", app.page, "help")
        check("说明页在布局里", app.page_help.winfo_manager(), "pack")
        check("设置页被收起来了（不是叠在上面）",
              app.page_settings.winfo_manager(), "")
        tb = text_widget_in(app.page_help)
        check_true("说明正文真的在（不是空页）",
                   tb is not None and "校园网" in tb.get("1.0", "end"), "")
        check("高亮跟着走：说明亮着", 
              [k for k, b in app.nav_btns.items() if b.is_active()], ["help"])
        app.nav_btns["settings"].invoke()
        check("点回「设置」→ 回来了", app.page, "settings")
        check("高亮也回来了",
              [k for k, b in app.nav_btns.items() if b.is_active()], ["settings"])

        # --- 不销毁别的页（来回切，还是同一块 Frame）---
        first = app.page_help
        app.nav_btns["help"].invoke()
        app.nav_btns["settings"].invoke()
        app.nav_btns["help"].invoke()
        check("说明页是**同一块 Frame**（切页只 pack_forget，不重建）",
              app.page_help is first, True)
        app.nav_btns["settings"].invoke()

        # --- 🔴 点「账号中心」真的建得起来（嵌入模式那条路）---
        app.nav_btns["center"].invoke()
        check("切到账号中心页", app.page, "center")
        check_true("面板真的建出来了", app.panel is not None, "")
        check_true("面板嵌在那一页里（`host` 就是 page_center）",
                   app.panel is not None
                   and app.panel.win.master is app.page_center, "")
        check_true("嵌进主窗口之后**不再建 Toplevel**",
                   app.panel is not None and app.panel.embedded, "")
        if app.panel is not None:
            check("导航栏还在布局里（没被内容盖掉／顶掉）",
                  app.nav.winfo_manager(), "pack")
            check("面板看的账号 = 界面上那个账号",
                  app.panel.account, "2025000000")
        # ⚠️ 面板起来之后 `after(80, _pump)` 就在跑了，销毁前先关掉它，
        #    否则 `root.destroy()` 之后那一拍还会往死控件上摸。
        app.nav_btns["settings"].invoke()
        app._drop_center()

        # --- 铁律：主按钮还是三个 ---
        main3 = (app.btn_switch, app.btn_save, app.btn_out)
        check("三个主按钮互不相同", len({id(x) for x in main3}), 3)
        app.set_busy(True)
        try:
            check("忙碌时三个主按钮都灰了",
                  [str(w.cget("state")) for w in main3],
                  ["disabled", "disabled", "disabled"])
        finally:
            app.set_busy(False)
    finally:
        app.root.destroy()


def test_center_cards(root):
    """⑮ 账号中心：停机状态 / 上网详单（含口径区分）+ A 档失败要说出来。"""
    print("\n⑮ 账号中心：停机状态 / 上网详单")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "yes", "login": "ok"}
    w = open_panel(root)
    pump(root, 0.4)
    try:
        # ① 直接塞数据 + 重画（登录那条流程 ④ 已经验过，不重复走）
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.5)

        # --- 停机状态（并进账号卡）---
        check("账号卡里有「停机状态」行", row_value(w.win, "停机状态"), "正常")
        check("「正常」用绿色（语义色）", row_fg(w.win, "停机状态"), C.OK)

        # --- 上网详单卡 ---
        check("区间（最旧 ~ 最新）", row_value(w.win, "区间"), "2026-10-13 ~ 2026-10-14")
        check("会话数用服务端的 total", row_value(w.win, "会话数"), "227 次")
        check("详单合计时长（秒 → 人话）",
              row_value(w.win, "详单合计时长"), "2710 小时 4 分")
        check("详单合计流量（上行+下行）",
              row_value(w.win, "详单合计流量"), "601.40 GB")

        # 🔴 红线 35：两个口径的「累计」同屏，必须**都写清是哪个**
        check("账号卡那边的口径还在（学校记录：累计 674.68 GB）",
              row_value(w.win, "余额"), "12.50 元")
        blob = "\n".join(texts_of(w.win))
        check_true("两个数都在界面上（601.40 GB 和 674.68 GB）",
                   "601.40 GB" in blob and "674.68 GB" in blob,
                   "详单口径 vs 账号口径")
        check_true("两个数**不相等**（不是同一口径重复显示）",
                   "601.40 GB" != "674.68 GB", "")
        check_true("🔴 界面上解释了「为什么两个数不一样」",
                   "不是同一个数" in blob, "")

        # --- 会话明细（最新在前）---
        check_true("第一条会话的起止", "10-14 15:05 → 10-15 03:06" in blob, "")
        check_true("第一条的时长", "12 小时 0 分" in blob, "")
        check_true("🔴 上行/下行**分开**写（用户要看上下行）",
                   "↑ 207.28 MB" in blob and "↓ 2.80 GB" in blob, "")
        check_true("第一条的断开原因", "空闲超时" in blob, "")
        check_true("第二条会话也在", "10-13 08:03 → 10-13 12:03" in blob, "")
        check_true("第二条的原因", "用户主动下线" in blob, "")

        # --- 会话数超过 BILL_SHOW 时要有说明 ---
        w.BILL_SHOW = 1
        w._render()
        pump(root, 0.3)
        blob2 = "\n".join(texts_of(w.win))
        check_true("超过上限时写「只列最近 N 次」",
                   "这里只列最近 1 次" in blob2, "")
        check_true("超上限时第二条不画了", "用户主动下线" not in blob2, "")
        w.BILL_SHOW = 10

        # --- 套餐可用流量：0 时**不显示**（0 会被读成「没流量了」，和事实相反）---
        check("可用流量 0 ⇒ 不显示这一行", row_value(w.win, "套餐可用流量"), None)
        d = json.loads(json.dumps(FAKE_DATA))
        d["package"]["available_flow"] = 512.0
        w.data = d
        w._render()
        pump(root, 0.3)
        check("可用流量 > 0 ⇒ 显示", row_value(w.win, "套餐可用流量"), "512.00 MB")

        # --- 停机中要用警示色 ---
        d = json.loads(json.dumps(FAKE_DATA))
        d["stop"] = {"page_type": "1", "state": "停机中", "reason": "欠费",
                     "oper": "admin", "date": "2026-10-15"}
        w.data = d
        w._render()
        pump(root, 0.3)
        check("停机中", row_value(w.win, "停机状态"), "停机中")
        check("停机中用红色", row_fg(w.win, "停机状态"), C.DANGER)
        check_true("停机原因写出来了", "原因：欠费" in "\n".join(texts_of(w.win)), "")

        # ② 🔴 A 档没读到：必须**说出来**，不能假装「这个账号没有记录」
        d2 = json.loads(json.dumps(FAKE_DATA))
        d2["bill"] = None
        d2["stop"] = None
        d2["package"] = None
        d2["center_error"] = "详单接口抽风"
        w.data = d2
        w._render()
        pump(root, 0.5)
        blob3 = "\n".join(texts_of(w.win))
        check_true("🔴 明说「这次没读到详单」", "这次没读到详单" in blob3, "")
        check_true("🔴 不能说成「还没有上网记录」",
                   "还没有上网记录" not in blob3, "")
        check_true("页脚写出原因", "详单接口抽风" in blob3, "")
        check_true("🔴 页脚说清「不代表没有」", "不代表「没有」" in blob3, "")
        check("🔴 停机没读到 ⇒ 不画那一行（不知道 ≠ 正常）",
              row_value(w.win, "停机状态"), None)
        # 主数据必须照常
        check("主数据照常：账号状态", row_value(w.win, "账号状态"), "正常")
        check_true("主数据照常：设备卡还在",
                   "在线设备" in "\n".join(texts_of(w.win)), "")

        # ③ 走一遍真流程：同步后 data 里带着 A 档（验 `_job_load` 用的是 full）
        reset_fake()
        FakeSvc.script = {"has_session": "yes", "login": "ok"}
        w2 = open_panel(root)
        try:
            pump(root, 0.4)
            w2.start_sync()
            ok = pump(root, 6.0, lambda: w2.data is not None)
            check_true("同步后拿到数据", ok, "")
            check_true("🔴 同步走的是 full()（不是 load）", "full" in FakeSvc.calls,
                       str(sorted(set(FakeSvc.calls))))
            check("同步后详单卡也画出来了",
                  row_value(w2.win, "会话数"), "227 次")
        finally:
            w2.close()
    finally:
        w.close()


# ---------------------------------------------------------------- A 档第二批

def test_center_cards2(root):
    """⑯ 账号资料 / 费用记录 / 可购套餐（A 档第二批，2026-10-16）。

    ⚠️ 直接塞 `w.data` 再 `_render()` —— 同步流程由 ④ 和 ⑮③ 覆盖，
       这条只管「数据到了界面该长什么样」。
    """
    print("\n⑯ 账号资料 / 费用记录 / 可购套餐")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "yes", "login": "ok"}
    w = open_panel(root)
    pump(root, 0.4)
    try:
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.5)
        blob = "\n".join(texts_of(w.win))

        # --- 三张卡的标题 ---
        for title in ("账号资料", "费用记录", "可购套餐"):
            check_true("有「%s」卡" % title, title in blob, "")

        # --- 详细资料：有值的字段要画出来 ---
        check("开通日期", row_value(w.win, "开通日期"), "2024-09-01")
        check("认证结果", row_value(w.win, "认证结果"), "成功")
        check("认证结果成功 ⇒ 绿色", row_fg(w.win, "认证结果"), C.OK)
        check("登录次数", row_value(w.win, "登录次数"), "42 次")
        # 流量 + 时长同一行（单位由 fmt_* 各自处理）
        check("累计（流量 + 时长同一行）", row_value(w.win, "累计"),
              "674.68 GB　时长 3359.50 小时")
        check("本月", row_value(w.win, "本月"),
              "18.02 GB　时长 104.02 小时")

        # 🔴 空字段**不许**画成空行（画一排「-」会让卡片看着像坏了）
        check("空字段不画成行（expirationDate 是空的）",
              row_value(w.win, "到期日期"), None)
        check("空字段不画成行（proxyCtrlStopTime 是空的）",
              row_value(w.win, "上次处理时间"), None)

        # --- 代理 / 共享检测：有值就画 + 必须带解释 ---
        check("代理检测结果", row_value(w.win, "检测结果"), "未发现")
        check("代理检测次数", row_value(w.win, "检测次数"), "0 次")
        check_true("🔴 代理检测有一句解释（学校网页上不会解释这个数是干嘛的）",
                   "多人共享" in blob, "")

        # --- MAC 绑定槽位 ---
        check_true("列出了绑定槽位数", "已绑定的设备（1 个槽位）" in blob, "")
        check_true("槽位的 MAC 画出来了", "84:9E:56:40:69:1D" in blob, "")
        # 🔴 只许 MAC + `os`，**不许**带 `kind`（`terminalType` 实测是 "a" 这种代码，
        #    含义没验证过 —— 显示它等于编）。第一版这里真写着「Windows　a」，
        #    是**看截图**看出来的，不是断言看出来的 ⇒ 补一条钉住。
        mac_line = [x for x in texts_of(w.win)
                    if x.startswith("84:9E:56:40:69:1D")]
        check("绑定槽位那行只带 MAC + 系统，不带 kind 代码",
              mac_line, ["84:9E:56:40:69:1D　Windows"])

        # --- 费用记录：两个**方向相反**的集合必须分得清 ---
        check_true("有「学校扣你的」小标题",
                   "学校扣你的" in blob, "")
        check_true("有「你交给学校的」小标题",
                   "你交给学校的" in blob, "")
        check("四类汇总：月租（0 也要画，0 是有效信息）",
              row_value(w.win, "月租"), "0.00 元")
        check("四类汇总：固定费", row_value(w.win, "固定费"), "0.00 元")
        check_true("扣费明细画出来了（含类型）", "月租" in blob, "")
        check_true("缴费明细画出来了（含状态）", "已支付" in blob, "")

        # --- 可购套餐：空列表**不许**说成「你没有」 ---
        check_true("空套餐说成「学校返回的是空的」",
                   "学校返回的可购套餐列表是空的" in blob, "")
        check_true("🔴 空套餐没有说成「你没有可购套餐」",
                   "你没有可购套餐" not in blob, "")
        check_true("空套餐给出了去处（去学校网页办）",
                   "本软件不代办" in blob, "")

        # --- 明细超过上限要有说明 ---
        d = json.loads(json.dumps(FAKE_DATA))
        d["fee"]["rows"] = d["fee"]["rows"] * 8      # 8 条，上限 6
        w.data = d
        w._render()
        pump(root, 0.3)
        check_true("扣费超上限时写「只列最近 N 条」",
                   "这里只列最近 6 条" in "\n".join(texts_of(w.win)), "")

        # ② 🔴 四项都没读到：必须**明说**，不能假装「没有记录」
        d2 = json.loads(json.dumps(FAKE_DATA))
        for k in ("base", "fee", "charge", "buspkg"):
            d2[k] = None
        d2["center_error"] = "详细资料等四项接口抽风"
        w.data = d2
        w._render()
        pump(root, 0.5)
        blob2 = "\n".join(texts_of(w.win))
        for what in ("账号资料", "扣费记录", "缴费记录", "可购套餐"):
            check_true("🔴 明说「这次没读到%s」" % what,
                       "这次没读到%s" % what in blob2, "")
        check_true("页脚写出原因", "详细资料等四项接口抽风" in blob2, "")
        check_true("🔴 页脚说清「不代表没有」", "不代表「没有」" in blob2, "")
        # 主数据必须照常
        check("主数据照常：账号状态", row_value(w.win, "账号状态"), "正常")
        check_true("主数据照常：图表卡还在", "数据流量" in blob2, "")

        # ③ 走一遍真流程：同步后 data 里带着第二批（验 `full()` 真的带回来了）
        reset_fake()
        FakeSvc.script = {"has_session": "yes", "login": "ok"}
        w2 = open_panel(root)
        try:
            pump(root, 0.4)
            w2.start_sync()
            ok = pump(root, 6.0, lambda: w2.data is not None)
            check_true("同步后拿到数据", ok, "")
            check("同步后账号资料卡也画出来了",
                  row_value(w2.win, "开通日期"), "2024-09-01")
            check_true("同步后费用记录卡也画出来了",
                       "学校扣你的" in "\n".join(texts_of(w2.win)), "")
        finally:
            w2.close()
    finally:
        w.close()


# ---------------------------------------------------------------- B 档写操作

def test_actions(root):
    """⑰ B 档写操作：按钮出现条件 / 二次确认 / 请求体 / 失败分类 / 绝不重试。

    🔴 这一节比别的都重要：别的测漏了顶多「显示不对」，这里测漏了是
       **「把用户的网断了」**。
    """
    print("\n⑰ B 档写操作（下线 / 解绑 / 停机复机）")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "yes", "login": "ok"}
    w = open_panel(root)
    pump(root, 0.4)
    try:
        # ① 没同步过 ⇒ 一个写操作按钮都不许有（没有会话，点了也是白点）
        for name in ("下线这台", "解绑", "停机", "复机"):
            check_true("没同步过 ⇒ 没有「%s」按钮" % name,
                       find_button(w, name) is None, "")

        # ② 同步之后按钮出现，而且真的挂着左键事件（能点）
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.4)
        for name in ("下线这台", "解绑", "停机"):
            check_true("同步后出现「%s」" % name,
                       find_button(w, name) is not None, "")
        b = find_button(w, "下线这台")
        check_true("「下线这台」是圆角按钮（有 _rb_text）", b is not None, "")
        check_true("「下线这台」挂上了左键事件（能点）",
                   bool(b is not None and b.bind("<Button-1>")), "")

        # ③ 二次确认点了「取消」⇒ **一个请求都不发**
        asked = []

        def _no(msg):
            asked.append(msg)
            return False

        w.ask = _no
        FakeSvc.posts = []
        find_button(w, "下线这台").invoke()
        pump(root, 0.8)
        check("弹了一次确认框", len(asked), 1)
        check_true("确认框里说清了后果（会断网）", "断网" in asked[0], asked[0])
        check("取消 ⇒ 一个写请求都没发", FakeSvc.posts, [])

        # ④ 确认 ⇒ **先现取一次在线清单**，再按四个字段踢
        w.ask = lambda msg: True
        FakeSvc.posts = []
        find_button(w, "下线这台").invoke()
        ok = pump(root, 6.0,
                  lambda: any(p[0] == "kickonline.do" for p in FakeSvc.posts))
        check_true("踢人请求发出去了", ok, "")
        paths = [p[0] for p in FakeSvc.posts]
        check("先取在线清单、再踢（顺序）", paths[:2],
              ["getonline.do", "kickonline.do"])
        check("踢人请求体（四个字段全部来自 rows）", FakeSvc.posts[1][1],
              {"accountId": "2025000000", "accountIp": "10.15.116.156",
               "billingId": "bee1f27f", "serverIp": "10.0.1.10"})
        check("踢人只发了一次（绝不重试）",
              len([p for p in paths if p == "kickonline.do"]), 1)
        ok = pump(root, 8.0, lambda: not w.syncing and "已下线" in w.note)
        check_true("界面上说了「已下线」", ok, w.note)
        check_true("🔴 自动刷新后那句「已下线」**没被同步提示盖掉**",
                   "已下线" in w.note and "已同步" in w.note, w.note)

        # ⑤ 解绑：槽位号直接来自**那条**绑定记录
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.4)
        w.ask = lambda msg: True
        FakeSvc.posts = []
        find_button(w, "解绑").invoke()
        ok = pump(root, 6.0,
                  lambda: any(p[0] == "clearusermac.do" for p in FakeSvc.posts))
        check_true("解绑请求发出去了", ok, "")
        check("解绑请求体（clearType 就是那个槽位）",
              [p for p in FakeSvc.posts if p[0] == "clearusermac.do"][0][1],
              {"accountId": "2025000000", "clearType": "3"})
        pump(root, 8.0, lambda: not w.syncing)

        # ⑥ 停机：**先问原因**，再确认
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.4)
        w.ask = lambda msg: True
        w.ask_text = lambda title, prompt, initial: "测试原因"
        FakeSvc.posts = []
        find_button(w, "停机").invoke()
        ok = pump(root, 6.0,
                  lambda: any(p[0] == "selfstopping.do" for p in FakeSvc.posts))
        check_true("停机请求发出去了", ok, "")
        check("停机请求体（立即停机那条分支）",
              [p for p in FakeSvc.posts if p[0] == "selfstopping.do"][0][1],
              {"accountId": "2025000000", "shutdownReason": "测试原因"})
        pump(root, 8.0, lambda: not w.syncing)

        # 原因为空 ⇒ 直接拦下，**一个请求都不发**
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.4)
        w.ask_text = lambda title, prompt, initial: "   "
        FakeSvc.posts = []
        find_button(w, "停机").invoke()
        pump(root, 0.8)
        check("原因为空 ⇒ 一个请求都不发", FakeSvc.posts, [])
        check_true("并且说清了为什么", "不能为空" in w.note, w.note)

        # 取消输入 ⇒ 也不发
        w.ask_text = lambda title, prompt, initial: None
        FakeSvc.posts = []
        find_button(w, "停机").invoke()
        pump(root, 0.8)
        check("取消输入 ⇒ 一个请求都不发", FakeSvc.posts, [])

        # ⑦ 停机中 ⇒ 出现「复机」，且**不再有**「停机」
        d = json.loads(json.dumps(FAKE_DATA))
        d["stop"] = {"page_type": "1", "state": "停机中", "reason": "欠费",
                     "oper": "admin", "date": "2026-10-15"}
        w.data = d
        w.ask_text = lambda title, prompt, initial: "测试原因"
        w._render()
        pump(root, 0.4)
        check_true("停机中 ⇒ 有「复机」", find_button(w, "复机") is not None, "")
        check_true("停机中 ⇒ 没有「停机」", find_button(w, "停机") is None, "")
        FakeSvc.posts = []
        find_button(w, "复机").invoke()
        ok = pump(root, 6.0,
                  lambda: any(p[0] == "selfrecovery.do" for p in FakeSvc.posts))
        check_true("复机请求发出去了", ok, "")
        check("复机请求体",
              [p for p in FakeSvc.posts if p[0] == "selfrecovery.do"][0][1],
              {"accountId": "2025000000"})
        pump(root, 8.0, lambda: not w.syncing)

        # 🔴 状态是「未知」⇒ **一个按钮都不画**
        #    （不知道那时候什么操作合法；红线 51 的同类：不认识的取值别硬套）
        d["stop"] = {"page_type": "9", "state": "未知", "reason": "",
                     "oper": "", "date": ""}
        w.data = d
        w._render()
        pump(root, 0.4)
        check_true("状态「未知」⇒ 不画「停机」", find_button(w, "停机") is None, "")
        check_true("状态「未知」⇒ 不画「复机」", find_button(w, "复机") is None, "")

        # ⑧ 🔴 失败分类：四类各自说人话，而且**都只发一次**
        w.data = json.loads(json.dumps(FAKE_DATA))
        w._render()
        pump(root, 0.4)
        w.ask = lambda msg: True
        for mode, want, why in (
                ("net", "不确定",
                 "🔴 net 要说「成没成不确定」（只有这种可以再点）"),
                ("session", "重新登录", "session 要提示重新同步"),
                ("denied", "不要再点", "🔴 denied 要说「不要再点」"),
                ("boom", "内部错误", "🔴 内部异常也要在界面上看得见")):
            FakeSvc.script = {"has_session": "yes", "login": "ok", "_post": mode}
            FakeSvc.posts = []
            find_button(w, "解绑").invoke()
            ok = pump(root, 6.0, lambda: "失败" in getattr(w, "note", ""))
            check_true("%s：界面报出来了" % mode, ok, w.note)
            check_true("%s：%s" % (mode, why), want in w.note, w.note)
            check("%s：只发一次（绝不重试）" % mode, len(FakeSvc.posts), 1)
            # 🔴 界面上**不许出现 markdown 的 `**`** —— Tk 的 Label 不渲染
            #    markdown，用户会看到一串星号。第一版真写了「**不要再点**」，
            #    是**看日志**看出来的（断言只看子串，看不出多了两个星号）。
            bad = [x for x in texts_of(w.win) if "**" in x]
            check("%s：界面上没有 markdown 的星号" % mode, bad, [])
            w.note = ""
            w._render()
            pump(root, 0.3)

        # ⑨ 后台线程里跑（不能卡主线程）—— 上面几轮已经隐含验过，
        #    这里显式确认「写请求也在子线程」
        check_true("写请求不在 Tk 主线程上发", FakeSvc.off_main, "")
    finally:
        w.close()


# ---------------------------------------------------------------- C 档链接

def test_links_card(root):
    """⑱ C 档：去学校网页办（只放链接）+ 官方文件。"""
    print("\n⑱ C 档：跳转链接 + 官方文件")
    reset_fake()
    reset_history()
    FakeSvc.script = {"has_session": "yes", "login": "ok"}
    w = open_panel(root)
    pump(root, 0.4)
    real_open = C.open_in_browser

    def chips_now():
        return [c for c in all_widgets(w.win)
                if getattr(c, "_rb_text", None) == "打开"]

    try:
        # ① **没同步过也有这张卡**（它只是几个网址，不需要会话）
        t0 = texts_of(w.win)
        check_true("没同步过也有「去学校网页办」卡", "去学校网页办" in t0, "")

        # ② 五个 C 档入口 + 两个官方文件都在
        for label in ("账号充值 / 在线支付", "卡充值", "转账",
                      "办理套餐", "变更套餐"):
            check_true("有「%s」入口" % label, label in t0, "")
        check_true("有官方文件小标题",
                   any("官方文件" in x for x in t0), "")
        check_true("有账号安全通知",
                   any("账号安全管理" in x for x in t0), "")
        check_true("说清了「涉及付款，本软件不代办」",
                   any("本软件不代办" in x for x in t0), "")
        check_true("说清了「浏览器里要再登录一次」",
                   any("再登录一次" in x for x in t0), "")

        # ③ 每个「打开」都真的能点，且网址是 **hash 路由**
        #
        # ⚠️ 按**行结构**找片（左边 Label 的文字 == 那一行的说明），
        #    **不能**靠 `all_widgets()` 的顺序 —— 实测那个顺序不是创建顺序，
        #    照着下标点会点到别的行上去（第一版就点错了，7 次里有 5 次
        #    点的是同一行）。这和 `row_value` 靠「恰好两个子控件」定位是同一个思路。
        def chip_for(label):
            for f in all_widgets(w.win):
                try:
                    kids = f.winfo_children()
                except Exception:
                    continue
                if len(kids) != 2:
                    continue
                try:
                    if str(kids[0].cget("text")) != label:
                        continue
                except Exception:
                    continue
                if getattr(kids[1], "_rb_text", None) == "打开":
                    return kids[1]
            return None

        check("「打开」按钮 7 个（5 个 C 档 + 2 个官方文件）",
              len(chips_now()), 7)

        B = "http://202.196.169.246"
        want = [
            ("账号充值 / 在线支付", B + "/self/index.html#/payfees"),
            ("卡充值", B + "/self/index.html#/cardpay"),
            ("转账", B + "/self/index.html#/chargetransfer"),
            ("办理套餐", B + "/self/index.html#/subscribeOrder"),
            ("变更套餐", B + "/self/index.html#/changeorder"),
            ("《关于加强校园网账号安全管理的通知》（PDF）",
             B + "/uploadpic/notice/20260513.pdf"),
            ("《暑假自助暂停通知》（DOCX）",
             B + "/uploadpic/notice/20260703.docx"),
        ]
        opened = []
        C.open_in_browser = lambda u: (opened.append(u), True)[1]
        for label, url in want:
            chip_for(label).invoke()
            pump(root, 0.25)
            check("「%s」打开的网址" % label, opened[-1], url)
        check("🔴 所有链接都指向学校自己的服务器（没有第三方域名）",
              sorted({u.split("/")[2] for u in opened}), ["202.196.169.246"])

        # ④ 🔴 打开失败必须**把网址显示出来**（静默失败 = 用户以为按钮坏了）
        C.open_in_browser = lambda u: False
        w.note = ""
        chip_for("《暑假自助暂停通知》（DOCX）").invoke()
        pump(root, 0.4)
        check_true("🔴 打不开浏览器时把网址写出来了",
                   "没打开浏览器" in w.note and "uploadpic" in w.note, w.note)
        check_true("网址也画到界面上了",
                   any("uploadpic" in x for x in texts_of(w.win)), "")
    finally:
        C.open_in_browser = real_open
        w.close()


# ---------------------------------------------------------------- 收尾

def main():
    print("=" * 78)
    print("SelfServiceWindow（账号中心面板）界面自检")
    print("真的建窗口 / 不联网 / 不碰真实数据")
    print("=" * 78)

    d = datasafe.sandbox(copy_real=False, tag="selfwin")
    print("沙箱数据目录：%s" % d)
    print("沙箱里的历史文件：%s" % C.SELF_FLOW_FILE)

    real_svc = S.SelfService
    S.SelfService = FakeSvc
    real_net = _no_net_guard()
    rc = 0
    try:
        test_available()
        test_spacing_scale()
        root = _tk().Tk()
        # 🔴 **不能 withdraw**：面板靠 `winfo_width()` 量宽度来排版
        #    （两列 / 单列、wraplength 都从它来），主窗一隐藏，
        #    Windows 就不映射子控件 —— `winfo_width()` 永远是 1，
        #    那条「够不够宽」的断言就成了假绿（量到 1 也算过）。
        #    所以这里让它真的显示出来（测试期间闪一下而已）。
        # ⚠️ 2026-10-16：从 320x140 放大到 900x800 —— 面板现在是**嵌在主窗口
        #    里的一页**，得给它一块真实的画布尺寸才测得到两列排版。
        root.geometry("900x800+30+30")
        root.update()
        try:
            test_open_offline_empty(root)
            test_open_offline_with_history(root)
            test_sync_full_flow(root)
            test_sync_captcha_wrong(root)
            test_sync_login_fail(root)
            test_sync_tries_gate(root)
            test_sync_net_fail(root)
            test_job_crash_visible(root)
            test_clear_history(root)
            test_chart_views(root)
            test_devices_layout(root)
            test_center_cards(root)
            test_center_cards2(root)
            test_actions(root)
            test_links_card(root)
            test_two_columns(root)
        finally:
            root.destroy()
        test_off_main_thread()
        test_main_window_pages()
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
    proc_tree.exit_hard(_rc)
