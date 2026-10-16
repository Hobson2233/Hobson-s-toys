# -*- coding: utf-8 -*-
"""把「内容需要高度 reqh」在各种运行状态下的值**量出来**，而不是从注释里抄。

为什么需要它（2026-10-04）：
    `content_h_probe.py` 只能量**当前这一台机器此刻**的状态（账号数、自启警告
    显不显示都由真实配置决定）。而 `geometry_test.CONTENT_H` 是一个固定的输入，
    到底该取哪个状态的值，只能靠「把各状态都量一遍」来回答。
    上一版注释里写着「1 账号无警告 814 / 1 账号 + 自启警告 830 / 2 账号 936」，
    实测发现 2 账号无警告 就已经是 830 —— 那三个数至少有一个是错的，
    正是本项目最忌讳的「注释自洽但是错的」。

做法：
    把决定高度的**外部输入**全部换成合成值（不碰真实配置、不写盘）：
      · load_accounts / load_config —— 决定账号卡里有几行、每行多高
      · autostart_stale / autostart_outdated —— 决定自启警告显不显示
    然后照常建界面，把 `window_geometry` 收到的 reqh 记下来。

🔴 为什么光「打桩 load_accounts」远远不够（2026-10-04 真出过事）：
    `run_gui` 启动时走 `load_all() → mutate_accounts() → save_accounts()`，
    而 `mutate_accounts()` 内部调的是**模块全局的** `load_accounts` ——
    也就是**被打桩的那个**。于是它把这里造的合成账号原样写进了真实的
    `accounts.json`：真实账号的 lastSuccess/lastAttempt/lastResult 被清空，
    还凭空多出 6 个假账号（就是下面 `six` 那档的 2025000010..015）。
    打桩只改变了「读什么」，完全没阻止「写回去」。

    所以现在**两道一起上**：`datasafe.sandbox()` 把数据目录整体换到临时目录
    （真身根本不在写入路径上），外加 `datasafe.assert_untouched()` 收尾核对。

用法：
    python content_h_matrix.py            # 跑全部预设状态
    python content_h_matrix.py plain      # 只跑一个（调试用）
"""
import json
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C      # noqa: E402
import datasafe               # noqa: E402
import proc_tree              # noqa: E402


# ---------------------------------------------------------------- 合成状态

def _acct(uid, last_success="", result="成功"):
    return {"passwd": "x" * 8, "lastSuccess": last_success,
            "lastAttempt": last_success, "lastResult": result}


def _store(accounts, last_online=""):
    return {"accounts": accounts, "lastOnline": last_online}


# 每种状态：(说明, 账号库, 当前 userId, 自启警告种类)
# 账号行的两种形态要分开量 —— 多一行「上次成功」就多十几像素：
#   plain 行：只有学号
#   rich  行：学号 + 「当前使用」+「上次登录」徽标 + 「上次成功：…」
STATES = {
    # 0 账号：列表位置显示「暂无保存的账号」那行
    "zero": ("0 账号 · 无警告", _store({}), "", None),
    # 1 账号，行内只有学号（最矮的账号行）
    "plain": ("1 账号(素行) · 无警告",
              _store({"2025000000": _acct("2025000000")}), "2025000000", None),
    # 1 账号，行内有徽标 + 上次成功（最高的账号行）
    "rich": ("1 账号(带徽标+上次成功) · 无警告",
             _store({"2025000000": _acct("2025000000", "2026-10-04 08:12")},
                    last_online="2025000000"), "2025000000", None),
    # 本机实际状态：2 账号
    "two": ("2 账号 · 无警告",
            _store({"2025000000": _acct("2025000000", "2026-10-04 08:12"),
                    "2025000001": _acct("2025000001", "2026-10-03 21:40")},
                   last_online="2025000000"), "2025000000", None),
    # 1 账号 + 「旧位置」警告（两行文字）
    "warn_stale": ("1 账号(素行) · 旧位置警告",
                   _store({"2025000000": _acct("2025000000")}), "2025000000", "stale"),
    # 1 账号 + 「旧设置」警告（两行文字）
    "warn_args": ("1 账号(素行) · 旧设置警告",
                  _store({"2025000000": _acct("2025000000")}), "2025000000", "outdated"),
    # 最坏组合：账号行最高 + 警告显示
    "worst": ("2 账号(带徽标) · 旧位置警告",
              _store({"2025000000": _acct("2025000000", "2026-10-04 08:12"),
                      "2025000001": _acct("2025000001", "2026-10-03 21:40")},
                     last_online="2025000000"), "2025000000", "stale"),
    # 多账号：账号卡会一直长（列表不截断）
    # ⚠️ 这 6 个是**合成**学号，故意从 2025000010 起（避开主账号 2025000000
    #    和副账号 2025000001，否则字典键会撞上、这一档就少一行）。
    "six": ("6 账号 · 无警告",
            _store({("2025000%03d" % (i + 10)): _acct("2025000%03d" % (i + 10), "2026-10-04 08:12")
                    for i in range(6)}, last_online="2025000000"), "2025000000", None),
}


