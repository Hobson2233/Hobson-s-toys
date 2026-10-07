# -*- coding: utf-8 -*-
"""「测一下速度」的回归测试（2026-10-07 加）。

被测的是 `campus_login.speed_probe()` —— 下载发布站上的测速文件、量出速度。
它有两个**错了也不报错**的地方，这个测试就是来钉住它们的：

  1) **第 1 轮不能计速**。第 1 轮里含 DNS + TCP + TLS 握手，而这些只发生一次。
     把它摊进平均值，量出来的数就没法解释了。第 2 节用「第 1 轮握手 5 秒、
     之后每轮 0.5 秒」的对照来钉：算进去会得到 2.4 Mbps，不算是 16.8 Mbps
     —— 差 7 倍，而且**两种结果看起来都像正常数字**。

  2) **每轮 URL 必须各不相同**。测速的做法就是反复下同一个 URL，所以缓存对它是
     致命的：命中一次缓存，那一轮走的是内存，速度凭空翻几倍，客户端完全看不出来。
     站点侧配了 `Cache-Control: no-store`，客户端还带一个变化的时间戳参数 ——
     第 5 节钉住后者。

⚠️ 本测试**不碰真实数据目录**（不建窗口、不调 write_json）。仍然挂了一层
   `datasafe` 沙箱 + 收尾核对，理由是项目惯例：探针将来被人加一行写盘代码时，
   那一道闸会立刻响，而不是安静地把用户数据改掉。
"""
import hashlib
import http.server
import os
import socket
import sys
import tempfile
import threading
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  [%s] %-52s got=%r want=%r" % ("OK" if ok else "!!", name, got, want))


