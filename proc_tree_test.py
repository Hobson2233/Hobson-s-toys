# -*- coding: utf-8 -*-
"""测 proc_tree：看门狗会不会按时硬退、进程树会不会被清干净。

为什么必须测这两件事：

1. **看门狗只在出问题时才生效** —— 平时它什么都不做，所以「没报错」不能说明它
   是好的。必须真的让它超时一次，看退出码对不对；也要验证解除之后**不会误杀**，
   否则正常跑完的脚本会被它半路砍掉。

2. **进程树清理是那个真 bug 的修复**，而且它有一个「反例」值得钉住：
   onefile 的 exe 是**两个进程**（启动器父进程 + 持有窗口的子进程），
   而 Popen.pid 给的是父进程。所以「只 terminate 父进程」会留下孤儿 ——
   这就是用户看到那个卡住的设置窗口的来源。第 3 节**故意复现这个错误做法**，
   确认它真的会留下孤儿，再确认正确做法不会。

**全部用 --selftest 跑，不建 Tk 窗口** —— 这个测试本身绝不能成为新的不稳定源。

用法：
    python proc_tree_test.py
"""
import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import proc_tree
import paths

EXE = paths.default_exe()
PY = sys.executable
FAILS = []

# 进程枚举在 proc_tree 里（那边才是实现），这里直接用 —— 别在这里再抄一份
children_of = proc_tree.children_of
snapshot = proc_tree.snapshot


