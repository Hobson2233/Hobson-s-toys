# -*- coding: utf-8 -*-
"""多源下载（0.4.4）与下载进度节流的**确定性**测试。

为什么不用真网络（这是和 `updater_test.py` 的分工）：
    真网络测的是「这台机器现在到哪个源快」—— 那个结论对别人没用，
    而且受限会话里连回环都会被丢弃。这里要测的是**规则**：
    「按速度排完序之后，快的先试、失败要换源、全失败要报出来」。
    规则用假 opener 喂确定的数据就能验，跟网络无关。

两个被钉住的东西：

  1. `updater.download_multi()` 的选路与回退。**假 opener** 按 URL 分发假响应，
     快的那个源故意给错内容 → 必须自动换到慢的那个源并成功。

  2. 🔴 下载进度**换了一轮必须从 0 重新报**（2026-10-08 修，0.5.0 加固）。
     原实现把「上次报的百分比」挂在回调函数对象上，跨重试不重置 ——
     于是第 2 轮在 0% ~ 上次那个百分比之间**一条进度都不报**，
     界面卡住不动，看着就是死机。这条只在「下载中途失败然后重试」时出现，
     手动几乎撞不上，所以必须由脚本钉住。

     0.5.0 把判据从「调用方记得重置」改成「函数自己认出进度回退了」——
     因为「每个重启点都记得调」正是这个 bug 的成因（漏一处就复发，
     换源那处就一直漏着）。所以第 7 节现在钉的是**两件事**：
     ① 不手动重置，第 2 轮也要从头完整报一遍；
     ② 🔴 **正常前进时不许重置** —— 判据若写成 `pct <= last`，
        同一个百分比重复一次就归零，进度条会永远停在 3%。

用法：
    python updater_multisource_test.py
"""
import hashlib
import os
import sys
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import campus_login as C       # noqa: E402  （模块级 import 不建窗口，pwd_test 同样做法）
import updater as U            # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-52s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print("  [%s] %-52s %s" % ("OK" if cond else "失败", name, detail))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 假网络
#
# 只实现 updater 真正用到的那几个接口：status / headers.get / read / 上下文管理。
# 少了任何一个都会让被测代码走进 except 分支 —— 那样测试会「通过」但什么都没验到。

class FakeResp(object):
    def __init__(self, body, delay=0.0, status=200):
        self._body = body
        self._pos = 0
        self._delay = delay
        self.status = status
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n=-1):
        if self._pos >= len(self._body):
            return b""
        if n is None or n < 0:
            n = len(self._body) - self._pos
        # 每次 read 睡一会儿 —— 这样 _probe 量出来的「速度」才是可控的。
        # 不睡的话 dt≈0，速度快到没边，两个源分不出先后。
        if self._delay:
            time.sleep(self._delay)
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener(object):
    """按 URL 分发假响应。plan = {url: (内容, 每次 read 的延时)}。

    不在 plan 里的 URL 抛 URLError —— 模拟「这个源连不上」，
    用来验「探测失败不会让这个源出局」。
    """

    def __init__(self, plan):
        self.plan = plan

    def open(self, req, timeout=None):
        url = getattr(req, "full_url", req)
        if url not in self.plan:
            raise urllib.error.URLError("连不上（假）")
        body, delay = self.plan[url]
        return FakeResp(body, delay)


FAST = "https://fast.example/dl/app.exe"      # 快，但内容故意是错的
SLOW = "https://slow.example/dl/app.exe"      # 慢，内容是**正确**的
DEAD = "https://dead.example/dl/app.exe"      # 连不上

GOOD = b"GOOD-BYTES" * 2048                   # 真实的「安装包」内容
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()
BAD = b"EVIL-BYTES" * 2048                    # 长度一样，但内容不同 → 校验必失败

# 进度该报出来的完整序列：每 3% 一次，外加**一定报**的 100%。
# （100 不是 3 的倍数，靠 `pct != 100` 那个例外放行 —— 少了它用户永远看不到
#   「下载完成」，只能看到 99%。）
EXPECT_SEQ = ["下载中 %d%%" % p for p in list(range(3, 100, 3)) + [100]]