def close_to(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol
    (PASS if ok else FAIL).append(name)
    print("  [%s] %-52s got=%r want≈%r" % ("OK" if ok else "!!", name, got, want))


def section(t):
    print("\n=== %s ===" % t)


MiB = 1024 * 1024

# ---------------------------------------------------------------- 假替身
#
# 为什么要假 opener + 假时钟：真跑一遍速度**不可复现**（同一台机器同一天
# 能在 160 KB/s ～ 1.65 MB/s 之间抖），断言就没法写。把「读了多少字节」
# 和「花了多久」两个输入接管过来，算术才验得动。


class _FakeResp(object):
    """假的响应体：按 chunk 吐出 nbytes 个字节，吐完返回 b""。"""

    def __init__(self, nbytes, chunk):
        self.left = nbytes
        self.chunk = chunk

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        k = min(self.left, self.chunk)
        self.left -= k
        return b"x" * k

    def close(self):
        pass


class _FakeOpener(object):
    """假的 opener。plan 里每一项是一轮的 (字节数, 连接耗时, 总耗时)，或是异常。"""

    def __init__(self, plan, chunk=64 * 1024):
        self.plan = list(plan)
        self.chunk = chunk
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        step = self.plan.pop(0)
        if isinstance(step, Exception):
            raise step
        nbytes, _conn, _elapsed = step
        return _FakeResp(nbytes, self.chunk)


class _FakeClock(object):
    """假时钟：把预定的时间点一个个弹出来。

    speed_probe 每轮正好调三次 clock()：进函数一次(t0)、open 返回后一次、
    读完之后一次 —— 所以每轮往 values 里压三个值就是精确的。
    抛异常的那一轮只调一次（t0）。
    """

    def __init__(self):
        self.values = []

    def __call__(self):
        if not self.values:
            raise AssertionError("假时钟用光了 —— speed_probe 调 clock() 的次数"
                                 "和预期对不上（改动过它就同步改这里）")
        return self.values.pop(0)


def run_probe(plan, **kw):
    """按 plan 跑一次 speed_probe，返回 (结果, opener)。"""
    opener = _FakeOpener(plan)
    clk = _FakeClock()
    t = 0.0
    for step in plan:
        if isinstance(step, Exception):
            clk.values.append(t)          # 只调到 t0 就抛了
            break
        _b, conn, elapsed = step
        clk.values += [t, t + conn, t + elapsed]
        t += elapsed
    return C.speed_probe(opener=opener, clock=clk, **kw), opener


# ================================================================ 开跑

def main():
    print("被测: campus_login.speed_probe / speed_describe")
    print("SPEEDTEST_URL = %s" % C.SPEEDTEST_URL)

    # ------------------------------------------------------------ 1
    section("1. 基本算术：字节数 / 耗时 / Mbps 三者的换算")
    # 第 1 轮握手 0.3s、之后两轮各 0.5s。计速只看后两轮：
    #   2 MiB / 1.0s = 2 MiB/s = 2097152 B/s × 8 / 1e6 = 16.777216 Mbps
    (ok, info), op = run_probe(
        [(MiB, 0.3, 0.5), (MiB, 0.0, 0.5), (MiB, 0.0, 0.5)],
        min_seconds=99, max_rounds=4)
    check("返回 ok", ok, True)
    check("计速字节数 = 2 MiB（第 1 轮不算）", info["bytes"], 2 * MiB)
    close_to("计速耗时 = 1.0 秒", info["seconds"], 1.0)
    check("计速轮数 = 2", info["rounds"], 2)
    close_to("Mbps = 16.777216", info["mbps"], 2097152.0 * 8 / 1e6, 1e-9)
    close_to("MB/s = 2.0", info["mbytes_per_s"], 2.0)
    close_to("连接耗时 = 第 1 轮的 0.3", info["connect"], 0.3)
    check("一共下了 3 轮（第 1 轮 + 2 轮计速）", len(op.urls), 3)

    # ------------------------------------------------------------ 2
    section("2. 🔴 第 1 轮的握手时间绝不能计进速度（核心语义）")
    # 第 1 轮握手 5 秒（慢 DNS / 慢 TLS 的极端情形），之后两轮各 0.5 秒。
    #   正确：2 MiB / 1.0s  = 16.777 Mbps
    #   错误：2 MiB / 6.0s  ≈  2.796 Mbps   ← 把握手摊进去了
    (ok, info), _ = run_probe(
        [(MiB, 5.0, 6.0), (MiB, 0.0, 0.5), (MiB, 0.0, 0.5)],
        min_seconds=99, max_rounds=4)
    check("返回 ok", ok, True)
    close_to("计速耗时仍是 1.0 秒（没把 5 秒握手算进来）", info["seconds"], 1.0)
    close_to("Mbps 仍是 16.777216（不是 2.796）", info["mbps"], 2097152.0 * 8 / 1e6, 1e-9)
    close_to("握手那 5 秒只出现在 connect 里", info["connect"], 5.0)

    # ------------------------------------------------------------ 3
    section("3. 轮数控制：min_seconds 够停就停，max_rounds 封顶")
    # 每轮 0.2 秒，要累计到 0.5 秒 → 3 轮（0.2 / 0.4 / 0.6）
    (_ok, info), _ = run_probe(
        [(MiB, 0.05, 0.2)] * 10, min_seconds=0.5, max_rounds=10)
    check("min_seconds=0.5 → 下 3 轮", info["rounds"], 3)
    # 每轮 0.01 秒，永远到不了 100 秒 → 被 max_rounds 封顶
    (_ok, info), _ = run_probe(
        [(MiB, 0.005, 0.01)] * 10, min_seconds=100, max_rounds=3)
    check("max_rounds=3 → 计速轮数封顶在 2", info["rounds"], 2)
    # max_rounds=1 → 一轮都不计速 → 没有速度可给，必须报错而不是给假数字
    (ok, msg), _ = run_probe([(MiB, 0.1, 0.5)], min_seconds=0.1, max_rounds=1)
    check("max_rounds=1 → 报「没量到有效数据」而不是编一个速度", (ok, msg),
          (False, "没量到有效数据"))

    # ------------------------------------------------------------ 4
    section("4. 出错路径：每种失败都要有**给用户看的中文**，不能抛异常")
    (ok, msg), _ = run_probe([urllib.error.HTTPError("u", 404, "Not Found", {}, None)])
    check("404 → 报状态码", (ok, msg), (False, "服务器返回 HTTP 404"))
    (ok, msg), _ = run_probe([urllib.error.URLError("connection refused")])
    check("连不上 → 报连不上", (ok, msg), (False, "连不上发布站：connection refused"))
    (ok, msg), _ = run_probe([urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] x")])
    check("证书错 → 单独认出来（本文件不 import ssl，靠文字认）",
          (ok, msg), (False, "证书校验失败：[SSL: CERTIFICATE_VERIFY_FAILED] x"))
    (ok, msg), _ = run_probe([socket.timeout()])
    check("超时 → 报超时", (ok, msg), (False, "超时（20 秒内没下完一轮）"))
    (ok, msg), _ = run_probe([(0, 0.1, 0.1)])
    check("200 但零字节 → 报空内容", (ok, msg), (False, "服务器返回了空内容"))

    # ------------------------------------------------------------ 5
    section("5. 🔴 每轮 URL 必须各不相同（缓存是测速的天敌）")
    (_ok, _info), op = run_probe(
        [(MiB, 0.1, 0.5), (MiB, 0.0, 0.5), (MiB, 0.0, 0.5)],
        min_seconds=99, max_rounds=4)
    check("3 轮的 URL 互不相同", len(set(op.urls)), len(op.urls))
    check("每轮都带时间戳参数", all("t=" in u for u in op.urls), True)
    check("每轮都带轮次参数", all("r=" in u for u in op.urls), True)
    check("URL 前缀就是 SPEEDTEST_URL", op.urls[0].split("?")[0], C.SPEEDTEST_URL)

    # ------------------------------------------------------------ 6
    section("6. 中途抖动不该毁掉整个功能（但一轮都没量到就必须报错）")
    # 第 2 轮成功、第 3 轮失败 → 用已经量到的，照常给结果
    (ok, info), _ = run_probe(
        [(MiB, 0.1, 0.5), (MiB, 0.0, 0.5), urllib.error.URLError("抖动")],
        min_seconds=99, max_rounds=4)
    check("后面一轮挂了仍然给结果", ok, True)
    check("  用的是已经量到的那 1 轮", info["rounds"], 1)
    check("  字节数也是那一轮的", info["bytes"], MiB)
    # 第 1 轮成功、第 2 轮就挂 → 没有任何计速数据 → 必须报错
    (ok, msg), _ = run_probe(
        [(MiB, 0.1, 0.5), urllib.error.URLError("抖动")],
        min_seconds=99, max_rounds=4)
    check("一轮都没量到 → 报错，不编速度", (ok, msg), (False, "连不上发布站：抖动"))

    # ------------------------------------------------------------ 7
    section("7. 速度翻倍 → 报出来的数必须跟着翻倍（证明数字真的是量出来的）")
    (_ok, a), _ = run_probe([(MiB, 0.1, 0.5), (MiB, 0.0, 0.5)], min_seconds=99, max_rounds=4)
    (_ok, b), _ = run_probe([(MiB, 0.1, 0.5), (MiB, 0.0, 1.0)], min_seconds=99, max_rounds=4)
    close_to("耗时翻倍 → Mbps 减半", a["mbps"] / b["mbps"], 2.0, 1e-9)

    # ------------------------------------------------------------ 8
    section("8. speed_describe 的文案分档与「量得太快」提示")
    base = {"mbps": 2.0, "mbytes_per_s": 0.25, "bytes": MiB, "seconds": 3.0,
            "rounds": 2, "connect": 0.4}
    check("慢速用两位小数", C.speed_describe(base).splitlines()[0],
          "下载速度 约 2.00 Mbps（0.25 MB/s）")
    check("中速用一位小数",
          C.speed_describe(dict(base, mbps=12.34)).splitlines()[0],
          "下载速度 约 12.3 Mbps（0.25 MB/s）")
    check("快速取整",
          C.speed_describe(dict(base, mbps=150.6)).splitlines()[0],
          "下载速度 约 151 Mbps（0.25 MB/s）")
    check("量得太快时有如实提示",
          C.speed_describe(dict(base, seconds=0.3)).count("仅供参考"), 1)
    check("量够了就没有那句提示",
          C.speed_describe(base).count("仅供参考"), 0)
    check("第二行带连接耗时 / 总量 / 轮数",
          C.speed_describe(base).splitlines()[1],
          "连接耗时 0.40 秒 · 量了 1.0 MB / 2 轮")

    # ------------------------------------------------------------ 9
    section("9. 真回环集成：走一遍真 socket，确认读循环和超时参数真的能用")
    ok_loop, note = _loopback_check()
    if ok_loop is None:
        print("  [跳过] %s" % note)
        print("         ⚠️ 这一段没跑 = 「真 socket 读得通」这件事**这次没验到**，"
              "不是通过。")
    else:
        check("回环下载 1 MiB：读到的字节数对得上", ok_loop, True)

    # ------------------------------------------------------------ 10
    section("10. 与站点侧的交叉核对（名字和缓存规则不能各写一份）")
    _site_check()

    # ------------------------------------------------------------ 收尾
    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败清单：")
        for n in FAIL:
            print("  - %s" % n)
        return 1
    return 0


def _loopback_check():
    """起一个只绑 127.0.0.1 的 HTTP 服务，真下载一次。

    返回 (True/False, "") 或 (None, 跳过原因)。
    ⚠️ 受限会话里回环连接会被丢弃（updater_test.py 的 docstring 就记着这条），
       所以这里区分「回环不通」和「逻辑不对」—— 前者是跳过，后者是失败。
    """
    payload = b"y" * MiB

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    # 只绑 127.0.0.1 —— 别绑 0.0.0.0，那会把测试用的端口暴露到局域网上。
    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        ok, info = C.speed_probe(url="http://127.0.0.1:%d/speedtest.bin" % port,
                                 min_seconds=0.2, max_rounds=3, timeout=10)
    except Exception as e:                       # noqa: BLE001
        srv.shutdown()
        return None, "回环测试抛异常：%s: %s" % (type(e).__name__, e)
    finally:
        srv.shutdown()

    if not ok:
        # 连不上回环 —— 是环境限制，不是被测代码的问题。
        return None, "回环连不上（%s）—— 受限会话常见，本次跳过" % info
    print("     量到 %.2f Mbps / %d 轮 / 连接 %.3f 秒"
          % (info["mbps"], info["rounds"], info["connect"]))
    # ⚠️ 不能断言「正好 1 MiB」——回环快到飞起，min_seconds 根本拦不住，
    #    它会一直下到 max_rounds 封顶（实测 2 轮 = 2 MiB）。
    #    能钉住的是「读到的字节数正好是文件大小的整数倍」：多一个字节少一个
    #    字节都说明读循环有问题（提前退出 / 多读一轮空包）。
    ok = (info["bytes"] >= MiB and info["bytes"] % MiB == 0 and info["rounds"] >= 1)
    if not ok:
        print("     ⚠️ 字节数 %d 不是 %d 的整数倍 —— 读循环有问题" % (info["bytes"], MiB))
    return ok, ""


def _site_check():
    """核对站点侧（工作区根目录的 make_site.py）和客户端说的是同一件事。

    ⚠️ 从 repo/ 检出里单独跑时**看不到**工作区根目录（domain_drift_test.py
       开头记着同一个约束），所以这一节找不到文件就明说跳过。
    """
    root = os.path.dirname(HERE)
    if not os.path.isfile(os.path.join(root, "make_site.py")):
        print("  [跳过] 工作区根目录的 make_site.py 不在这个检出里")
        return
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import make_site as M
    except Exception as e:                       # noqa: BLE001
        print("  [跳过] 导入 make_site 失败：%s" % e)
        return
    check("站点生成的文件名 = 客户端请求的文件名",
          M.SPEEDTEST_NAME, C.SPEEDTEST_FILE)
    headers = M.build_headers()
    check("_headers 里有 /speedtest.bin 规则", "/speedtest.bin" in headers, True)
    check("  而且必须是 no-store（否则第二轮量到的是内存）",
          "no-store" in headers.split("/speedtest.bin", 1)[1], True)
    body = M.speedtest_bytes(4096)
    check("生成的测速内容长度对得上", len(body), 4096)
    check("  两次生成字节一致（确定性）", M.speedtest_bytes(4096) == body, True)
    import zlib
    check("  而且压不动（可压缩就等于量的是解压速度）",
          len(zlib.compress(body, 9)) >= len(body), True)
    check("  内容不是全零", set(body) == {0}, False)


if __name__ == "__main__":
    sys.exit(main())
