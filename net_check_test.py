# -*- coding: utf-8 -*-
"""联网检测 test_internet 的三态语义测试。

背景（这个 bug 的教训）：检测函数原来是「单地址 + 单次 + 只看真假」，把
「测不出来」和「确实没认证」都归成 False。后果不只是状态栏显示错：

  - do_auto   把「其实能上网」误判成未认证 → 多跑一次没必要的登录
  - do_switch 把「其实在线」误判成未认证 → 跳过下线，登录目标账号时被服务端
              回「此IP已在线请勿重复认证」，而 portal_login 只看「登录后能否
              上网」，能上网就报成功 → 切换静默失败（界面说成功，账号没变）
  - GUI 状态栏把「能上网」显示成「未连接校园网」

所以这里逐条验证三态语义、多地址兜底、以及切换前的决策函数。

用法：
    python net_check_test.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C

FAILS = []
PORTAL_PAGE = "<html><body>请先登录校园网</body></html>"


def _target_with_expect():
    """第一个「有正文校验」的目标。

    ⚠️ 用**特征**挑，不写死下标 —— 目标顺序是可变的事实（2026-10-10 刚从
       「微软优先」调成「国内优先」）。写死 [0] / [2] 的话，改一次顺序测试就全挂，
       而且挂的方式是「断言失败」，看起来像被测代码坏了，其实只是测试太脆。
    """
    for t in C.CHECK_TARGETS:
        if t[1]:
            return t
    raise AssertionError("CHECK_TARGETS 里没有带正文校验的目标")


def _target_no_expect():
    """第一个「只看状态码」的目标（generate_204 这类）。"""
    for t in C.CHECK_TARGETS:
        if not t[1]:
            return t
    raise AssertionError("CHECK_TARGETS 里没有只看状态码的目标")


class FakeResp(object):
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.url = "http://fake/"


def check(name, got, want):
    ok = got is want
    print("  [%s] %-46s -> %-5r (期望 %r)" % ("OK" if ok else "失败", name, got, want))
    if not ok:
        FAILS.append(name)


def run_with_fake_http(fake, fn):
    """把模块级 http_get 换成 fake，跑完必定恢复"""
    orig = C.http_get
    C.http_get = fake
    try:
        return fn()
    finally:
        C.http_get = orig


def _ok_for(url):
    for u, e, _n in C.CHECK_TARGETS:
        if url == u:
            return FakeResp(200, "..." + e + "...") if e else FakeResp(204, "")
    raise AssertionError("未预期的 url: %s" % url)


def fake_all_ok(url, timeout=10, headers=None):
    return _ok_for(url)


def fake_all_timeout(url, timeout=10, headers=None):
    raise OSError("timed out")


def fake_all_hijacked(url, timeout=10, headers=None):
    return FakeResp(200, PORTAL_PAGE)


def fake_primary_timeout(url, timeout=10, headers=None):
    """主目标连不上，备用目标正常 —— 多地址兜底要救的就是这个场景"""
    if url == _target_with_expect()[0]:
        raise OSError("timed out")
    return _ok_for(url)


def fake_primary_hijacked(url, timeout=10, headers=None):
    """主目标被劫持，备用目标正常 —— 不能因为主目标挂了就判未认证"""
    if url == _target_with_expect()[0]:
        return FakeResp(200, PORTAL_PAGE)
    return _ok_for(url)


def fake_only_primary_blocked(url, timeout=10, headers=None):
    """只有主目标给出「被拦截」的证据，其余全部连不上"""
    if url == _target_with_expect()[0]:
        return FakeResp(200, PORTAL_PAGE)
    raise OSError("timed out")


def _check_timing(name, dt, limit):
    """耗时类断言。单独一个函数是因为输出要带**实测秒数**（不信注释只信量）。"""
    ok = dt < limit
    print("  [%s] %-46s -> %.3fs (上限 %.2fs)"
          % ("OK" if ok else "失败", name, dt, limit))
    if not ok:
        FAILS.append(name)


def _check_race():
    """并发探测（`_probe_race`）的专项用例 —— 2026-10-10 加入。

    🔴 为什么必须有这一节：上面 1~5 节只验证**判定结果**没变。而并发改造真正想
       解决的是**耗时**问题（用户报「开机后自动登录慢」）。只测结果不测耗时的话，
       将来谁把 `_probe_race` 退回串行，1~5 节照样全绿 —— 那这个 bug 就白修了。
       所以这里既测「谁给的结论」，也测「花了多久」。

    🔴 全部用假目标 + 假 http_get，**不碰真实网络**（不然测试会依赖外网，还会
       把别人服务器的响应速度当成自己的测试条件）。
    """
    tn = ("http://race-fast.test/generate_204", None, "fast")
    slow = ("http://race-slow.test/generate_204", None, "slow")
    targets = (slow, tn)          # ⚠️ 故意把慢的排在前面 —— 这正是要防的排列

    def fake_slow_first(url, timeout=10, headers=None):
        """慢目标 1.5 秒才回，快目标 0.05 秒就回（并发下总耗时应该 ≈ 0.05 秒）"""
        if "slow" in url:
            time.sleep(1.5)
        else:
            time.sleep(0.05)
        return FakeResp(204, "")

    # --- 1. 慢目标排在前，也不能拖慢整体 ---
    t0 = time.perf_counter()
    verdict, who = run_with_fake_http(
        fake_slow_first, lambda: C._probe_race(targets, 3.0))
    dt = time.perf_counter() - t0
    check("慢目标在前：判定仍是 True", verdict, True)
    check("慢目标在前：采用的是最快那个目标的结论", who, "fast")
    _check_timing("慢目标在前：总耗时 ≈ 最快者（串行会是 1.55s）", dt, 0.8)

    # --- 2. 端到端：test_internet 也不该被慢目标拖住 ---
    orig_ct = C.check_targets
    C.check_targets = lambda cfg=None: targets
    try:
        t0 = time.perf_counter()
        r = run_with_fake_http(fake_slow_first, C.test_internet)
        dt = time.perf_counter() - t0
    finally:
        C.check_targets = orig_ct
    check("端到端 test_internet：慢目标在前仍是 True", r, True)
    _check_timing("端到端 test_internet：总耗时 ≈ 最快者", dt, 0.8)

    # --- 3. 全部被门户拦截 -> False（并发不能把「被拦截」这个证据弄丢）---
    blocked_targets = (
        ("http://race-b1.test/x", "Expected Body", "b1"),
        ("http://race-b2.test/x", "Expected Body", "b2"),
    )
    verdict, who = run_with_fake_http(
        lambda *a, **k: FakeResp(200, PORTAL_PAGE),
        lambda: C._probe_race(blocked_targets, 3.0))
    check("全部被拦截 -> False", verdict, False)
    check("全部被拦截 -> 报出给出结论的目标名", who in ("b1", "b2"), True)

    # --- 4. blocked 优先于 None（哪怕 None 先到、blocked 后到）---
    mixed = (
        ("http://race-blocked.test/x", "Expected Body", "blocked"),
        ("http://race-timeout.test/x", None, "timeout"),
    )

    def fake_blocked_slow(url, timeout=10, headers=None):
        if "race-timeout" in url:
            raise OSError("timed out")       # 这个先返回，结论是 None
        time.sleep(0.3)                      # 这个后返回，结论是 False
        return FakeResp(200, PORTAL_PAGE)

    verdict, who = run_with_fake_http(
        fake_blocked_slow, lambda: C._probe_race(mixed, 3.0))
    check("blocked 优先于 None -> False", verdict, False)
    check("blocked 优先于 None -> 报出 blocked 那个目标", who, "blocked")

    # --- 5. 全部连不上 -> None ---
    verdict, who = run_with_fake_http(
        fake_all_timeout, lambda: C._probe_race(targets, 3.0))
    check("全部连不上 -> None", verdict, None)
    check("全部连不上 -> 不报目标名", who, "")

    # --- 6. 有目标卡死（模拟 DNS 解析卡住）也必须按时返回，不能挂住进程 ---
    # DNS 解析**不受 socket timeout 约束**，所以这里必须靠 deadline 兜底。
    def fake_hang(url, timeout=10, headers=None):
        if "slow" in url:
            time.sleep(30)                   # 远超 deadline（1.0 + 0.5 秒）
        raise OSError("timed out")

    t0 = time.perf_counter()
    verdict, _who = run_with_fake_http(
        fake_hang, lambda: C._probe_race(targets, 1.0))
    dt = time.perf_counter() - t0
    check("有目标卡死 -> 仍然给出结论（不挂住）", verdict, None)
    _check_timing("有目标卡死：deadline 兜底生效", dt, 2.0)


def main():
    print("检测目标:")
    for u, e, n in C.CHECK_TARGETS:
        print("  %-16s %s" % (n, u))
    print()

    print("=== 1. test_internet 三态语义 ===")
    check("全部正常 -> True",
          run_with_fake_http(fake_all_ok, C.test_internet), True)
    check("全部超时 -> None（测不出来，不能当未认证）",
          run_with_fake_http(fake_all_timeout, C.test_internet), None)
    check("全部被门户劫持 -> False（确实未认证）",
          run_with_fake_http(fake_all_hijacked, C.test_internet), False)

    print()
    print("=== 2. 多地址兜底（本次修复的核心）===")
    check("主目标超时、备用正常 -> True",
          run_with_fake_http(fake_primary_timeout, C.test_internet), True)
    check("主目标被劫持、备用正常 -> True",
          run_with_fake_http(fake_primary_hijacked, C.test_internet), True)

    print()
    print("=== 3. 「明确被拦截」优先于「测不出来」===")
    check("主目标被劫持、其余连不上 -> False（不能降级成 None）",
          run_with_fake_http(fake_only_primary_blocked, C.test_internet), False)

    print()
    print("=== 4. _probe 单点语义 ===")
    tw = _target_with_expect()      # 有正文校验的目标（msftconnecttest 这类）
    tn = _target_no_expect()        # 只看状态码的目标（generate_204 这类）
    check("有正文校验 + 状态码 500 -> None",
          run_with_fake_http(lambda *a, **k: FakeResp(500, ""),
                             lambda: C._probe(tw[0], tw[1], 3)), None)
    check("有正文校验 + 200 但正文不符 -> False",
          run_with_fake_http(lambda *a, **k: FakeResp(200, PORTAL_PAGE),
                             lambda: C._probe(tw[0], tw[1], 3)), False)
    check("只看状态码 + 204 -> True",
          run_with_fake_http(lambda *a, **k: FakeResp(204, ""),
                             lambda: C._probe(tn[0], None, 3)), True)
    check("只看状态码 + 200 带正文 -> None（没有正文可校验，不当证据）",
          run_with_fake_http(lambda *a, **k: FakeResp(200, PORTAL_PAGE),
                             lambda: C._probe(tn[0], None, 3)), None)

    print()
    print("=== 5. 切换前是否下线的决策 ===")
    check("未认证(False) -> 跳过下线",
          C.need_logout_before_switch(False), False)
    check("已联网(True) -> 先下线",
          C.need_logout_before_switch(True), True)
    check("测不出来(None) -> 先下线（宁可多下一次）",
          C.need_logout_before_switch(None), True)

    print()
    print("=== 6. 并发探测（_probe_race）===")
    _check_race()

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
