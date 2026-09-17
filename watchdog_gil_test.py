# -*- coding: utf-8 -*-
"""看门狗在「主线程卡在 Tk」时到底能不能触发？

为什么必须单独验：
    `proc_tree.arm_watchdog()` 是**线程**版 —— 到点由 daemon 线程调 `hard_kill`。
    但主线程若卡在**持 GIL 的 C 调用**里，看门狗线程连 GIL 都抢不到，
    `time.sleep` 醒来后永远卡在「等 GIL」上，**永远不会触发**。
    而它存在的意义恰恰是这种情况（「窗口不会留在用户桌面上」）。

    `proc_tree_test.py` 里的看门狗用例只能证明「主线程在 `time.sleep` 时它会触发」——
    那一步是**主动让出 GIL** 的，所以证明不了要防的那种卡法。
    这正是本项目那条铁律：**检查通过 != 检查真的在跑**。

做法：
    子进程里 `arm_watchdog(WATCH)` → `run_gui(smoke=True)` → 落标记 → `hard_kill(0)`。
    用 pythonw.exe（无控制台），避免第 12 节那个「控制台模式 ExitProcess 卡住」
    混进来污染判断；退出也走 hard_kill，不引入第二个变量。

判读（每轮三选一）：
    标记文件在            -> 本轮 Tk 没卡，不计入（环境这次是好的）
    标记不在 + 退出码 4    -> Tk 卡住了，**看门狗救回来了**（线程版可用）
    标记不在 + 一直活着    -> **看门狗失效**（GIL 被主线程占住）

用法：
    python watchdog_gil_test.py [轮数] [看门狗秒数]           # 真实 Tk 卡（概率性，常复现不到）
    python watchdog_gil_test.py --synthetic [看门狗秒数]      # 确定性地占住 GIL
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import proc_tree    # noqa: E402

PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
MARKER = os.path.join(HERE, "_watchdog_gil_marker.txt")

CHILD = (
    "import sys; sys.path.insert(0, %r)\n"
    "import proc_tree, campus_login as C\n"
    "proc_tree.arm_watchdog(%d, 'watchdog_gil_test')\n"
    "C.run_gui(smoke=True)\n"
    "open(%r, 'w', encoding='utf-8').write('smoke_ok')\n"
    "proc_tree.hard_kill(0)\n"
)

# 确定性地占住 GIL：switchinterval 调到 1000 秒，主线程再纯 Python 忙循环 ——
# 解释器在 1000 秒内不会把 GIL 让出去，看门狗线程 time.sleep 醒来后
# 永远卡在「等 GIL」上。这是「主线程持 GIL 不放」的可复现模型
# （真实 Tk 卡住是否也这样，还没测到 —— 见文件头）。
SYNTH = (
    "import sys; sys.path.insert(0, %r)\n"
    "import proc_tree\n"
    "sys.setswitchinterval(1000.0)\n"
    "proc_tree.arm_watchdog(%d, 'synthetic GIL hold')\n"
    "t = 0; i = 0\n"
    "while True:\n"                     # 纯 Python 忙循环，1000 秒内不给 GIL 让路
    "    i += 1\n"
    "    t += i * i\n"
    "open(%r, 'w', encoding='utf-8').write('loop_done')\n"
    "proc_tree.hard_kill(0)\n"
)


def one_round(idx, watch, synthetic=False):
    try:
        os.remove(MARKER)
    except OSError:
        pass
    if synthetic:
        code = SYNTH % (HERE, watch, MARKER)
        exe = sys.executable          # 合成场景不需要 Tk，用带控制台的就行
    else:
        code = CHILD % (HERE, watch, MARKER)
        exe = PYW
    proc = subprocess.Popen([exe, "-c", code], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    limit = watch + 10
    t0 = time.time()
    while time.time() - t0 < limit:
        if proc.poll() is not None:
            break
        time.sleep(0.2)

    alive = proc.poll() is None
    marked = os.path.isfile(MARKER)
    if alive:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait(timeout=10)

    what = "忙循环占住 GIL" if synthetic else "Tk"
    if marked:
        print("  第 %d 轮: %s 没卡（本轮不计入）" % (idx, what))
        return "ok"
    if not alive and proc.returncode == proc_tree.WATCHDOG_EXIT:
        print("  第 %d 轮: **%s 卡住了，看门狗救回来了**（退出码 %d）"
              % (idx, what, proc_tree.WATCHDOG_EXIT))
        return "rescued"
    print("  第 %d 轮: **看门狗失效** —— %s 卡住 %ds 后进程仍存活，只能外部杀掉"
          % (idx, what, limit))
    return "failed"


def main():
    args = sys.argv[1:]
    synthetic = "--synthetic" in args
    if synthetic:
        args.remove("--synthetic")
        # 合成模式一轮就要跑满一整个看门狗周期，跑多轮没意义 ——
        # 所以这里的数字是**看门狗秒数**，不是轮数（用法说明也这么写的）。
        # 曾经把它当 rounds 收下、随后又被强制成 1，参数等于被静默吞掉。
        rounds = 1
        watch = int(args[0]) if args else 15
    else:
        rounds = int(args[0]) if args else 6
        watch = int(args[1]) if len(args) > 1 else 15
    if not synthetic and not os.path.isfile(PYW):
        print("找不到 pythonw.exe: %s" % PYW)
        return 2
    if synthetic:
        print("被测: 合成场景 —— sys.setswitchinterval(1000) + 纯 Python 忙循环")
        print("      （确定性占住 GIL，模拟「主线程卡在持 GIL 的调用里」）")
    else:
        print("被测: 源码模式 run_gui(smoke=True)（pythonw，无控制台）")
    print("看门狗: %d 秒；轮数: %d" % (watch, rounds))
    print("=" * 66)
    tally = {"ok": 0, "rescued": 0, "failed": 0}
    for i in range(1, rounds + 1):
        tally[one_round(i, watch, synthetic)] += 1
    print()
    print("=" * 66)
    print("没卡: %d 轮 / 卡住被救回: %d 轮 / **看门狗失效: %d 轮**"
          % (tally["ok"], tally["rescued"], tally["failed"]))
    if tally["failed"]:
        print("结论: **看门狗在这种卡法下不可靠** —— 得改成独立进程版。")
        return 1
    if tally["rescued"]:
        print("结论: 至少救回过一次，看门狗在这个卡法下**能**触发。")
        return 0
    print("结论: 本轮没复现到卡住，**什么都没验到**（不是通过）—— 加轮数或稍后再跑。")
    return 3


if __name__ == "__main__":
    try:
        code = main()
    finally:
        # 标记文件只是「子进程跑完了没」的信号，别留在仓库里：
        # 它会以未跟踪文件的形式出现在 `git status` 里，内容还像一份「通过」的凭据，
        # 容易被人当成证据提交上去。正常退出和抛异常都清掉；
        # 被强杀时清不掉，所以 .gitignore 里也留了一条。
        try:
            os.remove(MARKER)
        except OSError:
            pass
    sys.exit(code)
