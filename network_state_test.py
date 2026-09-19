"""验证 2026-09-19 修的两个问题。

背景（用户报告，多台电脑都出现）：
    1) 界面「是否连接校园网」不判断是不是校园网 —— 只要连上任何网络就显示
       「已连接校园网」。
    2) 开机自启生效慢，连上几分钟后莫名其妙掉线。

排查结论：
    1) 界面把 test_internet()（能不能上公网）直接当成 on_campus_network()
       （在不在校园网）显示。
    2) 开机那次登录，**门户在重定向 URL 里给了 mac=00:00:00:00:00:00**，
       portal_login 优先信任门户参数 → 拿全零 MAC 认证 → 认证能过但 BRAS
       事后对账对不上 → 几分钟后踢会话。日志证据：2026-09-19 11:49:49。

这个测试的重点是**每条判断都带阳性对照**：光验证「现在返回 True」证明不了
判断有效，必须构造一个能让它返回 False 的场景，确认它真的会失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  %s %-58s got=%r want=%r" % ("[OK]" if ok else "[!!]", name, got, want))


def section(t):
    print("\n=== %s ===" % t)


# ---------------------------------------------------------------- 1. valid_mac
section("1. valid_mac —— 什么算合法 MAC")
check("真 MAC 通过", C.valid_mac("84:9e:56:40:69:1d"), True)
check("大写+连字符 归一化后通过", C.valid_mac("84-9E-56-40-69-1D"), True)
check("全零被拒（这就是掉线的根因）", C.valid_mac("00:00:00:00:00:00"), False)
check("广播被拒", C.valid_mac("ff:ff:ff:ff:ff:ff"), False)
check("组播位被拒", C.valid_mac("01:00:5e:00:00:01"), False)
check("空串被拒", C.valid_mac(""), False)
check("None 被拒", C.valid_mac(None), False)
check("少一段被拒", C.valid_mac("84:9e:56:40:69"), False)
check("非十六进制被拒", C.valid_mac("zz:9e:56:40:69:1d"), False)
# 阳性对照的反面：本地管理位（0x02）必须**通过** —— Wi-Fi Direct 虚拟网卡用它，
# 实测本机有 86:9e:.. / 8a:9e:..。误拒了会拿不到 MAC。
check("本地管理位（Wi-Fi Direct）通过", C.valid_mac("86:9e:56:40:69:1d"), True)

# ------------------------------------------------------- 2. on_campus_network
section("2. on_campus_network —— 真机实测 + 阳性对照")
real = C.on_campus_network()
print("     本机出口 IP = %r，校园网段 = %r" % (C.local_ipv4(), C.campus_subnets()))
check("本机在校园网（实测）", real, True)

# 阳性对照：把判据改成不可能满足，必须返回 False。
# 没有这一步，就分不清「判断正确」和「判断恒为 True」。
_orig_subnets, _orig_portal = C.campus_subnets, C.portal_reachable
try:
    C.campus_subnets = lambda: ["203.0.113."]        # TEST-NET-3，永远不可能是本机
    C.portal_reachable = lambda *a, **k: False
    check("阳性对照：改成不可能网段+门户不可达 → False", C.on_campus_network(), False)
finally:
    C.campus_subnets, C.portal_reachable = _orig_subnets, _orig_portal

# 再验证「兜底」那条路真的生效：网段对不上、但门户可达 → 仍判在校园网
try:
    C.campus_subnets = lambda: ["203.0.113."]
    C.portal_reachable = lambda *a, **k: True
    check("兜底：网段对不上但门户可达 → True", C.on_campus_network(), True)
finally:
    C.campus_subnets, C.portal_reachable = _orig_subnets, _orig_portal

# ---------------------------------------------------------- 3. network_state
section("3. network_state —— 本次修复的核心回归")
# 先把原件存下来：下面要反复替换这些函数，最后必须原样还回去。
_orig_on, _orig_ti = C.on_campus_network, C.test_internet
_orig_session = C.Session
_orig_subnets2, _orig_portal2 = C.campus_subnets, C.portal_reachable
# 这就是用户报的 bug：不在校园网，但能上公网时，**绝不能**报「已连接校园网」。
try:
    C.on_campus_network = lambda *a, **k: False
    C.test_internet = lambda *a, **k: True
    check("不在校园网 + 能上网 → 未连接校园网（旧版会误报已连接）",
          C.network_state(), C.NET_OFF_CAMPUS)

    C.test_internet = lambda *a, **k: False
    check("不在校园网 + 被拦截 → 未连接校园网",
          C.network_state(), C.NET_OFF_CAMPUS)

    C.on_campus_network = lambda *a, **k: True
    C.test_internet = lambda *a, **k: True
    check("在校园网 + 已认证 → 已连接校园网", C.network_state(), C.NET_OK)

    C.test_internet = lambda *a, **k: False
    check("在校园网 + 被拦截 → 已连校园网未认证", C.network_state(), C.NET_NEED_LOGIN)

    C.test_internet = lambda *a, **k: None
    check("在校园网 + 测不出来 → 未知", C.network_state(), C.NET_UNKNOWN)

    # 阳性对照：确认上面这组断言**有能力失败** ——
    # 把 on_campus_network 换成「恒 True」，第一条就必须变成 NET_OK。
    C.on_campus_network = lambda *a, **k: True
    C.test_internet = lambda *a, **k: True
    check("阳性对照：判据恒 True 时 → 变成已连接（证明断言有效）",
          C.network_state(), C.NET_OK)
finally:
    C.on_campus_network = _orig_on
    C.test_internet = _orig_ti

# ------------------------------------------------- 4. portal_login 的 MAC 替换
section("4. portal_login —— 门户给全零 MAC 时必须换掉")


class FakeResp(object):
    def __init__(self, url="", text=""):
        self.url = url
        self.text = text
        self.status_code = 200


class FakeSession(object):
    """记录 POST 出去的参数，不发真请求。"""
    last = {}

    def __init__(self, headers=None):
        pass

    def get(self, url, timeout=10):
        return FakeResp(url=FakeSession.redirect_url)

    def post(self, url, data=None, timeout=10):
        FakeSession.last = {"url": url, "data": dict(data or {})}
        return FakeResp(url=url, text="<html>ok</html>")


def run_login(redirect_mac):
    """跑一次 portal_login，返回它实际 POST 出去的 mac。"""
    FakeSession.redirect_url = (
        "http://202.196.169.166/webauth.do?wlanacip=10.0.1.10&wlanacname=ZZHK-ZAX-BRAS"
        "&wlanuserip=10.15.116.156&mac=%s&vlan=0&url=http://1.1.1.1/" % redirect_mac)
    C.Session = FakeSession
    C.test_internet = lambda *a, **k: True          # 第一次探测就成功，别等
    C.portal_login("2026000001", "x")
    return FakeSession.last["data"].get("mac")


_real = C.local_mac()
try:
    C.local_mac = lambda: "84:9e:56:40:69:1d"
    got = run_login("00:00:00:00:00:00")
    check("门户给全零 → POST 的是真 MAC（这就是修复点）", got, "84:9e:56:40:69:1d")

    # 阳性对照：门户给合法 MAC 时必须**原样保留**，不能无脑覆盖。
    got = run_login("aa:bb:cc:dd:ee:01")
    check("门户给合法 MAC → 原样保留", got, "aa:bb:cc:dd:ee:01")

    # 门户给畸形值也要换掉
    got = run_login("not-a-mac")
    check("门户给畸形值 → 换成真 MAC", got, "84:9e:56:40:69:1d")

    # 两边都拿不到时：不崩，且**原样透传门户的值**（不凭空编造）。
    # 为什么不是返回空串：空 mac 会被门户直接拒登，用户彻底上不了网；
    # 透传至少还能用几分钟，而且日志里已经留了醒目告警。
    # 这是极罕见的分支（psutil + getmac 两条路同时失败），所以选「不更糟」而非「更严格」。
    C.local_mac = lambda: ""
    got = run_login("00:00:00:00:00:00")
    check("门户全零且本机也拿不到 → 不崩，原样透传（不编造）",
          got, "00:00:00:00:00:00")
finally:
    C.local_mac = _real
    C.Session = _orig_session
    C.test_internet = _orig_ti

# ------------------------------------------------------------------ 5. 代理
section("5. 会话必须绕开系统代理（2026-09-19 新发现）")
# 背景：urllib 的默认 opener 会吃系统代理 —— 既读 HTTP_PROXY 环境变量，
# 在 Windows 上还读 IE/WinINET 注册表。本机注册表里就躺着
# ProxyServer=127.0.0.1:7897（Clash 默认端口），而 ProxyOverride 的绕过列表
# 只含 10.* / 192.168.* / <local>，**不含门户的 202.196.169.166**。
# 于是开机时若 ProxyEnable=1 而 Clash 还没起来，门户请求全部超时 ——
# 正是「开机自启生效相当慢」的现场。updater.py 早就绕开了，门户会话当时漏了。
import urllib.request  # noqa: E402

_env_key = "HTTP_PROXY"
_old_env = os.environ.get(_env_key)
try:
    os.environ[_env_key] = "http://127.0.0.1:65001"
    # 阳性对照：默认 opener 必须**被污染**，否则下面那条断言证明不了任何事。
    _d = urllib.request.build_opener()
    _dph = [h for h in _d.handlers
            if isinstance(h, urllib.request.ProxyHandler)]
    check("阳性对照：默认 opener 里确实有 ProxyHandler",
          len(_dph), 1)
    check("阳性对照：它真的吃进了 HTTP_PROXY",
          "65001" in str(_dph[0].proxies), True)

    # ⚠️ 这里断言的是「**没有** ProxyHandler」，不是「有一个空代理表的」。
    #    build_opener(ProxyHandler({})) 会把 ProxyHandler **整个丢掉** ——
    #    OpenerDirector.add_handler 里 `proxy_open` 被当成「名字撞车」跳过，
    #    于是它一个协议方法都不贡献，压根不进 handlers 列表。
    #    效果正是我们想要的（完全不查代理），但一开始我按「有空代理表」去断言，
    #    得到 len==0 就报了假失败。
    _s = C.Session({})
    _sph = [h for h in _s.opener.handlers
            if isinstance(h, urllib.request.ProxyHandler)]
    check("Session 的 opener 里没有 ProxyHandler（=完全不查代理）", len(_sph), 0)
    # 更新器走的是另一套代码，两边都得绕开，否则「检查更新正常、登录全超时」
    # 这种「一半能通」最难查。
    import updater  # noqa: E402
    _u = updater._opener()
    _uph = [h for h in _u.handlers
            if isinstance(h, urllib.request.ProxyHandler)]
    check("updater 的 opener 同样没有 ProxyHandler", len(_uph), 0)
finally:
    if _old_env is None:
        os.environ.pop(_env_key, None)
    else:
        os.environ[_env_key] = _old_env


# ------------------------------------------------------------------ 汇总
print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
sys.exit(1 if FAIL else 0)