def wait_child(pid, timeout=10.0):
    """等 pid 生出来的第一个子进程。返回子进程 pid 或 None。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        kids = children_of(pid)
        if kids:
            return kids[0]
        time.sleep(0.1)
    return None


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-46s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def run_snippet(code, timeout=30):
    t0 = time.time()
    p = subprocess.run([PY, "-c", code], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, time.time() - t0, p.stdout.decode("utf-8", "replace")


# ==================== 1. 看门狗 ====================

def test_watchdog():
    print("=== 1. 看门狗到点会硬退（退出码 %d）===" % proc_tree.WATCHDOG_EXIT)
    code = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import proc_tree\n"
        "proc_tree.arm_watchdog(1, '自测')\n"
        "time.sleep(30)\n"
        "print('不该走到这里')\n"
        "sys.exit(0)\n" % HERE
    )
    rc, secs, out = run_snippet(code)
    check("超时后按约定退出码退出", rc, proc_tree.WATCHDOG_EXIT)
    check("确实是到点才退（1~10 秒之间）", 0.5 <= secs <= 10, True)
    check("看门狗打了提示", "看门狗" in out, True)
    check("卡住之后的代码没被执行", "不该走到这里" in out, False)

    print()
    print("=== 2. 正常跑完不会被误伤 ===")
    code = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import proc_tree\n"
        "d = proc_tree.arm_watchdog(20, '自测')\n"
        "time.sleep(0.3)\n"
        "d()\n"
        "time.sleep(2)\n"
        "print('正常结束')\n"
        "sys.exit(0)\n" % HERE
    )
    rc, secs, out = run_snippet(code)
    check("解除后正常退出码 0", rc, 0)
    check("正常跑完了", "正常结束" in out, True)
    check("没有被看门狗杀掉", proc_tree.WATCHDOG_EXIT != rc, True)

    print()
    print("=== 3. 解除函数可重复调用 / exit_hard 按码退出 ===")
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import proc_tree\n"
        "d = proc_tree.arm_watchdog(20, '自测')\n"
        "d(); d(); d()\n"
        "print('多次解除 OK')\n"
        "proc_tree.exit_hard(7)\n"
        "print('不该走到这里')\n" % HERE
    )
    rc, secs, out = run_snippet(code)
    check("多次 disarm 不报错", "多次解除 OK" in out, True)
    check("exit_hard 的退出码就是 7", rc, 7)
    check("硬退前输出没丢", "即将硬退" in out or "多次解除 OK" in out, True)
    check("硬退之后没继续执行", "不该走到这里" in out, False)


# ==================== 4/5. 进程树 ====================

def test_tree():
    if not os.path.isfile(EXE):
        print()
        print("=== 4/5. 进程树 ===")
        print("  [SKIP] 找不到 exe：%s" % EXE)
        return

    print()
    print("=== 4. 错误做法会留下孤儿（复现原 bug）===")
    # **完全照旧代码的写法**：裸 Popen，不建 job。
    # （不能用 proc_tree.start —— 那样进程进了 job，关句柄时子进程会一起被杀，
    #   就复现不出「只杀父进程」的错误了。第一版测试就是栽在这里。）
    proc = subprocess.Popen([EXE, "--selftest"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = proc.pid
    child = wait_child(parent)
    check("onefile 确实是父子两个进程", child is not None, True)
    if child is None:
        proc.kill()
        return
    print("      父（启动器）=%s  子（真正跑代码、持有窗口的那个）=%s" % (parent, child))

    proc.kill()                      # 只杀父 —— 这正是旧代码干的事
    proc.wait(timeout=10)
    time.sleep(0.3)
    orphan = proc_tree.alive(child)
    if orphan:
        check("只杀父进程 -> 子进程变成孤儿（这就是 bug）", orphan, True)
    else:
        print("  [SKIP] 子进程已经自己跑完了（--selftest 太快），没抓到孤儿窗口")

    # 收拾干净
    proc_tree.kill_tree(child)
    time.sleep(0.5)
    check("兜底 kill_tree 能把孤儿清掉", proc_tree.alive(child), False)

    print()
    print("=== 5. 正确做法：整棵树一起清 ===")
    proc, job = proc_tree.start([EXE, "--selftest"])
    parent = proc.pid
    child = wait_child(parent)
    check("拿到子进程", child is not None, True)
    if child is None:
        proc_tree.shutdown(proc, job)
        return
    ok = proc_tree.shutdown(proc, job)
    check("shutdown 报告清干净了", ok, True)
    check("父进程没了", proc_tree.alive(parent), False)
    check("**子进程也没了**（不再有孤儿窗口）", proc_tree.alive(child), False)


def test_sweep():
    """清理解压目录的工具：该删的删掉，太新的别动。

    这个函数只在「已经攒了垃圾」的时候才有事做，所以不能靠「没报错」判断它好使 ——
    得自己造两个目录（一个够老、一个刚建），看它是否**只删老的那个**。
    """
    import shutil
    import tempfile

    print()
    print("=== 6. _MEI 残留清理：删旧的、留新的 ===")
    root = tempfile.gettempdir()
    tag = "_MEItest_%d" % os.getpid()
    old, new = root + os.sep + tag + "_old", root + os.sep + tag + "_new"
    for d in (old, new):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "dummy.txt"), "w") as f:
            f.write("x")
    # 把 old 的修改时间拨到 2 小时前（超过 600 秒门槛）
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))

    n = proc_tree.sweep_mei_temp(min_age=600, verbose=False)
    check("够老的残留被删掉", os.path.isdir(old), False)
    check("刚建的目录没被动（时间门槛生效）", os.path.isdir(new), True)
    check("返回值 >= 1（至少删了 old）", n >= 1, True)

    # 关键安全性质：**目录里有文件被打开着时，改名必须失败 → 跳过**。
    # 这是整个清理工具敢动别人目录的前提（正在运行的程序就是这么被保护住的）。
    locked = root + os.sep + tag + "_locked"
    os.makedirs(locked, exist_ok=True)
    fh = open(os.path.join(locked, "inuse.txt"), "w")
    try:
        os.utime(locked, (two_hours_ago, two_hours_ago))
        proc_tree.sweep_mei_temp(min_age=600, verbose=False)
        check("**有文件被打开 -> 目录没被删（保护生效）**", os.path.isdir(locked), True)
    finally:
        fh.close()

    # 收拾
    for d in (new, locked):
        try:
            shutil.rmtree(d)
        except Exception:
            pass
    check("测试自己建的目录已清理", os.path.isdir(new) or os.path.isdir(locked), False)


def test_guitest_exit():
    """--guitest 必须按时返回 —— 这是「启动器父进程挂着不退」那个坑的回归测试。

    现象：`--guitest` 的**子进程**正常跑完并退出（标记文件里都写好 GUI_OK 了），
    但**启动器父进程**卡在递归删 `_MEIxxxx` 上不退。而 `Popen.pid` 给的正是父进程，
    于是「明明已经跑完」被误判成「超时未退出」。这个假象害我两次把它当环境抖动放过，
    所以值得钉一条测试。

    修法：`run_exe` 改成等「真正干活的那个进程」（`wait_done`），而不是等父进程。
    这个现象是**间歇性**的（有时 2 秒完成、有时一直挂着），所以连跑 3 次 ——
    只跑一次通过说明不了什么。
    """
    print()
    print("=== 7. --guitest 连跑 3 次都要按时返回（回归）===")
    if not os.path.isfile(EXE):
        print("  [SKIP] 找不到 exe：%s" % EXE)
        return
    for i in range(1, 4):
        t = time.time()
        rc, timed_out = proc_tree.run_exe(EXE, ["--guitest"], timeout=45)
        dt = time.time() - t
        check("第 %d 次：按时返回且退出码 0（%.1fs）" % (i, dt),
              (rc, timed_out), (0, False))


def main():
    test_watchdog()
    test_tree()
    test_sweep()
    test_guitest_exit()

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    # 这个测试自己会强杀 onefile 进程，所以一定会漏下 _MEI 解压目录（负责清理的
    # 启动器父进程被一起杀了）。收尾时扫一遍。
    # 注意：**本次刚生成的目录会被时间门槛挡下**（默认 10 分钟），所以这里多半
    # 报「跳过 N 个」—— 那是正常的，它们会在下一次扫描（或跑 build 时）被收走。
    # 时间门槛是给「别的程序刚起来」留的余量，不值得为了跑完立刻干净而取消它。
    print()
    proc_tree.sweep_mei_temp()
    n, b = proc_tree.mei_usage()
    if n:
        print("_MEI 残留: %d 个, %.1f MB —— 测完想立刻清干净就跑 sweep_mei_temp(min_age=0)"
              % (n, b / 1048576.0))
    else:
        print("_MEI 残留: 0 个")
    print("全部通过 —— 看门狗会触发不会误伤，进程树也清得干净")
    return 0


if __name__ == "__main__":
    sys.exit(main())
