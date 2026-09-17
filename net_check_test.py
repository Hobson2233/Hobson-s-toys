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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C

FAILS = []
PORTAL_PAGE = "<html><body>请先登录校园网</body></html>"


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
    if url == C.CHECK_TARGETS[0][0]:
        raise OSError("timed out")
    return _ok_for(url)


def fake_primary_hijacked(url, timeout=10, headers=None):
    """主目标被劫持，备用目标正常 —— 不能因为主目标挂了就判未认证"""
    if url == C.CHECK_TARGETS[0][0]:
        return FakeResp(200, PORTAL_PAGE)
    return _ok_for(url)


def fake_only_primary_blocked(url, timeout=10, headers=None):
    """只有主目标给出「被拦截」的证据，其余全部连不上"""
    if url == C.CHECK_TARGETS[0][0]:
        return FakeResp(200, PORTAL_PAGE)
    raise OSError("timed out")


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
    check("有正文校验 + 状态码 500 -> None",
          run_with_fake_http(lambda *a, **k: FakeResp(500, ""),
                             lambda: C._probe(C.CHECK_TARGETS[0][0],
                                              C.CHECK_TARGETS[0][1], 3)), None)
    check("有正文校验 + 200 但正文不符 -> False",
          run_with_fake_http(lambda *a, **k: FakeResp(200, PORTAL_PAGE),
                             lambda: C._probe(C.CHECK_TARGETS[0][0],
                                              C.CHECK_TARGETS[0][1], 3)), False)
    check("只看状态码 + 204 -> True",
          run_with_fake_http(lambda *a, **k: FakeResp(204, ""),
                             lambda: C._probe(C.CHECK_TARGETS[2][0], None, 3)), True)
    check("只看状态码 + 200 带正文 -> None（没有正文可校验，不当证据）",
          run_with_fake_http(lambda *a, **k: FakeResp(200, PORTAL_PAGE),
                             lambda: C._probe(C.CHECK_TARGETS[2][0], None, 3)), None)

    print()
    print("=== 5. 切换前是否下线的决策 ===")
    check("未认证(False) -> 跳过下线",
          C.need_logout_before_switch(False), False)
    check("已联网(True) -> 先下线",
          C.need_logout_before_switch(True), True)
    check("测不出来(None) -> 先下线（宁可多下一次）",
          C.need_logout_before_switch(None), True)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
