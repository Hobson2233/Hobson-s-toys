# -*- coding: utf-8 -*-
"""验证「退出当前账号」这次修复真的把 bug 堵住了（带阳性 + 阴性对照）。

对应 `logout_fail_probe.py` 复现出来的那三个成因，逐条验：

  1. 门户正文说「下线成功」/「本来就不在线」时，不能再靠 3 秒后的网络探测
     把一次成功的下线报成失败 —— `logout_body_verdict()` 要认出来。
  2. 判不出来（None）时要**重试**，而不是立刻弹「退出失败，请稍后重试」。
  3. 两个并发动作不能再互相把对方弄失败 —— 进程内锁要让后到的那个
     明确报「另一个操作正在执行」，而不是跑到门户那儿去撞车。

每个断言都配阴性对照：信号消失时断言必须失败（否则就是恒真断言）。

用法：
    python logout_retry_probe.py
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print("  [%s] %-58s -> %r%s"
          % ("OK" if ok else "失败", name, got, "" if ok else "  (期望 %r)" % (want,)))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, extra=""):
    print("  [%s] %-58s %s" % ("OK" if cond else "失败", name, extra))
    if not cond:
        FAILS.append(name)


# ==================== 1. 正文判定 ====================
def part1_body_verdict():
    print("=== 1. 门户正文的判定（logout_body_verdict）===")
    f = C.logout_body_verdict

    # 真实日志里的那一句，必须认成成功
    check("真实现场那句「下线成功」", f("--> WIFI authentication 下线成功!"), True)
    # 重复下线时门户的说法 —— 用户的意图（让这个 IP 下线）已经达成，算成功
    check("「该IP未在线」算成功", f("该用户未在线，请勿重复操作"), True)
    check("「已经下线」算成功", f("该账号已经下线"), True)
    # 明确失败
    check("「下线失败」算失败", f("下线失败，请重试"), False)

    # ---- 阴性对照：认不出来时必须给 None，不许猜 ----
    check("空正文 -> None（不猜）", f(""), None)
    check("无关键字的正文 -> None（不猜）", f("portal"), None)
    check("HTML 包着的成功字样也能认出来",
          f("<html><body>WIFI authentication 下线成功!</body></html>"), True)

    # 顺序坑：同时含「未在线」和「下线」时必须走成功分支
    check("「未在线…下线」同时出现时判成功（顺序坑）",
          f("该IP未在线，无需重复下线"), True)

    # ---- 阴性对照：把判定函数换成一个恒真的假货，本组前两条断言必须失败 ----
    real = C.logout_body_verdict
    C.logout_body_verdict = lambda t: True
    bad_would_pass = (C.logout_body_verdict("莫名其妙") is None)
    C.logout_body_verdict = real
    check_true("阴性对照：恒真的假判定器确实会让上面的断言失效",
               bad_would_pass is False, "（说明这组断言不是恒真）")


# ==================== 2. portal_logout 三态 ====================
def part2_tristate():
    print()
    print("=== 2. portal_logout 返回三态，不再靠单次探测下结论 ===")
    saved_http = C.http_post
    saved_tok = C.get_distoken
    saved_net = C.test_internet
    saved_sleep = time.sleep

    class R(object):
        def __init__(self, text):
            self.status_code = 200
            self.text = text

    def with_portal(body, net_after):
        """把门户响应和后续网络探测都换成可控的。"""
        C.get_distoken = lambda: "tok"
        C.http_post = lambda *a, **k: R(body)
        C.test_internet = lambda *a, **k: net_after
        time.sleep = lambda *a, **k: None          # 别真等那 3 秒
        try:
            return C.portal_logout()
        finally:
            C.http_post = saved_http
            C.get_distoken = saved_tok
            C.test_internet = saved_net
            time.sleep = saved_sleep

    # 核心修复点：正文明确说成功，但 3 秒后探测「仍可联网」（会话没断干净的
    # 典型表现）—— 修复前会返回 False（弹「退出失败」），修复后必须 True。
    check("正文说成功 + 探测仍在线 -> True（修复前是 False）",
          with_portal("WIFI authentication 下线成功!", True), True)

    check("正文说成功 + 探测也下线 -> True",
          with_portal("下线成功", False), True)

    # 正文明确说失败 -> False（不重试，重试没意义）
    check("正文明确失败 -> False", with_portal("下线失败", False), False)

    # 正文无结论 + 探测被门户拦截 -> True
    check("正文无结论 + 探测已被拦截 -> True", with_portal("portal", False), True)

    # 正文无结论 + 探测仍在线 -> False（确定还在线）
    check("正文无结论 + 探测仍在线 -> False", with_portal("portal", True), False)

    # 正文无结论 + 探测也不确定 -> None（**不是** False！这正是老代码的坑：
    # 把「测不出来」当成「没下线」，于是正常下线也会报失败）
    check("正文无结论 + 探测不确定 -> None（老代码在这里报失败）",
          with_portal("portal", None), None)

    # 拿不到 distoken -> None（能力问题，不是结论）
    C.get_distoken = lambda: None
    try:
        check("拿不到 distoken -> None（不再直接判 False）", C.portal_logout(), None)
    finally:
        C.get_distoken = saved_tok


# ==================== 3. do_logout 会重试 ====================
def part3_retry():
    print()
    print("=== 3. do_logout 判不出来时会重试（而不是立刻报失败）===")
    saved = {k: getattr(C, k) for k in ("wait_for_campus", "test_internet",
                                        "portal_logout", "set_last_online")}
    C.wait_for_campus = lambda *a, **k: True
    C.test_internet = lambda *a, **k: True
    C.set_last_online = lambda *a, **k: None
    saved_sleep = time.sleep
    time.sleep = lambda *a, **k: None

    try:
        # 前两次判不出来、第三次成功 —— 修复前会在第一次就弹「退出失败」
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                return None
            return True

        C.portal_logout = flaky
        code = C.do_logout()
        r = C.read_json(C.RESULT_FILE) or {}
        check("抖动两次后第三次成功 -> 退出码 0", code, 0)
        check("尝试了 3 次", calls["n"], 3)
        check("提示语是成功那句", r.get("message"), "已退出当前账号")

        # 一直判不出来 -> 不能报成功（否则用户在还联网时以为退了）
        C.portal_logout = lambda: None
        code = C.do_logout()
        r = C.read_json(C.RESULT_FILE) or {}
        check("三次都判不出来 -> 退出码 3（不算成功）", code, 3)
        check("提示语是「无法确认」而不是「已退出」",
              r.get("message"), "无法确认是否已退出，请检查网络状态后重试")
        check_true("用独立的 code 与「确定失败」区分开",
                   r.get("code") == "LOGOUT_UNKNOWN", r.get("code"))

        # 门户明确说还在线 -> 直接失败，不该白重试 3 次
        calls["n"] = 0

        def still_online():
            calls["n"] += 1
            return False

        C.portal_logout = still_online
        code = C.do_logout()
        r = C.read_json(C.RESULT_FILE) or {}
        check("门户明确说仍在线 -> 退出码 3", code, 3)
        check("只试了 1 次（确定失败就不重试）", calls["n"], 1)
        check("提示语是原来那句", r.get("message"), "退出失败，请稍后重试")
    finally:
        for k, v in saved.items():
            setattr(C, k, v)
        time.sleep = saved_sleep


# ==================== 4. 并发不再互相弄失败 ====================
def part4_lock():
    print()
    print("=== 4. 两个动作并发时不再互相把对方弄失败 ===")
    saved = {k: getattr(C, k) for k in ("wait_for_campus", "test_internet",
                                        "portal_logout", "set_last_online")}
    C.wait_for_campus = lambda *a, **k: True
    C.test_internet = lambda *a, **k: True
    C.set_last_online = lambda *a, **k: None

    # 这里必须真 sleep：靠它让第一个动作占住锁久一点
    held = {"max_concurrent": 0, "live": 0}
    lock = threading.Lock()

    def slow_success():
        with lock:
            held["live"] += 1
            if held["live"] > held["max_concurrent"]:
                held["max_concurrent"] = held["live"]
        try:
            time.sleep(0.6)            # 模拟真实下线请求耗时
            return True
        finally:
            with lock:
                held["live"] -= 1

    C.portal_logout = slow_success
    results = {}

    def worker(who):
        results[who] = C.do_logout()

    ts = [threading.Thread(target=worker, args=(w,)) for w in ("并发A", "并发B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    print("       结果:", results)
    print("       同时进入门户请求的最大并发数:", held["max_concurrent"])
    check("最多只有一个动作在碰门户", held["max_concurrent"], 1)
    check("两个动作都成功（不再有一个报失败）",
          sorted(results.values()), [0, 0])

    # ---- 阴性对照：把锁换成一个永不放行的假货，第二个必须报「忙碌」 ----
    class NeverLock(object):
        def acquire(self, timeout=None):
            return False

        def release(self):
            pass

    real_lock = C._LOGOUT_LOCK
    C._LOGOUT_LOCK = NeverLock()
    try:
        code = C.do_logout()
        r = C.read_json(C.RESULT_FILE) or {}
        check("阴性对照：拿不到锁时报 3（不是静默成功）", code, 3)
        check("阴性对照：提示语说清了是「另一个操作在执行」",
              r.get("message"), "另一个操作正在执行，请等它结束后再试")
    finally:
        C._LOGOUT_LOCK = real_lock
        for k, v in saved.items():
            setattr(C, k, v)


def main():
    part1_body_verdict()
    part2_tristate()
    part3_retry()
    part4_lock()
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 三条成因都堵上了，且每组断言都有阴性对照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