def part1_probe_order():
    print("=== 1. 并发测速：快的排前面，连不上的不丢 ===")
    plan = {
        FAST: (GOOD, 0.0),          # 不睡 → 极快
        SLOW: (GOOD, 0.004),        # 每次 read 睡 4ms → 慢
        DEAD: None,
    }
    del plan[DEAD]                  # 不放进去 = 连不上
    U._opener = lambda: FakeOpener(plan)
    try:
        t0 = time.time()
        pairs = U.probe_sources([SLOW, DEAD, FAST])
        dt = time.time() - t0
        hosts = [u.split("//", 1)[-1].split("/", 1)[0] for u, _s in pairs]
        check("返回顺序与输入一致（排序是下一步的事）", hosts,
              ["slow.example", "dead.example", "fast.example"])
        check_true("快的那条量出了速度", pairs[2][1] is not None,
                   "%.2f MB/s" % ((pairs[2][1] or 0) / 1048576.0))
        check_true("慢的那条也量出了速度", pairs[0][1] is not None,
                   "%.2f MB/s" % ((pairs[0][1] or 0) / 1048576.0))
        check("连不上的那条是 None（不是 0，0 会被当成「很慢但能用」）",
              pairs[1][1], None)

        order = U.order_by_speed(pairs)
        check("排序：快 → 慢 → 连不上", order, [FAST, SLOW, DEAD])
        check("★ 连不上的源仍在列表里（只是排最后）", len(order), 3)
        # 并发而不是串行：串行的话连不上的那个要等满 PROBE_TIMEOUT 才轮到下一个。
        check_true("探测是并发的（总耗时远小于串行）", dt < U.PROBE_TIMEOUT,
                   "耗时 %.2fs，串行至少要 %.0fs" % (dt, U.PROBE_TIMEOUT))
    finally:
        U._opener = _real_opener


def part2_pick_and_fallback():
    print()
    print("=== 2. 多源下载：快的先试，内容不对就换源 ===")
    plan = {FAST: (BAD, 0.0), SLOW: (GOOD, 0.004)}
    U._opener = lambda: FakeOpener(plan)
    dest = os.path.join(_tmpdir, "app.exe.new")
    picks = []
    try:
        ok, reason, used = U.download_multi(
            [SLOW, FAST], dest, sha256=GOOD_SHA, size=len(GOOD),
            on_pick=lambda u, n, t: picks.append((u, n, t)))
        check("下载成功（换到了对的源）", ok, True)
        check("★ 用的是内容正确的那个源（不是「先写的那个」）", used, SLOW)
        check("★ on_pick 报了两次（第 1 次是快的那个，失败后才换）",
              [(u, n, t) for u, n, t in picks], [(FAST, 1, 2), (SLOW, 2, 2)])
        with open(dest, "rb") as f:
            got = f.read()
        # 比哈希而不是比内容 —— 内容不等时 check() 会把 20KB 的字节全打出来。
        check("落盘内容正确", hashlib.sha256(got).hexdigest(), GOOD_SHA)
        check("大小与清单一致", len(got), len(GOOD))
    finally:
        U._opener = _real_opener

    print()
    print("=== 3. 全部失败：必须报出原因和「最后试的那个源」 ===")
    plan = {FAST: (BAD, 0.0), SLOW: (BAD, 0.004)}
    U._opener = lambda: FakeOpener(plan)
    dest = os.path.join(_tmpdir, "app2.exe.new")
    try:
        ok, reason, used = U.download_multi([SLOW, FAST], dest,
                                            sha256=GOOD_SHA, size=len(GOOD))
        check("失败", ok, False)
        check_true("原因里带上了每个源（不是一句干巴巴的「失败」）",
                   "fast.example" in reason and "slow.example" in reason, reason)
        check("★ 第三个返回值是最后试的那个源（界面拿它开浏览器）", used, SLOW)
        check("失败后不留残骸", os.path.exists(dest), False)
        check("失败后也不留 .part",
              os.path.exists(dest + ".part"), False)
    finally:
        U._opener = _real_opener

    print()
    print("=== 4. 只有一个源时不做探测（老清单的行为不能变） ===")
    plan = {SLOW: (GOOD, 0.0)}
    U._opener = lambda: FakeOpener(plan)
    dest = os.path.join(_tmpdir, "app3.exe.new")
    try:
        ok, _reason, used = U.download_multi([SLOW], dest,
                                             sha256=GOOD_SHA, size=len(GOOD))
        check("单源照常能下", ok, True)
        check("用的就是它", used, SLOW)
    finally:
        U._opener = _real_opener

    print()
    print("=== 5. 老清单（只有 url）走 evaluate → candidate_urls 仍然是单源 ===")
    st, info = U.evaluate({"version": "9.9.9", "url": SLOW,
                           "sha256": GOOD_SHA, "size": len(GOOD)}, "0.0.1")
    check("判定为有新版", st, U.STATE_NEWER)
    check("urls 退化成一项", U.candidate_urls(info), [SLOW])


