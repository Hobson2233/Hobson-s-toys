# -*- coding: utf-8 -*-
"""端到端实测：先下线，紧接着自动登录，看日志里有没有新的多次探测行。

这是唯一能证明「桌面上的 exe 真的包含 portal_login 修复」的办法 ——
如果日志里出现「认证后第 N 次探测」，说明跑的是新代码。

用法：
    python e2e_test.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 数据目录逻辑只留一份（在 build.py 里），避免这里去旧位置读日志。
from build import _data_dir as data_dir  # noqa: E402
import proc_tree  # noqa: E402
import paths

EXE = paths.default_exe()


def run(flag, timeout):
    t = time.time()
    # run_exe 超时会把**整棵进程树**收掉。subprocess.run(timeout=...) 只杀启动器
    # 父进程，子进程会变成孤儿 —— 对 --logout / --auto 来说就是留下一个僵尸。
    rc, timed_out = proc_tree.run_exe(EXE, [flag], timeout=timeout)
    return rc, time.time() - t, ("超时 %ss" % timeout if timed_out else None)


def tail(path, n=40):
    if not os.path.isfile(path):
        return "(没有日志文件)"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-n:])


def main():
    data = data_dir()
    logf = os.path.join(data, "campus-login.log")
    resf = os.path.join(data, "last-result.json")

    before = os.path.getsize(logf) if os.path.isfile(logf) else 0

    print("=== 1) 先下线 ===")
    rc, dt, err = run("--logout", 60)
    print("退出码 %s / %.1fs%s" % (rc, dt, " / " + err if err else ""))
    time.sleep(1)

    print()
    print("=== 2) 立刻自动登录（真实认证） ===")
    rc2, dt2, err2 = run("--auto", 120)
    print("退出码 %s / %.1fs%s" % (rc2, dt2, " / " + err2 if err2 else ""))

    print()
    print("=== last-result.json ===")
    if os.path.isfile(resf):
        with open(resf, "r", encoding="utf-8", errors="replace") as f:
            try:
                print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
            except Exception:
                f.seek(0)
                print(f.read())
    else:
        print("(不存在)")

    print()
    print("=== 本次新增日志 ===")
    if os.path.isfile(logf):
        with open(logf, "r", encoding="utf-8", errors="replace") as f:
            f.seek(before)
            print(f.read())
    else:
        print("(没有日志文件)")

    print("=== 日志尾部 40 行 ===")
    print(tail(logf))

    return 0


if __name__ == "__main__":
    sys.exit(main())
