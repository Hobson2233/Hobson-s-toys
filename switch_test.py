# -*- coding: utf-8 -*-
"""三轮真实切换实测：正常切换 -> 密码错回滚 -> 切回原账号

**账号密码不写在源码里** —— 这个仓库是公开的。从 `local_secrets.py` 读
（已在 .gitignore 里），没有就跳过并说明怎么建。

    cp local_secrets.example.py local_secrets.py   # 然后填真值

用法：
    python switch_test.py
"""
import sys
import time

import campus_login as C


def load_accounts():
    """从本地私有文件读 (学号, 密码, 备注)。读不到就明确退出。"""
    try:
        import local_secrets
    except ImportError:
        print("!! 找不到 local_secrets.py —— 里面是真实账号密码，不能进仓库。")
        print("   建一个：cp local_secrets.example.py local_secrets.py")
        print("   然后填 ACCOUNTS = [(学号, 密码, 备注), ...]")
        return []
    return [tuple(a) for a in getattr(local_secrets, "ACCOUNTS", []) if len(a) >= 2]


def show(tag):
    r = C.read_json(C.RESULT_FILE) or {}
    cfg = C.load_config()
    st = C.load_accounts()
    print("[%s] code=%s ok=%s msg=%s" % (tag, r.get("code"), r.get("ok"), r.get("message")))
    print("       config账号=%s  在线记录=%s  当前联网=%s"
          % (cfg.get("userId"), st.get("lastOnline"), C.test_internet()))


def switch_to(uid, pwd):
    prev = C.load_config().get("userId") or ""
    cfg = C.load_config()
    cfg["userId"] = uid
    cfg["passwd"] = pwd
    C.save_config(cfg)
    t0 = time.time()
    code = C.do_switch(uid, pwd, prev)
    return code, time.time() - t0


def main():
    accts = load_accounts()
    if not accts:
        return 2
    if len(accts) < 2:
        print("!! ACCOUNTS 里至少要两个账号（一个正常的 + 一个用来验证回滚的）")
        return 2

    a, b = accts[0], accts[1]          # a=先切过去，b=再切回来
    print("=== 起点 ===")
    show("起点")

    print("\n=== 第 1 轮：切到 %s（%s，正确密码）===" % (a[0], a[2] if len(a) > 2 else ""))
    c, dt = switch_to(a[0], a[1])
    print("耗时 %.1f 秒，退出码 %s" % (dt, c))
    show("第1轮")

    print("\n=== 第 2 轮：切到 %s 但故意用错密码，应触发回滚 ===" % b[0])
    c, dt = switch_to(b[0], "WRONG_PASSWORD_TEST")
    print("耗时 %.1f 秒，退出码 %s" % (dt, c))
    show("第2轮")

    print("\n=== 第 3 轮：切回 %s（%s，正确密码）===" % (b[0], b[2] if len(b) > 2 else ""))
    c, dt = switch_to(b[0], b[1])
    print("耗时 %.1f 秒，退出码 %s" % (dt, c))
    show("第3轮")
    return 0


if __name__ == "__main__":
    sys.exit(main())