def part3_progress():
    print()
    print("=== 6. 进度节流：每 3% 一次、100% 一定报 ===")
    st = {"last": -1, "t0": 0.0}
    seq = [x for x in (C.dl_progress_text(st, g, 100, 0.0) for g in range(0, 101)) if x]
    check("0~100 报出来的序列", seq, EXPECT_SEQ)

    st = {"last": -1, "t0": 0.0}
    check("总量未知时不报（pct 恒为 0）", C.dl_progress_text(st, 12345, None), None)

    print()
    print("=== 7. 🔴 换了一轮，进度必须从 0 重新报（2026-10-08 修，0.5.0 加固） ===")
    # 第 1 轮：下到 60% 就断了
    st = {"last": -1, "t0": 0.0}
    r1 = [x for x in (C.dl_progress_text(st, g, 100, 0.0) for g in range(0, 61)) if x]
    check("第 1 轮报到 60%", r1[-1], "下载中 60%")
    check("第 1 轮共 20 条", len(r1), 20)

    # ★ 0.5.0 加固点：**不手动重置**，第 2 轮也必须从头完整报一遍。
    #   原来靠「每个重启点都记得调 dl_progress_reset()」，而那正是 bug 的成因
    #   （漏一处就复发 —— 换源那处就一直漏着）。现在函数自己认得出
    #   「进度回退了 = 新一轮」。这一条就是那个「漏掉的重置」的回归。
    r2_auto = [x for x in (C.dl_progress_text(st, g, 100, 0.0)
                           for g in range(0, 101)) if x]
    check("★ 不手动重置，第 2 轮也从头完整报一遍", r2_auto, EXPECT_SEQ)
    check("★ 而且它的前 20 条就是第 1 轮报过的那些", r2_auto[:len(r1)], r1)

    # 🔴 反向对照：**正常前进时不许重置**。
    #   判据若写成 `pct <= state["last"]`，同一个百分比重复报一次就会归零，
    #   于是进度条永远停在第一个刻度上 —— 修好了「不报」，换来「只报一条」。
    st2 = {"last": -1, "t0": 0.0}
    check("前进到 50% → 报", C.dl_progress_text(st2, 50, 100, 0.0), "下载中 50%")
    check("同一个百分比重复 → 不报（节流）",
          C.dl_progress_text(st2, 50, 100, 0.0), None)
    check("  ★ 而且 last 没被清掉（清掉就等于每步都重置）", st2["last"], 50)

    # 显式重置仍然有效：给「还没收到任何进度就想先重置」的调用方用
    C.dl_progress_reset(st, 0.0)
    r2_ok = [x for x in (C.dl_progress_text(st, g, 100, 0.0)
                         for g in range(0, 101)) if x]
    check("★ 显式重置后，第 2 轮从头完整报一遍（和全新下载一样）", r2_ok, EXPECT_SEQ)
    check("★ 而且它的前 20 条就是第 1 轮报过的那些", r2_ok[:len(r1)], r1)

    print()
    print("=== 8. 进度里带速度（用户靠它区分「慢」和「卡死」）===")
    st = {"last": -1, "t0": 0.0}
    txt = C.dl_progress_text(st, 1048576, 4194304, 1.0)
    check("1 秒下了 1 MB / 共 4 MB", txt, "下载中 25%  1.0 MB/s")
    st = {"last": -1, "t0": 100.0}
    check("时间太短时不给速度（dt<=0.3 会被抖动放大）",
          C.dl_progress_text(st, 1048576, 4194304, 100.1), "下载中 25%")
    st = {"last": -1, "t0": 0.0}
    check("重试重置也把计时归零（否则速度会被上一轮的字节数撑大）",
          C.dl_progress_reset(st, 50.0) or st["t0"], 50.0)


def part4_browser_url():
    print()
    print("=== 9. 失败时该用浏览器打开哪个地址 ===")
    check("优先用这次实际试过的源", C.browser_fallback_url(
        {"url": "https://main/x"}, "https://tried/y"), "https://tried/y")
    check("没试过就退回清单主源",
          C.browser_fallback_url({"url": "https://main/x"}), "https://main/x")
    check("清单里也没有就退回发布站首页",
          C.browser_fallback_url({}), U.BASE_URL_HINT)
    check("info 是 None 也不炸", C.browser_fallback_url(None), U.BASE_URL_HINT)
    check("空字符串的 used_url 当作没给",
          C.browser_fallback_url({"url": "https://main/x"}, "   "), "https://main/x")


def main():
    global _tmpdir, _real_opener
    import tempfile
    _tmpdir = tempfile.mkdtemp(prefix="upd_multi_")
    _real_opener = U._opener          # 记住真的，最后要还回去
    try:
        part1_probe_order()
        part2_pick_and_fallback()
        part3_progress()
        part4_browser_url()
    finally:
        U._opener = _real_opener
        for r_, _ds, fs_ in os.walk(_tmpdir, topdown=False):
            for x in fs_:
                try:
                    os.remove(os.path.join(r_, x))
                except OSError:
                    pass
            for x in _ds:
                try:
                    os.rmdir(os.path.join(r_, x))
                except OSError:
                    pass
        try:
            os.rmdir(_tmpdir)
        except OSError:
            pass

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