# ---------------------------------------------------------------- 打桩

def apply_state(store, cur, warn):
    C.load_accounts = lambda: json.loads(json.dumps(store))   # 深拷一份，防被改
    C.load_config = lambda: {"userId": cur} if cur else {}
    C.autostart_stale = lambda: warn == "stale"
    C.autostart_outdated = lambda: warn == "outdated"
    # 自启升级会去动**真实的计划任务** —— 数据沙箱管不到它（它不在数据目录里），
    # 所以这里单独掐掉。沙箱只管文件，系统状态得自己负责。
    C.autostart_upgrade = lambda: (False, "")
    # 不联网：状态条那行文字长度与 reqh 无关，但省掉几秒等待
    C.network_state = lambda: C.NET_UNKNOWN


seen = {}
_orig_wg = C.window_geometry


def spy(sw, sh, reqh):
    seen["reqh"] = reqh
    seen["sw"], seen["sh"] = sw, sh
    return _orig_wg(sw, sh, reqh)


def _find(root):
    """按结构找到 (holder, inner)。对不上就返回 (None, None)。

    ⚠️ 2026-10-16：主窗口改成了「左侧导航 + 右侧内容」，中间多了
       shell / nav / content / page_settings 四层 —— 原来那句
       `root.winfo_children()[0]` 数的是层数，结构一变就静默失效
       （表现是「量不出来」，不是报错）。
       现在改成在整棵树里找**第一个「Canvas 里只装一个 Frame、
       那个 Frame 里也只有一个 Frame」**的结构：那是内容区那个滚动画布
       独有的形状。找不到就老实说量不出来，绝不拿一棵错的树算数字。
    """
    try:
        for canvas in _all_canvas(root):
            kids = canvas.winfo_children()
            if len(kids) != 1 or kids[0].winfo_class() != "Frame":
                continue
            inner = kids[0].winfo_children()
            if len(inner) == 1 and inner[0].winfo_class() == "Frame":
                return kids[0], inner[0]
        return None, None
    except Exception:
        return None, None


def _all_canvas(w):
    out = []
    for c in w.winfo_children():
        if c.winfo_class() == "Canvas":
            out.append(c)
        out.extend(_all_canvas(c))
    return out


_orig_mainloop = tk.Tk.mainloop


def _mainloop(self, *a, **k):
    def report():
        holder, inner = _find(self)
        if holder is None:
            print("拆解            量不出来")
        else:
            parts = []
            for card in inner.winfo_children():
                parts.append(card.winfo_reqheight())
            print("拆解            holder=%d  各段=%s"
                  % (holder.winfo_reqheight(), "+".join(map(str, parts))))
        self.after(120, self.quit)
    self.after(2200, report)
    _orig_mainloop(self, *a, **k)


C.window_geometry = spy
tk.Tk.mainloop = _mainloop


def measure(name):
    label, store, cur, warn = STATES[name]
    seen.clear()
    apply_state(store, cur, warn)
    C.run_gui()
    return seen.get("reqh")


def main():
    want = sys.argv[1:] or list(STATES)
    bad = [n for n in want if n not in STATES]
    if bad:
        print("不认识的档位：%s" % ", ".join(bad))
        print("可选：" + ", ".join(STATES))
        return 2
    # 🔴 必须在任何 run_gui 之前：数据目录整体换到临时目录
    datasafe.sandbox(copy_real=False, tag="content_h_matrix")
    print("=== 各状态下「内容需要高度」实测（合成输入，跑在数据沙箱里）===")
    got = {}
    for name in want:
        r = measure(name)
        got[name] = r
        print("%-32s reqh = %s" % (STATES[name][0], r))
    print()
    vals = [v for v in got.values() if v]
    if vals:
        print("范围            %d .. %d  （差 %d px）"
              % (min(vals), max(vals), max(vals) - min(vals)))
        # 本机 1707x1067 的比例上限是 906 —— 越过它就会冒滚动条
        cap = int(seen.get("sh", 0) * C.SCREEN_H_RATIO)
        over = sorted(n for n, v in got.items() if v and v > cap)
        print("本机上限        %d  ->  %s" % (cap, "全部装得下" if not over
                                          else "装不下的档位: " + ", ".join(over)))
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
        print()
        print(datasafe.assert_untouched())
    except Exception as e:
        print("异常: %s: %s" % (type(e).__name__, e))
        code = 1
    finally:
        C.window_geometry = _orig_wg
        tk.Tk.mainloop = _orig_mainloop
    proc_tree.exit_hard(code)
