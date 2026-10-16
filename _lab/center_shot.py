# -*- coding: utf-8 -*-
"""「账号中心」新增卡片的截图 + **宽度实测**（一次性工具，本地看界面用）。

为什么截图不够：`Canvas` 的 reqwidth 不跟内容长，长文字被**静默裁掉**时
截图里可能只是「少了一截」，看不出来。所以这里除了拍图，还逐个控件量
`winfo_reqwidth()`，凡是超过卡片可用宽度的都打出来。

不联网（复用 selfwin_test.FakeSvc）/ 不碰真实数据（datasafe.sandbox）。
用法：python _lab/center_shot.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import datasafe                 # noqa: E402
import proc_tree                # noqa: E402
import self_history as H        # noqa: E402
import shot                     # noqa: E402
import selfwin_test as T        # noqa: E402

C = datasafe.C
OUT = os.path.join(REPO, "ui_shots")

HIST = [
    {"date": "2026-10-07", "flow": 472.4753, "up": 114.8973,
     "down": 357.5780, "hours": 10.0},
    {"date": "2026-10-08", "flow": 4247.2497, "up": 852.6813,
     "down": 3394.5685, "hours": 20.0},
    {"date": "2026-10-09", "flow": 3906.4059, "up": 683.3043,
     "down": 3223.1016, "hours": 22.0},
    {"date": "2026-10-10", "flow": 3463.7843, "up": 1186.5343,
     "down": 2277.2500, "hours": 18.0},
    {"date": "2026-10-11", "flow": 3936.1761, "up": 973.9739,
     "down": 2962.2022, "hours": 20.0},
    {"date": "2026-10-12", "flow": 2428.6915, "up": None, "down": None,
     "hours": 14.0},
]


def grab(widget, name):
    hwnd = shot.hwnd_of(widget)
    png, w, h = shot.capture(hwnd, foreground=False, settle=0.4)
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(png)
    print("  已保存 %-34s %dx%d  %d 字节" % (name, w, h, len(png)))


def measure(w, tag):
    """量所有控件的 reqwidth，挑出**超过卡片可用宽度**的（会被裁）。"""
    avail = C.CHART_W + 2 * C._CARD_PAD_X      # 卡片内部可用宽度
    bad = []
    for c in T.all_widgets(w.win):
        try:
            rq = c.winfo_reqwidth()
            cls = c.winfo_class()
        except Exception:
            continue
        if rq > avail:
            bad.append((cls, rq))
    print("  [%s] 卡片可用宽度 = %d；超宽控件 = %s"
          % (tag, avail, sorted(set(bad)) or "无"))
    # holder 的**要求**宽度 vs 画布**实际**宽度（差 > 2px 就是横向被裁）
    hw, cw = w.holder.winfo_reqwidth(), w.canvas.winfo_width()
    print("  [%s] holder 需要 %d px / 画布实际 %d px  ->  %s"
          % (tag, hw, cw, "OK" if hw <= cw + 2 else "🔴 横向被裁"))
    return sorted(set(bad))


def main():
    os.makedirs(OUT, exist_ok=True)
    datasafe.sandbox(copy_real=False, tag="center_shot")
    T.S.SelfService = T.FakeSvc

    root = T._tk().Tk()
    root.title("（截图用，不是真界面）")
    root.geometry("300x120+20+20")
    root.update()
    fam, base = T._font(root)

    T.reset_history()
    store, _ = H.load(C.SELF_FLOW_FILE)
    H.merge(store, "2507210230", HIST)
    H.mark_updated(store, "2507210230", "2026-10-16 09:20")
    H.save(C.SELF_FLOW_FILE, store)

    T.reset_fake()
    T.FakeSvc.script = {"has_session": "yes", "login": "ok"}
    w = C.SelfServiceWindow(root, T._tk(), fam, base, "2507210230",
                            "demo-1234", "http://202.196.169.246")
    T.pump(root, 1.0)

    # ① 还没同步过（只有本机历史）
    grab(w.win, "center_01_没同步过.png")
    measure(w, "没同步过")

    # ② 同步完（账号 + 图表 + 详单 + 设备）
    w.start_sync()
    T.pump(root, 8.0, lambda: w.data is not None)
    T.pump(root, 0.8)
    grab(w.win, "center_02_顶部.png")
    measure(w, "同步后")

    # ③ 滚到底，看详单 + 设备
    w.canvas.yview_moveto(1.0)
    T.pump(root, 0.5)
    grab(w.win, "center_03_底部.png")

    # ④ 详单在中间，滚到 0.55 左右
    w.canvas.yview_moveto(0.5)
    T.pump(root, 0.5)
    grab(w.win, "center_04_中间.png")

    # ⑤ A 档没读到
    w.data = dict(w.data)
    w.data["bill"] = None
    w.data["stop"] = None
    w.data["package"] = None
    w.data["center_error"] = "详单接口抽风"
    w._render()
    T.pump(root, 0.6)
    w.canvas.yview_moveto(1.0)
    T.pump(root, 0.4)
    grab(w.win, "center_05_A档没读到.png")

    # ⑥ 停机中
    w.data = None
    w._render()
    T.pump(root, 0.4)
    d = __import__("json").loads(__import__("json").dumps(T.FAKE_DATA))
    d["stop"] = {"page_type": "1", "state": "停机中", "reason": "欠费停机",
                 "oper": "admin", "date": "2026-10-15 10:00"}
    w.data = d
    w._render()
    T.pump(root, 0.5)
    grab(w.win, "center_06_停机中.png")

    w.close()
    root.destroy()
    print("完成，输出目录: %s" % OUT)
    proc_tree.exit_hard(0)


if __name__ == "__main__":
    main()
