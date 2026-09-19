# -*- coding: utf-8 -*-
"""updater 的端到端测试。

为什么非要走真实网络而不是把 _get 打桩：
    fetch_manifest / download 里最容易错的是**网络那一层**（状态码、超时、
    Content-Length、编码、代理）。打桩等于把要测的东西测掉了。

⚠️ 本沙箱的**回环连接被丢弃**（实测：连自己的监听都超时，裸 urlopen 也一样），
   所以脚本会**自动探测**：回环可用就跑本地 HTTP 服务（最干净）；
   不可用就改打真实公网 HTTPS。两条路都跑不了就明确报「跳过」，不会假装通过。

最要紧的两条断言（都是本项目踩过的静默失败）：
    A. **网络失败必须报 unknown，绝不能报 current** ——
       否则用户看到「已是最新」，其实一次都没查成功。
    B. **sha256 不匹配必须拒绝，且不能把文件留在磁盘上**。

还有一条**测试自身的阳性对照**：故意让一个断言失败，确认这套测试真的会报错
（一个永远绿的测试等于没有测试）。

用法：python updater_test.py
退出码 0 = 全过（跳过的项会单独列出来）。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import updater as U            # noqa: E402

# ⚠️ 别在这里写死本机路径（C:\Users\<用户名>\...）—— 这是要提交的公开文件。
#    用 %LOCALAPPDATA% 之类的环境变量推出来，换台机器也是对的。
PYW = os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"),
                   ".workbuddy-ai", "binaries", "python", "versions",
                   "3.13.12", "pythonw.exe")
LOCAL_EXE = os.path.join(os.path.expanduser("~"), "Desktop", "校园网自动登录.exe")
ASSET_URL = ("https://github.com/Hobson2233/Hobson-s-toys/releases/download/"
             "v0.1.0/campus-login.exe")
# 真实存在但不存在的路径 —— 用来测 404 分支
# ⚠️ 路径必须是**纯 ASCII**。这里原来写的是 `contents/不存在.json`，
#    结果 urllib 在发请求之前就因为「URL 含非法字符」抛了 UnicodeEncodeError，
#    三条用例（404 / DNS / 端口）**全都撞在同一个分支上**，谁也没测到各自的分支。
#    断言虽然写着「404 → UNKNOWN」，但换成任何 URL 都能过 —— 典型的检查空转。
URL_404 = "https://api.github.com/repos/Hobson2233/Hobson-s-toys/contents/no-such-file-xyz.json"
URL_JSON = "https://api.github.com/repos/Hobson2233/Hobson-s-toys/releases/latest"

FAILS = []
SKIPS = []
CHECKS = [0]
TMP = ""


def ck(name, got, want):
    CHECKS[0] += 1
    if got == want:
        print("  [OK] %-50s -> %r" % (name, got))
        return True
    print("  [失败] %-48s -> %r   (期望 %r)" % (name, got, want))
    FAILS.append(name)
    return False


def ck_true(name, cond):
    return ck(name, bool(cond), True)


def skip(name, why):
    SKIPS.append("%s（%s）" % (name, why))
    print("  [跳过] %s —— %s" % (name, why))


# ------------------------------------------------------------ 本地 HTTP 服务

class Handler(BaseHTTPRequestHandler):
    routes = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        item = self.routes.get(self.path.split("?")[0])
        if item is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, ctype = item
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server(routes):
    Handler.routes = routes
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def loopback_works():
    """回环到底能不能用。不能就早点说，别让后面一堆断言报假失败。"""
    try:
        srv, base = start_server({"/ping": (b"pong", "text/plain")})
    except OSError:
        return False, ""
    try:
        ok, body = U._get(base + "/ping", 4)
        return (ok and body == b"pong"), base
    finally:
        srv.shutdown()


# ------------------------------------------------------------ 各段测试

def manifest_blob(base, version, blob, **over):
    m = {"version": version, "url": base + "/app.exe",
         "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob), "notes": "测试用"}
    m.update(over)
    return json.dumps(m, ensure_ascii=False).encode("utf-8")


def part1_loopback(blob):
    print("\n=== 1. 清单获取与判定（本地 HTTP 服务）===")
    srv, base = start_server({})
    try:
        routes = {
            "/v-new.json": (manifest_blob(base, "0.2.0", blob), "application/json"),
            "/v-same.json": (manifest_blob(base, "0.1.0", blob), "application/json"),
            "/v-bad-sha.json": (manifest_blob(base, "0.2.0", blob, sha256="x" * 64),
                                "application/json"),
            "/v-broken.json": (b"{ not json", "application/json"),
            "/app.exe": (blob, "application/octet-stream"),
        }
        Handler.routes = routes

        st, info = U.check("0.1.0", base + "/v-new.json")
        ck("有新版 → NEWER", st, U.STATE_NEWER)
        ck("  版本号", info.get("version"), "0.2.0")
        ck("  体积", info.get("size"), len(blob))

        st, _ = U.check("0.2.0", base + "/v-same.json")
        ck("同版本 → CURRENT", st, U.STATE_CURRENT)

        st, _ = U.check("0.1.0", base + "/v-bad-sha.json")
        ck("sha256 不合法 → UNKNOWN（拒绝下载）", st, U.STATE_UNKNOWN)

        st, _ = U.check("0.1.0", base + "/v-broken.json")
        ck("清单不是 JSON → UNKNOWN", st, U.STATE_UNKNOWN)

        st, _ = U.check("0.1.0", base + "/missing.json")
        ck("404 → UNKNOWN", st, U.STATE_UNKNOWN)
    finally:
        srv.shutdown()


def part1_realnet():
    print("\n=== 1'. 清单获取与判定（真实公网 HTTPS）===")
    ok, body = U._get("https://api.github.com/zen", 25)
    ck("真实 HTTPS 取得到内容", ok, True)
    if ok:
        print("       内容：%r" % body[:40])

    st, reason = U.check("0.1.0", URL_JSON)
    ck("拿到的 JSON 没有 version 字段 → UNKNOWN", st, U.STATE_UNKNOWN)
    print("       原因文字：%s" % reason)

    st, reason = U.check("0.1.0", URL_404)
    ck("真实 404 → UNKNOWN", st, U.STATE_UNKNOWN)
    ck_true("真实 404 → 原因里带 'HTTP 404'（不是别的分支）",
            "HTTP 404" in str(reason))
    print("       原因文字：%s" % reason)


def part_failure_is_not_current():
    """**整段代码存在的理由**：查不出来时必须报 unknown，不能报「已是最新」。

    ⚠️ 每条用例都额外断言**原因文字里出现它该有的关键词** ——
    光断言 `state == UNKNOWN` 是不够的：URL 写错（比如带了中文）会让
    所有用例都从「地址不合法」这个分支出去，看起来全绿，实际上
    404 分支、DNS 分支、端口分支**一个都没被跑到**。
    2026-09-18 实测踩到过：三条用例的原因文字全是 "ascii codec can't encode"。

    ⚠️ 但关键词断言有个副作用：**本机网络抖一下，它也会报失败**。
    2026-09-19 实测：连跑 5 轮，第 3 轮「真实 404」这条挂了 ——
    原因是那次 api.github.com 没通，reason 变成了连接类错误，
    于是「HTTP 404」当然不在里面。而它旁边的 `state == UNKNOWN` 是过的。
    这种假失败最坏的地方不是烦人，是**会教人忽略红色**：一旦习惯了
    「这条偶尔红」，真出 bug 时也会被当成抖动放过去。
    所以这里把两类「对不上」分开报（本文件 NETWORK_HINTS 那段注释讲的就是这个）：
      · 主机这次确实不通  → 记 skip，明说「这条没被验证」
      · 主机通、关键词却不对 → 那是代码分支走错，报失败
    """
    print("\n=== 2. 网络失败不能被当成「已是最新」（关键行为）===")

    # 独立探一次「这台主机本身通不通」。**必须独立探**，不能拿被测用例自己的
    # 结果去解释自己 —— 否则「代码把 404 误判成连接错误」也会被当成网络抖动 skip 掉，
    # 而那正是这一段要防的 bug。
    host_ok, host_body = U._get("https://api.github.com/zen", 10)
    print("  参照探测：api.github.com 这次%s（%s）"
          % ("通" if host_ok else "不通", (host_body or b"")[:28]))

    cases = [
        # (名字, URL, 原因文字里必须出现的关键词)
        ("真实 404", URL_404, "HTTP 404"),
        ("DNS 解析不了", "https://no-such-host-xyzabc123.invalid/v.json",
         "连接失败"),
        ("端口没人听", "http://127.0.0.1:1/v.json", "连接失败"),
        ("畸形 URL（未转义中文）", "https://api.github.com/中文.json",
         "地址不合法"),
    ]
    for name, url, must in cases:
        st, reason = U.check("0.1.0", url, timeout=8)
        ck_true("%s → 不是 CURRENT" % name, st != U.STATE_CURRENT)
        ck_true("%s → 不是 NEWER" % name, st != U.STATE_NEWER)
        ck("%s → 就是 UNKNOWN" % name, st, U.STATE_UNKNOWN)
        # 这一条才是真正区分各分支的断言。上面三条在「分支走错」时都会假绿。
        if must in str(reason):
            ck_true("%s → 原因里带 %r（证明走的是该走的分支）" % (name, must), True)
        elif not host_ok and any(h in str(reason) for h in NETWORK_HINTS):
            skip("%s → 分支关键字核对" % name,
                 "本机到那台主机这次不通，关键词核对没做成")
        else:
            ck_true("%s → 原因里带 %r（证明走的是该走的分支）" % (name, must), False)
        print("       %s" % reason)


# 出现这些字样说明是「网络没测成」，不是「代码错了」——
# 两者必须分开报：把「测不到」当失败会掩盖真 bug，当通过则更糟。
NETWORK_HINTS = ("连接失败", "ConnectionReset", "timed out", "超时", "HTTP 5",
                 "远程主机强迫关闭", "10054", "10060", "证书")

# 一个可靠的真实 HTTPS 目标（实测 3/3 成功）。用它验证下载机制本身。
RELIABLE_URL = "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.js"

# 3b 段（GitHub 资产比对）的外层硬闸门，单位秒。
# ⚠️ 这个值不是「网络超时」，是「我们愿意等多久」。见 part3_download 里那段注释：
#    沙箱代理会把 github.com 挂住且 urllib 的 timeout 不生效，必须有外部闸门兜底。
ASSET_PROBE_BUDGET = 40


def part3_download():
    """下载与校验。

    返回值：拿到参照文件时返回 (sha256, size)，否则 None —— 3c 段要拿它去
    构造「下载完了但大小不符」的真实场景（没有参照文件就没法构造）。
    """
    print("\n=== 3. 下载与校验 ===")
    print("  --- 3a. 机制验证（用可靠的 CDN 目标）---")

    probe = os.path.join(TMP, "probe.bin")
    # 本机 DNS 有瞬时抖动（实测 getaddrinfo 偶发失败，见记忆里的 DNS 主服务器故障），
    # 一次失败就跳过整段验证太可惜 —— 重试几次，仍失败才认。
    ok, reason = False, "未尝试"
    for attempt in range(4):
        ok, reason = U.download(RELIABLE_URL, probe, None, None, timeout=40)
        if ok:
            break
        print("       第 %d 次取参照文件失败：%s" % (attempt + 1, reason))
        time.sleep(1.5)
    if not ok:
        skip("下载机制验证", "取不到参照文件：" + reason)
        return None
    real_sha = U.sha256_file(probe)
    real_size = os.path.getsize(probe)
    print("       参照文件 %d 字节 sha256 %s…" % (real_size, real_sha[:12]))
    ref = (real_sha, real_size)

    d1 = os.path.join(TMP, "ok.bin")
    ok, reason = U.download(RELIABLE_URL, d1, real_sha, real_size, timeout=40)
    ck("哈希正确 → 通过", ok, True)
    if ok:
        ck("  内容与参照一致", U.sha256_file(d1), real_sha)
        ck("  没有残留 .part", os.path.exists(d1 + ".part"), False)

    d2 = os.path.join(TMP, "bad.bin")
    ok, reason = U.download(RELIABLE_URL, d2, "a" * 64, real_size, timeout=40)
    ck("哈希错误 → 拒绝", ok, False)
    print("       原因文字：%s" % reason)
    ck("  不留目标文件", os.path.exists(d2), False)
    ck("  不留 .part", os.path.exists(d2 + ".part"), False)

    d3 = os.path.join(TMP, "size.bin")
    ok, reason = U.download(RELIABLE_URL, d3, real_sha, real_size + 12345, timeout=40)
    ck("大小不符 → 拒绝", ok, False)
    print("       原因文字：%s" % reason)
    ck("  不留文件", os.path.exists(d3), False)

    seen = []
    d4 = os.path.join(TMP, "prog.bin")
    ok, _ = U.download(RELIABLE_URL, d4, real_sha, real_size, timeout=40,
                       on_progress=lambda g, t: seen.append((g, t)))
    ck("进度回调被调用", len(seen) > 0, True)
    if seen:
        print("       最后一条：%s" % U.format_progress(*seen[-1]))

    print("\n  --- 3b. 发布资产完整性（GitHub，本机网络不稳定，只报告不判失败）---")
    if not os.path.isfile(LOCAL_EXE):
        skip("发布资产比对", "本地没有桌面 exe 可比对")
        return ref
    local_sha = U.sha256_file(LOCAL_EXE)
    local_size = os.path.getsize(LOCAL_EXE)
    t0 = time.time()
    # ⚠️ 这一段必须自带**外层闸门**，不能只靠 urllib 的 timeout。
    #    实测（2026-09-18）：本机出站走沙箱代理（http_proxy=127.0.0.1:10048），
    #    `github.com` / `api.github.com` 都被**解析到 127.0.0.1**，于是请求打到
    #    本机代理上、代理不回数据。此时 urllib 的 `timeout=` **完全不生效** ——
    #    实测跑满 11 分钟都没返回，只能靠外部杀进程。
    #    ⇒ 用子线程 + join 做硬超时：到点就放弃、记一条 skip，整个测试还能继续往下走。
    #       否则 3b 会把 4/5/6 三段一起拖住，「跑了但没跑完」比「明确跳过」坏得多。
    result = {}

    def _probe_asset():
        try:
            result["v"] = U.download(ASSET_URL, os.path.join(TMP, "asset.exe"),
                                     local_sha, local_size, timeout=45)
        except Exception as e:                      # noqa: BLE001
            result["v"] = (False, "探测线程异常：%s" % e)

    th = threading.Thread(target=_probe_asset, daemon=True)
    th.start()
    th.join(ASSET_PROBE_BUDGET)
    dt = time.time() - t0
    if th.is_alive():
        # 线程还挂着（daemon，进程退出时会一起收掉）。明确记成 skip，别静默。
        skip("GitHub 发布资产比对",
             "本机取不到（%.0fs 外层闸门到点，线程仍未返回 —— 沙箱代理挂住）" % dt)
        print("       （注意：被放弃的那个线程还挂在网络调用里，属预期）")
        return ref
    ok, reason = result.get("v", (False, "没有结果"))
    if ok:
        print("       ✅ 从 GitHub 下载成功，与本地 exe 逐字节相同（%.1f MB / %.1f 秒，%.1f KB/s）"
              % (local_size / 1048576.0, dt, local_size / 1024.0 / max(dt, 0.01)))
    elif any(h in reason for h in NETWORK_HINTS):
        skip("GitHub 发布资产比对", "本机取不到（%.0fs 后 %s）" % (dt, reason))
    else:
        ck("GitHub 资产与本地 exe 一致", False, True)
        print("       原因：%s" % reason)
    return ref


def part3c_retry(ref):
    """下载重试（0.3.1 新增）。

    为什么必须单独测：`download()` 原先**一次重试都没有**，而这条路径要下 12 MB ——
    实测同一台机器 8 次下载失败 1 次，形态是连接级停顿（`TimeoutError`）。
    加重试之后最怕的错是「写了重试但没生效」：那种错在正常网络上**永远看不出来**
    （正常网络一次就成功，走不到重试分支），所以必须专门构造失败。

    分三段，失败来源各不相同：
      3c-A  策略矩阵 —— 打桩 _attempt，把「试几次 / 什么时候不试 / 回调怎么报数」
            这些分支一次撞全（真实网络上撞不全，也搭不出那么听话的服务器）
      3c-B  真实 socket —— 连一个没人听的端口，验证 _attempt **自己**失败时确实重试
      3c-C  真实 HTTPS —— 故意报错大小，验证「下完了但大小不符」也重试
                          （这条最反直觉：断流不抛异常，就长这个样子）
      3c-D  透传 —— stage_update 有没有把 on_retry 交给 download

    ⚠️ 为什么这里允许打桩，而本文件开头说「打桩等于把要测的东西测掉了」：
    那条针对的是**网络层**（状态码、超时、Content-Length、编码、代理），
    由 3a/3b 用真实网络覆盖。本段要测的是 _attempt **之上**的重试策略，
    它的判据就是 _attempt 的第三个返回值 —— 打桩打掉的不是被测对象。
    即便如此仍然用 3c-B / 3c-C 在真实 socket / 真实 HTTPS 上各验一次，
    确认「剧本里的假设」和真实世界对得上。
    """
    print("\n=== 3c. 下载重试（0.3.1 新增）===")
    print("  背景：原先一次重试都没有；实测 8 次下载失败 1 次（连接级停顿）。")
    print("  注意：正常网络一次就成功，所以「重试没生效」这种错平时看不出来 ——")
    print("        必须主动构造失败才能验。")

    # ---------------------------------------------- 3c-A 策略矩阵
    print("\n  --- 3c-A. 重试策略矩阵（打桩 _attempt，只测策略）---")
    real_attempt = U._attempt

    def scripted(seq):
        """按剧本依次返回 (ok, 原因, 可重试)。

        ok=True 时要把 tmp 写出来 —— download() 成功后会 os.replace(tmp, dest)，
        没有这个文件就会抛 FileNotFoundError（那是**测试自己**的 bug，不是被测代码的）。
        """
        calls = []

        def fake(url, tmp, size, timeout, on_progress):
            calls.append(len(calls) + 1)
            ok, reason, retryable = next(it, (False, "剧本用完了", False))
            if ok:
                with open(tmp, "wb") as f:
                    f.write(b"X" * 64)
            return ok, reason, retryable

        it = iter(seq)
        return fake, calls

    try:
        # A1 断两次、第三次成功 —— 「重试真的有用」的全部意义所在
        fake, calls = scripted([(False, "连接失败：timed out", True),
                                (False, "连接失败：timed out", True),
                                (True, None, True)])
        U._attempt = fake
        seen = []
        d1 = os.path.join(TMP, "retry_ok.bin")
        ok, reason = U.download("http://x/y", d1,
                                on_retry=lambda a, n, w: seen.append((a, n)))
        ck("前两次失败、第三次成功 → 最终成功", ok, True)
        if not ok:
            print("       原因：%s" % reason)
        ck("  实际尝试 3 次（没重试的话只会是 1）", len(calls), 3)
        ck("  on_retry 响了 2 次", len(seen), 2)
        ck("  报的序号是「第 2 次」「第 3 次」", [a for a, _ in seen], [2, 3])
        ck("  报的总数一直是 ATTEMPTS=%d" % U.ATTEMPTS,
           [n for _, n in seen], [U.ATTEMPTS, U.ATTEMPTS])
        ck("  产物落地", os.path.exists(d1), True)
        ck("  不留 .part", os.path.exists(d1 + ".part"), False)

        # A2 一直失败 —— 必须停在 ATTEMPTS 次，不能无限重试
        fake, calls = scripted([(False, "连接失败：timed out", True)] * U.ATTEMPTS)
        U._attempt = fake
        seen = []
        d2 = os.path.join(TMP, "retry_dead.bin")
        ok, reason = U.download("http://x/y", d2,
                                on_retry=lambda a, n, w: seen.append(a))
        ck("一直失败 → 最终失败", ok, False)
        ck("  失败原因原样透传上来", reason, "连接失败：timed out")
        ck("  恰好试 ATTEMPTS 次就停（没无限重试）", len(calls), U.ATTEMPTS)
        ck("  重试回调响了 ATTEMPTS-1 次", len(seen), U.ATTEMPTS - 1)
        ck("  不留目标文件", os.path.exists(d2), False)
        ck("  不留 .part", os.path.exists(d2 + ".part"), False)

        # A3 4xx：服务端明确说「没有」，重试只是白等
        fake, calls = scripted([(False, "HTTP 404", False)])
        U._attempt = fake
        seen = []
        ok, reason = U.download("http://x/y", os.path.join(TMP, "retry_404.bin"),
                                on_retry=lambda a, n, w: seen.append(a))
        ck("4xx → 失败", ok, False)
        ck("  原因里带 HTTP 404", "HTTP 404" in str(reason), True)
        ck("  不重试：只试了 1 次", len(calls), 1)
        ck("  重试回调没响", len(seen), 0)

        # A4 体积超限：重试三次还是超限，同样不该重试
        fake, calls = scripted([(False, "下载体积超过上限，中止", False)])
        U._attempt = fake
        U.download("http://x/y", os.path.join(TMP, "retry_big.bin"))
        ck("体积超限 → 不重试", len(calls), 1)

        # A5 回调自己抛异常不能带崩下载
        fake, calls = scripted([(False, "连接失败：timed out", True), (True, None, True)])
        U._attempt = fake

        def boom(*a):
            raise RuntimeError("回调自己炸了")

        ok, reason = U.download("http://x/y", os.path.join(TMP, "retry_cb.bin"),
                                on_retry=boom)
        ck("on_retry 抛异常不影响下载", ok, True)
        if not ok:
            print("       原因：%s" % reason)
    finally:
        U._attempt = real_attempt
        print("       （已还原 _attempt）")

    # ---------------------------------------------- 3c-B 真实 socket
    print("\n  --- 3c-B. 真实 socket：连不上时会不会重试 ---")
    seen = []
    d3 = os.path.join(TMP, "retry_dead_port.bin")
    # timeout 压到 3 秒：这条路径注定要失败，而且本沙箱的回环连接会被**丢弃**
    # （不是立刻 refuse），每次尝试都要等满超时 → 3 次约 9 秒。
    ok, reason = U.download("http://127.0.0.1:1/v.json", d3, timeout=3,
                            on_retry=lambda a, n, w: seen.append((a, n)))
    ck("端口没人听 → 失败", ok, False)
    ck("  重试了 ATTEMPTS-1 次（真实失败路径也重试）", len(seen), U.ATTEMPTS - 1)
    # 没东西在听，就不可能拿到 HTTP 状态码 —— 这条能证明走的是 socket 异常分支，
    # 而不是「服务端回了个 4xx/5xx」那个分支。
    ck("  走的是连接异常分支（原因里没有 HTTP 状态码）", "HTTP" in str(reason), False)
    print("       原因：%s" % reason)
    ck("  不留目标文件", os.path.exists(d3), False)
    ck("  不留 .part", os.path.exists(d3 + ".part"), False)

    # ---------------------------------------------- 3c-C 真实 HTTPS
    print("\n  --- 3c-C. 真实 HTTPS：下完了但大小不符 ---")
    if ref is None:
        skip("真实网络上的重试", "3a 没取到参照文件")
    else:
        real_sha, real_size = ref
        seen = []
        d4 = os.path.join(TMP, "retry_size.bin")
        # 故意把期望大小写错 7 字节：每次都能完整下完，但校验必然不过。
        # 这正是**断流**的样子 —— 连接中途断掉时 r.read() 只是提前返回 b""，
        # 不抛异常，于是表现成「大小不符」。只重试「抛异常那一类」会漏掉它。
        ok, reason = U.download(RELIABLE_URL, d4, real_sha, real_size + 7, timeout=40,
                                on_retry=lambda a, n, w: seen.append(a))
        ck("真实网络：大小故意写错 → 失败", ok, False)
        ck("  原因点明是大小不符", "大小不符" in str(reason), True)
        print("       原因：%s" % reason)
        ck("  重试了 ATTEMPTS-1 次（真实网络上也生效）", len(seen), U.ATTEMPTS - 1)
        ck("  不留目标文件", os.path.exists(d4), False)
        ck("  不留 .part", os.path.exists(d4 + ".part"), False)

    # ---------------------------------------------- 3c-D 透传
    print("\n  --- 3c-D. stage_update 有没有把 on_retry 交给 download ---")
    seen = []
    ok, reason = U.stage_update(os.path.join(TMP, "stage_fake.exe"),
                                {"url": "http://127.0.0.1:1/v.json"},
                                timeout=3,
                                on_retry=lambda a, n, w: seen.append(a))
    ck("stage_update 下载失败 → 整体失败", ok, False)
    ck("  on_retry 被透传下去（响了 ATTEMPTS-1 次）", len(seen), U.ATTEMPTS - 1)
    print("       原因：%s" % reason)

    # ---------------------------------------------- 3c-E 阳性对照
    print("\n  --- 3c-E. 阳性对照：把重试拿掉，上面的断言必须对不上 ---")
    real_dl = U.download

    def no_retry(url, dest, sha256=None, size=None, timeout=U.TIMEOUT,
                 on_progress=None, on_retry=None):
        """0.3.1 **之前**的 download：只试一次，不重试。

        拿它当阳性对照 —— 如果上面 3c-B / 3c-C 那几条断言在这份实现上照样通过，
        说明它们根本没在测重试（典型症状：条件写错导致检查空转）。
        """
        tmp = dest + ".part"
        U._unlink(tmp)
        ok, reason, _ = U._attempt(url, tmp, size, timeout, on_progress)
        if not ok:
            U._unlink(tmp)
            return False, reason
        os.replace(tmp, dest)
        return True, None

    try:
        U.download = no_retry
        seen = []
        U.download("http://127.0.0.1:1/v.json", os.path.join(TMP, "ctrl.bin"),
                   timeout=3, on_retry=lambda a, n, w: seen.append(a))
    finally:
        U.download = real_dl
    ck("去掉重试后 on_retry 一次都不响（对照）", len(seen), 0)
    # 这条把两件事一起钉住：① 对照实现的回调次数确实和真实实现不同
    # （所以 3c-B/3c-C 那几条 `== ATTEMPTS-1` 是真在测重试，不是空转）；
    # ② ATTEMPTS 一旦被改成 1，重试就等于没有，这里会跟着失败 ——
    # 否则 3c-B/3c-C 的期望值会变成 `== 0` 而**静默通过**，检查空转。
    ck("  对照实现的回调次数 ≠ 真实实现的（且 ATTEMPTS > 1）",
       len(seen) != U.ATTEMPTS - 1, True)
    print("       对照实现下 on_retry 响 %d 次，真实实现下响 %d 次 —— 对不上才算测到了"
          % (len(seen), U.ATTEMPTS - 1))


def part4_real_replace():
    """真正替换一个**正在运行**的 exe。不需要网络，所以沙箱里也能跑。"""
    print("\n=== 4. 替换正在运行的 exe（真实进程）===")
    app = os.path.join(TMP, "app.exe")
    shutil.copy2(PYW, app)
    old_sha = U.sha256_file(app)

    proc = subprocess.Popen([app, "-c", "import time; time.sleep(120)"])
    time.sleep(2.5)
    if proc.poll() is not None:
        ck("被测进程成功启动", False, True)
        return
    print("  进程运行中，PID=%d" % proc.pid)

    new_bytes = b"FAKE NEW VERSION " * 5000
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    staged = app + ".new"
    open(staged, "wb").write(new_bytes)

    try:
        ok, reason = U.apply_update(app, staged)
        ck("apply_update 成功", ok, True)
        if not ok:
            print("       原因：%s" % reason)
        ck("  原路径已是新版本", U.sha256_file(app), new_sha)
        ck("  .old 里是旧版本", U.sha256_file(app + ".old"), old_sha)
        ck("  运行中的进程不受影响", proc.poll() is None, True)
        ck("  运行中删不掉 .old（所以留到下次启动）", U.cleanup_stale(app), None)

        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        time.sleep(0.5)
        ck("  进程退出后能删掉 .old", U.cleanup_stale(app), "app.exe.old")
        ck("  .old 确实没了", os.path.exists(app + ".old"), False)
        ck("  新版文件仍在", os.path.exists(app), True)

        print("\n  --- 回滚路径：新文件搬不进去时必须还原 ---")
        app2 = os.path.join(TMP, "app2.exe")
        shutil.copy2(PYW, app2)
        sha2 = U.sha256_file(app2)
        ok, reason = U.apply_update(app2, os.path.join(TMP, "根本不存在.new"))
        ck("新文件缺失 → 替换失败", ok, False)
        print("       原因文字：%s" % reason)
        ck("  已回滚：原文件完好", U.sha256_file(app2), sha2)
        ck("  没留下 .old 残骸", os.path.exists(app2 + ".old"), False)
    finally:
        if proc.poll() is None:
            proc.kill()


def part5_self_guard():
    print("\n=== 5. 源码运行时的自我保护 ===")
    ok, why = U.can_self_update()
    ck("源码运行 → 不允许自我更新", ok, False)
    print("       原因文字：%s" % why)
    ok2, exe = U.can_self_update(PYW)
    ck_true("给真实 exe 路径 → 允许", ok2)
    ok3, _ = U.can_self_update(os.path.join(TMP, "并不存在.exe"))
    ck("路径不存在 → 不允许", ok3, False)


def part6_negative_control():
    print("\n=== 6. 测试自身的阳性对照 ===")
    before = len(FAILS)
    ck("这条必须失败（对照）", 1, 2)
    if len(FAILS) == before + 1:
        print("  [OK] 阳性对照生效：断言失败确实被记录")
        FAILS.pop()
    else:
        print("  [失败] 阳性对照没生效 —— 本次「通过」不可信")
        FAILS.append("阳性对照未生效")


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="updater_test_")
    blob = os.urandom(64 * 1024)
    try:
        lb_ok, _ = loopback_works()
        print("回环连接：%s" % ("可用" if lb_ok else "被沙箱丢弃 —— 清单部分改走真实公网"))
        if lb_ok:
            part1_loopback(blob)
        else:
            part1_realnet()
        part_failure_is_not_current()
        ref = part3_download()
        part3c_retry(ref)
        part4_real_replace()
        part5_self_guard()
        part6_negative_control()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 64)
    if FAILS:
        print("失败 %d / %d 项：" % (len(FAILS), CHECKS[0]))
        for f in FAILS:
            print("  - %s" % f)
    else:
        print("全部通过（%d 项断言）" % CHECKS[0])
    if SKIPS:
        print("⚠️ 跳过 %d 项（这些**没有被验证**）：" % len(SKIPS))
        for s in SKIPS:
            print("  - %s" % s)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
