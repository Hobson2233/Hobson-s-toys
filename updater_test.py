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
    """
    print("\n=== 2. 网络失败不能被当成「已是最新」（关键行为）===")
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
        ck_true("%s → 原因里带 %r（证明走的是该走的分支）" % (name, must),
                must in str(reason))
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
        return
    real_sha = U.sha256_file(probe)
    real_size = os.path.getsize(probe)
    print("       参照文件 %d 字节 sha256 %s…" % (real_size, real_sha[:12]))

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
        return
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
        return
    ok, reason = result.get("v", (False, "没有结果"))
    if ok:
        print("       ✅ 从 GitHub 下载成功，与本地 exe 逐字节相同（%.1f MB / %.1f 秒，%.1f KB/s）"
              % (local_size / 1048576.0, dt, local_size / 1024.0 / max(dt, 0.01)))
    elif any(h in reason for h in NETWORK_HINTS):
        skip("GitHub 发布资产比对", "本机取不到（%.0fs 后 %s）" % (dt, reason))
    else:
        ck("GitHub 资产与本地 exe 一致", False, True)
        print("       原因：%s" % reason)


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
        part3_download()
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
