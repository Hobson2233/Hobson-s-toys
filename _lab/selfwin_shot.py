# -*- coding: utf-8 -*-
"""给「上网数据」面板截图（一次性工具，本地看界面用）。

为什么单独做：
    `selfwin_test.py` 验的是**逻辑**（该有的文字在不在、图有没有被 GC）。
    逻辑全绿界面也可能很难看 —— 柱子挤成一团、数值标签互相压、卡片被裁。
    这一层只能靠眼睛，所以把真实窗口拍下来。

不联网：复用 `selfwin_test.FakeSvc`（假数据层），并且一样掐掉 socket。
不碰真实数据：`datasafe.sandbox()`。

用法：
    python _lab/selfwin_shot.py     # 输出到 repo/ui_shots/panel_*.png
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
import selfwin_test as T        # noqa: E402  （复用它的替身 / 假数据 / 假验证码图）

C = datasafe.C
# 和 `ui_shot.py` 落在同一个目录（那个目录已被 .gitignore 挡掉，不会误提交）。
OUT = os.path.join(REPO, "ui_shots")


def grab(widget, name):
    hwnd = shot.hwnd_of(widget)
    png, w, h = shot.capture(hwnd, foreground=False, settle=0.35)
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(png)
    print("  已保存 %-34s %dx%d  %d 字节" % (name, w, h, len(png)))


def seed(days):
    store, _ = H.load(C.SELF_FLOW_FILE)
    H.merge(store, "2025000000", days)
    H.mark_updated(store, "2025000000", "2026-10-12 22:40")
    H.save(C.SELF_FLOW_FILE, store)


def main():
    os.makedirs(OUT, exist_ok=True)
    datasafe.sandbox(copy_real=False, tag="selfwin_shot")
    T.S.SelfService = T.FakeSvc

    # 攒一批像真的历史：9 月下旬 + 10 月（含国庆那几天的缺口，照实测那样）
    hist = [
        {"date": "2026-09-28", "flow": 3762.2503, "up": 393.5657, "down": 3368.6846, "hours": 24.0},
        {"date": "2026-09-29", "flow": 32812.7136, "up": 6170.4502, "down": 26642.2634, "hours": 16.0},
        {"date": "2026-09-30", "flow": 4250.7180, "up": 256.3758, "down": 3994.3422, "hours": 20.0},
        # 10-01 ~ 10-06 服务端没有记录 ⇒ **故意不补**，看图上会不会画成 0
        {"date": "2026-10-07", "flow": 472.4753, "up": 114.8973, "down": 357.5780, "hours": 10.0},
        {"date": "2026-10-08", "flow": 4247.2497, "up": 852.6813, "down": 3394.5685, "hours": 20.0},
        {"date": "2026-10-09", "flow": 3906.4059, "up": 683.3043, "down": 3223.1016, "hours": 22.0},
        {"date": "2026-10-10", "flow": 3463.7843, "up": 1186.5343, "down": 2277.2500, "hours": 18.0},
        {"date": "2026-10-11", "flow": 3936.1761, "up": 973.9739, "down": 2962.2022, "hours": 20.0},
        {"date": "2026-10-12", "flow": 2428.6915, "up": None, "down": None, "hours": 14.0},
    ]

    root = T._tk().Tk()
    # 🔴 根窗口**不能 withdraw**：面板是 `transient(root)` 的，
    #    主窗一隐藏，Windows 就不映射子窗了 —— 截出来是 1x1 的空白。
    root.title("（截图用，不是真界面）")
    root.geometry("300x120+20+20")
    root.update()
    fam, base = T._font(root)

    print("开始：建面板并截图")

    # ① 本机没历史（空状态）
    T.reset_history()
    w = C.SelfServiceWindow(root, T._tk(), fam, base, "2025000000",
                            "demo-1234", "http://202.196.169.246")
    T.pump(root, 1.0)
    grab(w.win, "panel_01_还没同步过.png")
    w.close()

    # ② 按天（默认）
    T.reset_history()
    seed(hist)
    w = C.SelfServiceWindow(root, T._tk(), fam, base, "2025000000",
                            "demo-1234", "http://202.196.169.246")
    T.pump(root, 1.0)
    grab(w.win, "panel_02_按天.png")

    # ③ 按月
    w.set_view("month")
    T.pump(root, 0.5)
    grab(w.win, "panel_03_按月.png")
    w.set_view("day")
    T.pump(root, 0.3)

    # ④ 9 月（有缺口那个月）
    w.shift_month(-1)
    T.pump(root, 0.5)
    grab(w.win, "panel_04_九月.png")
    w.close()

    # ⑤ 验证码界面
    T.reset_fake()
    T.reset_history()
    T.FakeSvc.script = {"has_session": "no", "login": "ok"}
    w = C.SelfServiceWindow(root, T._tk(), fam, base, "2025000000",
                            "demo-1234", "http://202.196.169.246")
    w._today = lambda: "2026-10-12"
    w.start_sync()
    T.pump(root, 8.0, lambda: w._cap_big is not None)
    root.update()
    grab(w.win, "panel_05_验证码.png")

    # ⑥ 同步完（账号 + 图表 + 设备）
    w._entry.insert(0, "a1b2")
    w._submit()
    T.pump(root, 8.0, lambda: "账号状态" in " ".join(T.texts_of(w.win)))
    T.pump(root, 0.5)
    grab(w.win, "panel_06_同步后.png")
    w.close()

    root.destroy()
    print("完成，输出目录: %s" % OUT)
    proc_tree.exit_hard(0)


if __name__ == "__main__":
    main()
