# -*- coding: utf-8 -*-
"""复现 `selfwin_test.py` ⑨「后台任务崩了也要看得见」的偶发失败。

    python repro_crash_flake.py [轮数]

现象：全量跑的时候这一条偶尔红，报「异常变成了界面上的提示」失败，
      而单独跑又常常是绿的 ⇒ 典型的时序问题。
目的：**量化**失败率，并且把失败那一刻的 `FakeSvc.calls` 打出来 ——
      如果里面没有 `login`，说明请求根本没发出去（问题在 `_submit` 之前）；
      如果有 `login`，说明发出了但结果没回到界面（问题在队列/`_pump`）。

⚠️ 建过 Tk 窗口的脚本收尾必须 exit_hard()（红线 26）。
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import selfwin_test as T                                 # noqa: E402
import proc_tree                                         # noqa: E402


def main(argv):
    rounds = int(argv[0]) if argv else 6
    T.datasafe.sandbox(copy_real=False, tag="reproflake")
    real_svc = T.S.SelfService
    T.S.SelfService = T.FakeSvc
    real_net = T._no_net_guard()
    bad = 0
    try:
        root = T._tk().Tk()
        root.geometry("900x800+30+30")
        root.update()
        for i in range(rounds):
            n0 = len(T.FAILS)
            calls_before = list(T.FakeSvc.calls)
            try:
                T.test_job_crash_visible(root)
            except Exception:
                print("第 %d 轮**抛异常**：\n%s" % (i + 1, traceback.format_exc()))
                bad += 1
                continue
            new = T.FAILS[n0:]
            if new:
                bad += 1
                print("第 %d 轮失败：%s" % (i + 1, new))
                print("    本轮新增的请求：%s"
                      % [c for c in T.FakeSvc.calls[len(calls_before):]])
            else:
                print("第 %d 轮 OK" % (i + 1))
        root.destroy()
    finally:
        T.S.SelfService = real_svc
        T._restore_net(real_net)
    print("\n%d/%d 轮失败" % (bad, rounds))
    sys.stdout.flush()
    proc_tree.exit_hard(1 if bad else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
