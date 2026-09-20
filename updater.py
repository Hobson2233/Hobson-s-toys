# -*- coding: utf-8 -*-
"""检查更新 / 下载 / 自我替换。

为什么单独一个模块：
    这部分逻辑要能**脱离 GUI 单独测**（起个本地 HTTP 服务就能跑完整流程），
    塞进 campus_login.py 就只能靠点界面验证了。项目里 local_secrets.py 也是同样做法。

对外暴露的函数全部**纯标准库**（urllib / hashlib / json / os），
不引入任何新依赖 —— 打包体积的三条手段（不用 requests、精简 EXCLUDES、裁 Tcl 数据）
一条都不破坏。

三个刻意的设计决定，都不是随手写的：

1. **不走系统代理**（ProxyHandler({})）。
   本项目实测过：系统代理开着指向一个不可用的 127.0.0.1:7897 时，
   HTTP 客户端会直接卡死。urllib 在 Windows 上默认会读 IE 的代理设置
   （getproxies() 读注册表），所以必须显式关掉，否则「检查更新」会重演那次故障。
   CDN 是公网服务，本来也不需要代理。

2. **失败一律返回 "unknown"，绝不返回 "current"**。
   这是本项目的老规矩（见 test_internet() 的三态）：把「测不出来」当成「没有新版」
   是典型的静默失败 —— 用户以为是最新版，其实根本没查成功。

3. **版本比较走元组，不走字符串**。
   字符串比较会让 "0.10.0" < "0.9.0"，这种 bug 平时不显形，等发了 0.10 才炸。
"""
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

# 发布清单的地址。域名定下来之后改这一行即可（也可以由调用方传入覆盖）。
# 域名 = hobson2233.dpdns.org（DigitalPlat 免费二级域名，NS 托管在 Cloudflare），
# 站点托管在 **Cloudflare Worker `campus-login` 的静态资源**上（⚠️ 不是 Pages 项目，
# 原 Pages 已并入 Workers；自定义域名绑的是**服务名**，所以那个名字不能改）。
# 改这里时记得同步改 make_manifest.py 的 BASE_URL。
MANIFEST_URL = "https://hobson2233.dpdns.org/version.json"

# 兜底清单地址：主域名被回收 / 解析不了时还能查到版本号。
# 只有 GitHub 的 api 域名在国内可用性还不错（raw.* 和 release 下载都实测过很差），
# 这里只用来**查版本号**，下载仍旧走主域名 —— 所以这条兜底不需要下载能力。
# 实测数据见 .workbuddy-ai/memory/2026-09-18.md「GitHub Release 下载基本不可用」一节。
FALLBACK_MANIFEST_URL = ("https://api.github.com/repos/Hobson2233/"
                         "Hobson-s-toys/releases/latest")

# MANIFEST_URL 去掉 /version.json 之后的站点根，用来拼下载地址给用户看。
BASE_URL_HINT = MANIFEST_URL.rsplit("/", 1)[0]

UA = "CampusLogin-Updater/1.0"
TIMEOUT = 10

# 允许的最大安装包体积。防的是「清单被改坏 / 指向了一个几百 MB 的文件」
# 这种把用户流量耗光的情况 —— 我们的 exe 一直稳定在 10 MB 上下。
MAX_SIZE = 60 * 1024 * 1024

OLD_SUFFIX = ".old"

# 下载最多尝试几次。为什么必须有重试（2026-09-19 实测）：
#   这条路径要下 12 MB，而它原先**一次重试都没有**。同一台机器上 8 次下载失败 1 次，
#   失败形态是 `TimeoutError: The read operation timed out` —— 连接级停顿。
#   ⚠️ 注意**问题不在超时值上**：实测每次 read 之间的间隔中位只有几十毫秒、
#      最大 1.55s，相对 TIMEOUT=10 有 6.5 倍余量，所以放大 TIMEOUT 治不了它。
#
# 为什么「大小不符 / 哈希不符」也必须重试（这条最反直觉）：
#   **传输被截断不会抛异常**。连接中途断掉时 `r.read()` 只是提前返回 b""，
#   循环正常结束 —— 于是表现成「文件大小不符」，看着像清单写错了。
#   只重试「抛异常的那类」会把真实断流当成清单错误，直接判死。
#
# 唯一不重试的是 HTTP 4xx：那是服务端明确说「没有 / 不给」，重试只是白白拖慢。
#   新版发布后旧文件名就是 404，GitHub 兜底路径上尤其不该在这里耗时间。
ATTEMPTS = 3

# 检查结果的状态。调用方必须三种都处理，不能把 UNKNOWN 当 CURRENT。
STATE_NEWER = "newer"       # 有新版，info 里带下载信息
STATE_CURRENT = "current"   # 已是最新
STATE_UNKNOWN = "unknown"   # 网络不通 / 清单格式不对 / 拿不到 sha256 —— 总之「没查出来」
STATE_MANUAL = "manual"     # 版本跨度太大，不支持原地更新，得手动下载


def _opener():
    """不读系统代理的 opener。理由见模块开头的第 1 条。"""
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
    )


def parse_version(s):
    """'0.10.2' -> (0, 10, 2)。解析不出来返回 None（不要返回空元组，那会变成「最小版本」）。"""
    if not isinstance(s, str):
        return None
    s = s.strip().lstrip("vV")
    if not s:
        return None
    parts = s.split(".")
    out = []
    for p in parts:
        # 只吃纯数字段；"1.0.0-beta" 的 beta 部分丢掉（和 build.py 的 _ver_tuple 一致）
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            return None if not out else tuple(out)
        out.append(int(num))
    return tuple(out) if out else None


def is_newer(remote, current):
    """remote 是否比 current 新。任一解析不出来 → None（不知道），不是 False。"""
    a = parse_version(remote)
    b = parse_version(current)
    if a is None or b is None:
        return None
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def _get(url, timeout=TIMEOUT):
    """取一个 URL 的原始字节。成功 (True, bytes)，失败 (False, 原因)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _opener().open(req, timeout=timeout) as r:
            if r.status != 200:
                return False, "HTTP %s" % r.status
            return True, r.read()
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code
    except urllib.error.URLError as e:
        return False, "连接失败：%s" % e.reason
    except ssl.SSLError as e:
        return False, "证书校验失败：%s" % e
    except ValueError as e:
        # URL 里有非法字符（比如没转义的中文）时 urllib 抛的是 UnicodeEncodeError，
        # 它是 ValueError 的子类、**不是** OSError —— 只 catch OSError 会漏掉它，
        # 让一个畸形的清单地址把整个程序带崩。实测踩到过。
        return False, "地址不合法：%s" % e
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)


def fetch_manifest(url=None, timeout=TIMEOUT):
    """拉发布清单。返回 (state, data_or_reason)。

    拿不到就返回 (STATE_UNKNOWN, 原因) —— 调用方必须把它和「已是最新」区分开。
    """
    url = url or MANIFEST_URL
    ok, body = _get(url, timeout)
    if not ok:
        return STATE_UNKNOWN, body
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return STATE_UNKNOWN, "清单不是合法 JSON：%s" % e
    if not isinstance(data, dict):
        return STATE_UNKNOWN, "清单顶层不是对象"
    return STATE_NEWER, data


def evaluate(manifest, current_version):
    """比对清单与当前版本。返回 (state, info)。

    state = NEWER / CURRENT / MANUAL / UNKNOWN
    info 在 NEWER 时是 dict(version, url, sha256, size, notes)，
    其余情况是字符串原因。
    """
    if not isinstance(manifest, dict):
        return STATE_UNKNOWN, "清单不是对象"

    remote = manifest.get("version")
    if not remote:
        return STATE_UNKNOWN, "清单里没有 version 字段"

    newer = is_newer(remote, current_version)
    if newer is None:
        return STATE_UNKNOWN, "版本号无法比较（远端 %r / 本地 %r）" % (remote, current_version)
    if not newer:
        return STATE_CURRENT, remote

    # 跨度太大就不做原地更新了。判断依据是清单里的 min_version：
    # 低于它说明中间的迁移逻辑接不上，硬换 exe 可能让数据格式对不上。
    lo = manifest.get("min_version")
    if lo:
        c = parse_version(current_version)
        m = parse_version(lo)
        if c is not None and m is not None and c < m:
            return STATE_MANUAL, "当前版本过低（需要 %s 以上），请手动下载" % lo

    url = manifest.get("url")
    sha = (manifest.get("sha256") or "").strip().lower()
    if not url:
        return STATE_UNKNOWN, "清单里没有 url 字段"
    # 没有校验值就不下载。宁可让用户手动下，也不装一个来源无法验证的可执行文件。
    if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
        return STATE_UNKNOWN, "清单里的 sha256 不合法，拒绝下载"

    size = manifest.get("size")
    if isinstance(size, int) and size > MAX_SIZE:
        return STATE_UNKNOWN, "安装包体积异常（%d 字节），拒绝下载" % size

    return STATE_NEWER, {
        "version": remote,
        "url": url,
        "sha256": sha,
        "size": size if isinstance(size, int) else None,
        "notes": (manifest.get("notes") or "").strip(),
    }


def parse_github_release(data, base_url=None):
    """把 GitHub Releases API 的响应转成我们自己的清单格式。

    GitHub 的返回长这样（只列用到的字段）：
        {"tag_name": "v0.1.0", "body": "## 更新内容\\n\\n- 第一条\\n- 第二条",
         "assets": [{"name": "campus-login.exe", "size": 9842616,
                     "browser_download_url": "https://github.com/..."}]}

    两个坑：
      * **tag 带 `v` 前缀**（`v0.1.0`），parse_version 会 lstrip 掉，但写进
        manifest 时要保持一致，所以这里统一去掉。
      * **GitHub 不给 sha256**。所以兜底路径只能用来「查版本号」，
        真要下载还是得回主域名 —— 那边有 sha256。这点必须让调用方知道：
        返回的 url 我们指向主域名的 dl/ 路径，而不是 GitHub 的资产地址
        （GitHub 资产下载在本校网络实测 3 次只成功 1 次，不能拿它当下载源）。
    """
    if not isinstance(data, dict):
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    version = tag.lstrip("vV")

    # 从 release body 里抠第一条 bullet 当更新说明，和 make_manifest 的口径一致。
    notes = ""
    for line in (data.get("body") or "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            notes = line[2:].strip().rstrip("。")
            break

    if base_url:
        url = "%s/dl/campus-login-%s.exe" % (base_url.rstrip("/"), version)
    else:
        url = ""
    # sha256 留空 —— evaluate 会因此判 unknown 并拒绝下载。
    # 这是刻意的：兜底只负责告诉用户「有新版本」，不负责下载。
    return {"version": version, "url": url, "sha256": "", "notes": notes,
            "source": "github"}


def judge_fallback(release, current_version):
    """判定「兜底清单」（GitHub Releases 形状的 dict）相对当前版本的结果。

    抽成纯函数是为了**能测** —— 这段逻辑依赖网络才能跑到，而沙箱里回环被丢弃，
    没法起假服务喂它。纯函数就能拿构造好的 dict 直接测。

    ⚠️ **本函数永远不会返回 STATE_CURRENT。** 这是刻意的，理由见下面那一段注释。
    """
    rel = parse_github_release(release, base_url=BASE_URL_HINT)
    if rel is None:
        return STATE_UNKNOWN, "兜底清单解析失败"

    newer = is_newer(rel["version"], current_version)
    if newer is None:
        return STATE_UNKNOWN, "版本号无法比较（远端 %r / 本地 %r）" % (
            rel["version"], current_version)
    if not newer:
        # ⚠️ 这里**绝对不能返回 STATE_CURRENT**。
        # 兜底路径查的是 GitHub Releases，而主站（Worker `campus-login` 的静态资源）
        # 才是权威来源。两者可能不一致 —— 比如发了新版只传主站、忘了发 Release，
        # 此时 GitHub 上还是旧版本，"相同"不等于"已是最新"。
        # 返回 CURRENT 就会让用户看到"已是最新版本"而实际有新版，典型的静默失败。
        # （2026-09-18 实测踩到：站点还没部署，主域名解析失败走兜底，
        #   恰好 GitHub 上是 v0.1.0 == 本地 0.1.0，界面就报了"已是最新版本"。）
        return STATE_UNKNOWN, (
            "主站（%s）连接失败；从 GitHub 看没有更新的版本（v%s），"
            "但无法确认是否为最新，请稍后重试或到官网查看" % (
                BASE_URL_HINT, rel["version"]))
    # 查到了新版本，但没有校验值 → 只能引导手动下载。
    return STATE_MANUAL, "发现新版本 v%s（%s），请到官网下载：%s" % (
        rel["version"], rel.get("notes") or "无说明", BASE_URL_HINT)


def check(current_version, url=None, timeout=TIMEOUT, allow_fallback=True):
    """一步到位：拉清单 + 比对。返回 (state, info_or_reason)。

    主域名查不通时会自动尝试兜底地址（GitHub Releases API）。**但兜底成功
    也只意味着「知道了新版本号」，因为 GitHub 不给 sha256，下载仍走主域名。**
    所以这种情况下返回的是 STATE_MANUAL（让用户去网页手动下），
    而不是 NEWER —— 否则程序会拿一个没有校验值的地址去下载，违反铁律。
    """
    state, data = fetch_manifest(url, timeout)
    if state != STATE_UNKNOWN:
        return evaluate(data, current_version)

    # 显式传了 url 说明调用方在测指定地址，不要去兜底（那会让测试测不到想测的东西）。
    primary_reason = data
    if url or not allow_fallback:
        return STATE_UNKNOWN, primary_reason

    fb_state, fb_data = fetch_manifest(FALLBACK_MANIFEST_URL, timeout)
    if fb_state == STATE_UNKNOWN:
        # 两个都挂了。**原因要把主域名的报错带上** —— 那才是最常见的原因，
        # 只报 GitHub 的错会把人引到错的方向。
        return STATE_UNKNOWN, "主域名：%s；兜底地址：%s" % (primary_reason, fb_data)

    st, info = judge_fallback(fb_data, current_version)
    if st == STATE_UNKNOWN:
        # 兜底也没给出结论 —— 把主域名的原因一并带上，别丢信息。
        return STATE_UNKNOWN, "主域名：%s；%s" % (primary_reason, info)
    return st, info


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _attempt(url, tmp, size, timeout, on_progress):
    """下载**一次**到 tmp。返回 (ok, 原因, 是否值得重试)。

    重试判定集中在这里，别让调用方去猜原因串的内容 —— 那是「过滤条件写错 =
    检查空转」的同类：拿字符串做判断，改一个字就静默失效。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _opener().open(req, timeout=timeout) as r:
            if r.status != 200:
                # 4xx 是「服务端说没有」，重试不会变
                return False, "HTTP %s" % r.status, not (400 <= r.status < 500)
            total = size or (int(r.headers.get("Content-Length") or 0) or None)
            got = 0
            with open(tmp, "wb") as f:
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    got += len(b)
                    if got > MAX_SIZE:
                        return False, "下载体积超过上限，中止", False
                    f.write(b)
                    if on_progress:
                        on_progress(got, total)
        return True, None, True
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code, not (400 <= e.code < 500)
    except urllib.error.URLError as e:
        return False, "连接失败：%s" % e.reason, True
    except (OSError, ValueError) as e:
        return False, "%s: %s" % (type(e).__name__, e), True


def download(url, dest, sha256=None, size=None, timeout=TIMEOUT, on_progress=None,
             on_retry=None):
    """下载到 dest 并校验。返回 (True, None) 或 (False, 原因)。

    dest 必须和目标 exe **同一个目录** —— 同一卷上 os.rename 才是原子的
    （跨卷会退化成复制，中途断电就留下半个文件）。
    校验不通过会把临时文件删掉，绝不留一个来路不明的 exe 在磁盘上。

    传输失败会自动重试（最多 ATTEMPTS 次，理由见 ATTEMPTS 的注释）。
    `on_retry(第几次尝试, 总次数, 原因)` 在每次重试**之前**回调 —— 调用方拿它提示
    「不是卡死了，是在重试」。回调抛异常会被吞掉：它只是提示，不该带崩下载。
    """
    tmp = dest + ".part"
    reason = "未开始"
    for attempt in range(1, ATTEMPTS + 1):
        _unlink(tmp)                       # 清掉上一轮的残骸，别让两次的数据混在一起
        ok, reason, retryable = _attempt(url, tmp, size, timeout, on_progress)

        if ok and size is not None:
            # 先把大小读出来再决定删文件。写成 `_unlink(tmp)` 之后再 getsize(tmp) 的话，
            # 报错信息里那句「实际 %d」会抛 FileNotFoundError —— **错误处理路径自己崩掉**，
            # 而且只在「大小不符」这个罕见分支才触发。这个 bug 是靠负面对照逼出来的。
            actual_size = os.path.getsize(tmp)
            if actual_size != size:
                # 注意这里 retryable 置 True：截断**不会抛异常**，就是走到这个分支的
                ok, retryable = False, True
                reason = "文件大小不符（期望 %d，实际 %d）" % (size, actual_size)

        if ok and sha256:
            actual = sha256_file(tmp)
            if actual != sha256:
                # 把两个哈希都打出来 —— 排查时最想知道的就是「差在哪」
                ok, retryable = False, True
                reason = "校验失败（期望 %s…，实际 %s…）" % (sha256[:12], actual[:12])

        if ok:
            break
        if not retryable or attempt == ATTEMPTS:
            _unlink(tmp)
            return False, reason
        if on_retry:
            try:
                on_retry(attempt + 1, ATTEMPTS, reason)
            except Exception:
                pass

    try:
        os.replace(tmp, dest)
    except OSError as e:
        _unlink(tmp)
        return False, "写入失败：%s" % e
    return True, None


def _unlink(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _same_volume(a, b):
    """两个路径是否在同一个卷上（决定 os.rename / os.replace 能不能原子工作）。

    只看盘符。对本地盘够用；网络盘、挂载点会退化成「不假设同卷」——
    那只是让我们保守一点（回退到 exe 同目录），不会做错事。
    """
    da = os.path.splitdrive(os.path.abspath(a))[0].lower()
    db = os.path.splitdrive(os.path.abspath(b))[0].lower()
    return bool(da) and da == db


def work_dir_for(exe, work_dir=None):
    """更新临时文件（`.new` / `.part` / `.old`）该放哪个目录。返回一个已存在的目录。

    **为什么不能默认放 exe 同目录**（2026-09-20 用户反馈）：
        用户常把 exe 直接放桌面。替换时会在桌面上留下 `.old`（几 MB 的旧程序本体，
        要等下次启动才删），下载期间还有 `.new.part`。对不懂的人来说就是
        「桌面上莫名多了个奇怪文件，又不敢删」—— 删错了程序就没了。

    **为什么必须判同卷**：
        `os.replace` / `os.rename` 在 Windows 上走 MoveFileEx，**不带 COPY_ALLOWED**，
        跨卷会直接失败（ERROR_NOT_SAME_DEVICE）。而替换 exe 这步必须是原子的
        （中途断电不能留下半个 exe）。所以跨卷时只能退回 exe 同目录。

    exe 为 None（源码运行）时返回 None。
    """
    if not exe:
        return None
    exe_dir = os.path.dirname(os.path.abspath(exe)) or "."
    if not work_dir:
        return exe_dir
    try:
        wd = os.path.abspath(work_dir)
        if not os.path.isdir(wd):
            os.makedirs(wd, exist_ok=True)
        if _same_volume(wd, exe):
            return wd
    except OSError:
        pass
    return exe_dir


def old_path_of(exe, work_dir=None):
    """旧版本备份的落点。exe 为 None（源码运行）时返回 None —— **不要拿它去拼字符串**。

    这里原本是 `return exe + OLD_SUFFIX`，传 None 直接 TypeError。
    留了个「反正调用方有 try/except 兜着」的隐患：源码运行时每次启动都会
    抛一个 TypeError 进日志，把**真正的**错误淹掉。
    （2026-09-18 实测踩到：日志尾部就是这条 TypeError。）

    2026-09-20：落点从「exe 同目录」改成 `work_dir`（程序数据目录）——
    见 work_dir_for。不传 work_dir 时仍是 exe 同目录，老调用方行为不变。
    """
    if not exe:
        return None
    d = work_dir_for(exe, work_dir)
    return os.path.join(d, os.path.basename(exe) + OLD_SUFFIX)


def cleanup_stale(exe, work_dir=None):
    """启动时调用：把上次更新留下的临时文件删掉。返回删掉的文件名或 None。

    为什么现在才删：`.old` 在被替换的进程退出前一直是被占用的，当时删不掉，
    只能留到下次启动。

    ⚠️ **两个位置都要看**：0.3.2 及之前 `.old` 就丢在 exe 同目录（用户的桌面），
       那些老残留升级后还躺在那儿 —— 只清新位置等于没修好。
    ⚠️ 用**前缀匹配**，不是几个写死的文件名。apply_update 在 `.old` 被占着时
       会退到带时间戳的备用名 `app.exe.old.<时间戳>` —— 写死名字就会漏掉它，
       于是它永远躺在数据目录里、越积越多。

    exe 传 None（源码运行）时不做事，直接返回 None —— 那不是错误情况。
    """
    if not exe:
        return None
    base = os.path.basename(exe)
    exe_dir = os.path.dirname(os.path.abspath(exe)) or "."
    # `<exe名>.old` 连备用名一起覆盖；`<exe名>.new` 连 `.new.part` 一起覆盖。
    prefixes = (base + OLD_SUFFIX, base + ".new")
    cands = []
    for d in (work_dir_for(exe, work_dir), exe_dir):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            # 老版本（≤0.3.2）的下载文件叫 `campus-login-<版本>.new`，不含 exe 名，
            # 按前缀+后缀单独扫一遍，把这些历史残留也收掉。
            if name.startswith(prefixes) or (
                    name.startswith("campus-login-")
                    and (name.endswith(".new") or name.endswith(".new.part"))):
                p = os.path.join(d, name)
                if p not in cands:
                    cands.append(p)
    removed = None
    for p in cands:
        try:
            os.remove(p)
            removed = removed or os.path.basename(p)
        except OSError:
            pass
    return removed


def can_self_update(exe=None):
    """能不能自我替换。返回 (True, exe路径) 或 (False, 原因)。

    exe=None  = 「检查我自己」→ 源码运行时直接说清楚，别让用户点个没反应的按钮
    exe=<路径> = 「检查这个路径」→ 跳过 frozen 判断，只看文件在不在、目录能不能写

    这两种语义必须分开：frozen 是**当前进程**的属性，可写性是**路径**的属性。
    混在一起写的话，测试里想验证可写性检查时会永远得到「源码运行」这个答案。
    """
    if exe is None:
        if not getattr(sys, "frozen", False):
            return False, "当前是源码运行，没有可替换的程序文件"
        exe = sys.executable
    if not os.path.isfile(exe):
        return False, "找不到程序文件：%s" % exe
    if not os.access(os.path.dirname(exe) or ".", os.W_OK):
        return False, "程序所在目录不可写，无法自动更新"
    return True, exe


def discard(path):
    """删掉一个更新临时文件。失败不抛异常 —— 清不掉也不该拦住主流程。"""
    _unlink(path)


def apply_update(exe, new_file, work_dir=None):
    """用 new_file 替换正在运行的 exe。返回 (True, None) 或 (False, 原因)。

    **实测过的机制**（updtest_running_exe.py，2026-09-18）：
        Windows 加载器把运行中的映像映射为 FILE_SHARE_READ | FILE_SHARE_DELETE，
        所以 os.rename() 能成功，而 open('wb') 覆写和 os.remove() 会被拒。
    于是流程是：把运行中的 exe 改名成 .old 腾出路径 → 把新文件搬到原路径。
    旧文件此刻删不掉（还被自己占着），交给下次启动的 cleanup_stale()。
    Chrome / Firefox 在 Windows 上就是这么做的。

    `.old` 落到 work_dir（程序数据目录），不再丢在 exe 同目录 —— 见 work_dir_for。

    ⚠️ `work_dir` 必须与 exe **同卷**，两条 rename 都吃这个条件：
       ① `os.rename(exe, old)` 把 exe 搬进 work_dir；
       ② `os.replace(new_file, exe)` 把新文件搬回 exe 的位置。
       两步在 Windows 上都走 MoveFileEx、**不带 COPY_ALLOWED**，跨卷直接
       ERROR_NOT_SAME_DEVICE。调用方用 work_dir_for() 决定位置（它会判卷）。
    """
    old = old_path_of(exe, work_dir)
    _unlink(old)  # 清掉更早一次留下的残骸；删不掉就带着继续，下面的 rename 会告诉我们
    try:
        os.rename(exe, old)
    except OSError as e:
        # ⚠️ 有一种情况 rename 会失败、而上面那句 _unlink 又删不掉：
        #    上一次替换留下的 `.old` 正被**当前进程**占着（当前进程就是从它跑起来的）。
        #    删不掉、也覆盖不了 —— 于是「更新完没重启就又点一次更新」会直接报升级失败。
        #    这时换个带时间戳的备用名，别让用户看到一句没头绪的「升级失败」。
        #    ⚠️ 备用名必须仍以 `<exe名>.old` 开头：cleanup_stale 是按前缀扫的，
        #       换个前缀（比如 `.old2`）它就再也清不掉了，会永久累积。
        alt = "%s.%d" % (old, int(time.time()))
        try:
            os.rename(exe, alt)
            old = alt
        except OSError:
            return False, "无法腾出原路径（改名失败）：%s" % e
    try:
        os.replace(new_file, exe)
    except OSError as e:
        # 关键回滚：新文件没搬进去，就把旧文件改回来，别让用户既没新版也没旧版
        try:
            os.rename(old, exe)
            return False, "替换失败，已回滚到原版本：%s" % e
        except OSError:
            return False, "替换失败且回滚也失败，请手动恢复：%s" % e
    return True, None


def stage_update(exe, info, work_dir=None, timeout=TIMEOUT, on_progress=None,
                 on_retry=None):
    """下载 + 校验 + 替换，一条龙。返回 (True, None) 或 (False, 原因)。

    临时文件放在 work_dir（默认 exe 同目录）—— 调用方应当传程序数据目录，
    免得把 `.new.part` 丢在用户桌面上（见 work_dir_for）。
    """
    d = work_dir_for(exe, work_dir)
    dest = os.path.join(d, os.path.basename(exe) + ".new")
    ok, reason = download(info["url"], dest, info.get("sha256"), info.get("size"),
                          timeout=timeout, on_progress=on_progress,
                          on_retry=on_retry)
    if not ok:
        return False, reason
    ok, reason = apply_update(exe, dest, work_dir)
    if not ok:
        _unlink(dest)
        return False, reason
    return True, None


def format_progress(got, total):
    if not total:
        return "已下载 %.1f MB" % (got / 1048576.0)
    return "已下载 %.1f / %.1f MB（%d%%）" % (
        got / 1048576.0, total / 1048576.0, int(got * 100 / total))


def describe(state, info):
    """把状态翻译成给用户看的一句话。GUI 和命令行都用它，保证口径一致。"""
    if state == STATE_NEWER:
        s = "发现新版本 v%s" % info["version"]
        if info.get("notes"):
            s += "：%s" % info["notes"]
        return s
    if state == STATE_CURRENT:
        return "已是最新版本"
    if state == STATE_MANUAL:
        return str(info)
    return "检查更新失败：%s" % info


def _selftest():
    """不用联网的自检：版本比较、清单判定、哈希校验。"""
    fails = []
    n = [0]          # 断言计数。以前是写死在最后那行 print 里的字面量，
                     # 加一条断言就漂移一次 —— 和「版本号只有一个来源」同一个道理。

    def ck(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s：得到 %r，期望 %r" % (name, got, want))

    ck("解析 0.1.0", parse_version("0.1.0"), (0, 1, 0))
    ck("解析 v0.2.0", parse_version("v0.2.0"), (0, 2, 0))
    ck("解析 0.2.0-beta", parse_version("0.2.0-beta"), (0, 2, 0))
    ck("解析垃圾", parse_version("abc"), None)
    ck("解析空串", parse_version(""), None)
    ck("解析 None", parse_version(None), None)

    # 这一条是整段代码存在的理由：字符串比较会说 "0.10.0" < "0.9.0"
    ck("0.10.0 > 0.9.0（字符串比较会判错）", is_newer("0.10.0", "0.9.0"), True)
    ck("0.9.0 < 0.10.0", is_newer("0.9.0", "0.10.0"), False)
    ck("0.1.0 == 0.1.0", is_newer("0.1.0", "0.1.0"), False)
    ck("0.1.1 > 0.1.0", is_newer("0.1.1", "0.1.0"), True)
    ck("0.2 > 0.1.9", is_newer("0.2", "0.1.9"), True)
    ck("比较不出来时返回 None", is_newer("abc", "0.1.0"), None)

    good = {"version": "0.2.0", "url": "https://x/y.exe", "sha256": "a" * 64, "size": 100}
    st, _ = evaluate(good, "0.1.0")
    ck("有新版", st, STATE_NEWER)
    st, _ = evaluate(good, "0.2.0")
    ck("已是最新", st, STATE_CURRENT)
    st, _ = evaluate({"version": "0.2.0", "url": "https://x/y.exe", "sha256": "短"}, "0.1.0")
    ck("sha256 不合法 → 拒下载", st, STATE_UNKNOWN)
    st, _ = evaluate({"version": "0.2.0", "sha256": "a" * 64}, "0.1.0")
    ck("缺 url → unknown", st, STATE_UNKNOWN)
    st, _ = evaluate({"url": "https://x/y.exe", "sha256": "a" * 64}, "0.1.0")
    ck("缺 version → unknown", st, STATE_UNKNOWN)
    st, _ = evaluate({"version": "0.2.0", "url": "u", "sha256": "a" * 64,
                      "min_version": "0.2.0"}, "0.1.0")
    ck("跨度过大 → 手动", st, STATE_MANUAL)
    st, _ = evaluate({"version": "0.2.0", "url": "u", "sha256": "a" * 64,
                      "size": MAX_SIZE + 1}, "0.1.0")
    ck("体积异常 → 拒下载", st, STATE_UNKNOWN)
    st, _ = evaluate("不是字典", "0.1.0")
    ck("清单不是对象 → unknown", st, STATE_UNKNOWN)

    # --- GitHub 兜底解析 ---
    gh = {"tag_name": "v0.2.0", "body": "## 更新内容\n\n- 第一条说明。\n- 第二条",
          "assets": [{"name": "campus-login.exe", "size": 123}]}
    r = parse_github_release(gh, base_url="https://a.b")
    ck("GitHub tag 的 v 前缀去掉", r["version"], "0.2.0")
    ck("GitHub 取第一条 bullet", r["notes"], "第一条说明")
    ck("GitHub 下载地址指向主域名", r["url"], "https://a.b/dl/campus-login-0.2.0.exe")
    ck("GitHub 不给 sha256（必须留空）", r["sha256"], "")
    ck("GitHub 无 tag → None", parse_github_release({"body": "x"}), None)
    ck("GitHub 非字典 → None", parse_github_release("x"), None)

    # 兜底解析出来的东西必须是「不能直接下载」的：sha256 为空 → evaluate 判 unknown。
    # 这条锁住的是「兜底路径不许绕过校验」这个铁律，改坏了这里会红。
    if r:
        st, _ = evaluate({"version": r["version"], "url": r["url"],
                          "sha256": r["sha256"]}, "0.1.0")
        ck("兜底清单没有 sha256 → 拒绝下载", st, STATE_UNKNOWN)

    # --- 兜底判定：**绝不许返回 CURRENT** ---
    # 这一组是本模块里唯一能抓住那个「假的最新版本」静默失败的测试。
    # 真实场景（2026-09-18 实测）：主域名没部署 → 解析失败 → 走兜底 →
    # GitHub 上恰好是 v0.1.0 == 本地 0.1.0 → 老代码返回 CURRENT →
    # 界面显示「已是最新版本」，而真实情况是「主站根本没查通」。
    same = {"tag_name": "v0.1.0", "body": "- x"}
    st, msg = judge_fallback(same, "0.1.0")
    ck("兜底：版本相同 → 不是 CURRENT", st, STATE_UNKNOWN)
    ck("兜底：版本相同的提示里有「无法确认」", "无法确认" in msg, True)

    older = {"tag_name": "v0.0.9", "body": "- x"}
    st, _ = judge_fallback(older, "0.1.0")
    ck("兜底：版本更旧 → 也不是 CURRENT", st, STATE_UNKNOWN)

    ahead = {"tag_name": "v0.2.0", "body": "- 新功能。"}
    st, msg = judge_fallback(ahead, "0.1.0")
    ck("兜底：有更新版本 → MANUAL（不是 NEWER，没有 sha256 不能自动下）",
       st, STATE_MANUAL)
    ck("兜底：MANUAL 的提示里带官网地址", BASE_URL_HINT in msg, True)

    ck("兜底：解析不出来 → unknown", judge_fallback({"body": "x"}, "0.1.0")[0],
       STATE_UNKNOWN)
    ck("兜底：版本无法比较 → unknown",
       judge_fallback({"tag_name": "abc"}, "0.1.0")[0], STATE_UNKNOWN)

    # --- 源码运行时的 None 路径 ---
    # 这几条锁的是「传 None 不该炸」。源码运行时 exe 就是 None，
    # 每次启动都会走到这儿 —— 崩了虽然被 try/except 兜住，但会往日志里
    # 灌一条 TypeError，把真错误淹掉（实测踩到过）。
    ck("old_path_of(None) 不炸", old_path_of(None), None)
    ck("old_path_of('') 不炸", old_path_of(""), None)
    # 不传 work_dir 时仍在 exe 同目录（老调用方行为不变）。
    # ⚠️ 用 normcase 比 —— work_dir_for 走 abspath，会把正斜杠规范成反斜杠，
    #    直接比字符串会假红。
    ck("old_path_of 默认仍在 exe 同目录",
       os.path.normcase(old_path_of("C:/a/x.exe")),
       os.path.normcase(os.path.join(os.path.dirname("C:/a/x.exe"), "x.exe.old")))
    ck("cleanup_stale(None) 返回 None", cleanup_stale(None), None)
    ck("cleanup_stale('') 返回 None", cleanup_stale(""), None)

    # --- 临时文件不许再丢在 exe 同目录（2026-09-20 用户反馈）---
    # 用户常把 exe 放桌面；替换时桌面上会留下 .old（几 MB 的旧程序本体），
    # 下载期间还有 .new.part。对不懂的人来说就是「莫名多了个怪文件，又不敢删」。
    # 这一组锁的是「work_dir 与 exe 同卷 → 临时文件必须落在 work_dir」。
    import tempfile
    t = tempfile.mkdtemp(prefix="upd_workdir_")
    try:
        exe_dir = os.path.join(t, "fake_desktop")
        work = os.path.join(t, "fake_data", "updates")
        os.makedirs(exe_dir)
        fake = os.path.join(exe_dir, "app.exe")
        with open(fake, "wb") as f:
            f.write(b"MZ")

        ck("同卷时 work_dir_for 用 work_dir（并自动建出来）",
           os.path.normcase(work_dir_for(fake, work)), os.path.normcase(work))
        ck("同卷时 .old 落在 work_dir 里，不在 exe 同目录",
           os.path.normcase(os.path.dirname(old_path_of(fake, work))),
           os.path.normcase(work))
        ck("不传 work_dir 时退回 exe 同目录（老行为不变）",
           os.path.normcase(work_dir_for(fake)), os.path.normcase(exe_dir))

        # 跨卷必须退回 exe 同目录：Windows 的 MoveFileEx 不带 COPY_ALLOWED，
        # os.replace 跨卷会直接失败 —— 那样替换 exe 永远不成功。
        other = "Y:\\" if os.path.splitdrive(exe_dir)[0].lower() != "y:" else "Z:\\"
        ck("跨卷时退回 exe 同目录",
           os.path.normcase(work_dir_for(fake, other)), os.path.normcase(exe_dir))

        # 真跑一次替换，看 .old 到底落在哪边
        new = os.path.join(work, "app.exe.new")
        with open(new, "wb") as f:
            f.write(b"MZNEW")
        ok, why = apply_update(fake, new, work)
        ck("apply_update 成功（%s）" % why, ok, True)
        with open(fake, "rb") as f:
            ck("替换后 exe 是新内容", f.read(), b"MZNEW")
        ck("替换后 **exe 同目录没有** .old",
           [x for x in os.listdir(exe_dir) if x.endswith(OLD_SUFFIX)], [])
        ck("替换后 .old 在 work_dir 里",
           [x for x in os.listdir(work) if x.endswith(OLD_SUFFIX)],
           ["app.exe" + OLD_SUFFIX])
        ck("cleanup_stale 能清掉它", cleanup_stale(fake, work), "app.exe" + OLD_SUFFIX)
        ck("清完之后 work_dir 里没有 .old",
           [x for x in os.listdir(work) if x.endswith(OLD_SUFFIX)], [])

        # 备用名（apply_update 在 `.old` 被占着时用的带时间戳名字）也必须清得掉。
        # 写死文件名就会漏掉它 —— 那种残骸会永远躺在数据目录里、越积越多。
        alt = os.path.join(work, "app.exe" + OLD_SUFFIX + ".1758336000")
        with open(alt, "wb") as f:
            f.write(b"x")
        ck("cleanup_stale 认带时间戳的备用名（.old.<ts>）",
           cleanup_stale(fake, work), "app.exe" + OLD_SUFFIX + ".1758336000")
        ck("  备用名清掉了",
           [x for x in os.listdir(work) if x.startswith("app.exe" + OLD_SUFFIX)], [])

        # .new / .new.part 残骸：下载中途被强杀、或替换失败没清，都会留下它们。
        for nm in ("app.exe.new", "app.exe.new.part"):
            with open(os.path.join(work, nm), "wb") as f:
                f.write(b"x")
        ck("cleanup_stale 顺手清掉 .new / .new.part 残骸",
           cleanup_stale(fake, work), "app.exe.new")
        ck("  .new / .new.part 都没了",
           [x for x in os.listdir(work) if ".new" in x], [])

        # 老版本（≤0.3.2）的下载文件名不含 exe 名，落在 exe 同目录 ——
        # 那些用户升级后还躺在桌面上，必须一并收掉，否则等于没修。
        with open(os.path.join(exe_dir, "campus-login-0.3.2.new"), "wb") as f:
            f.write(b"x")
        ck("cleanup_stale 收得掉老命名的历史残留",
           cleanup_stale(fake, work), "campus-login-0.3.2.new")
    finally:
        for r_, ds_, fs_ in os.walk(t, topdown=False):
            for x in fs_:
                try:
                    os.remove(os.path.join(r_, x))
                except OSError:
                    pass
            for x in ds_:
                try:
                    os.rmdir(os.path.join(r_, x))
                except OSError:
                    pass
        try:
            os.rmdir(t)
        except OSError:
            pass

    print("  [%s] 自检 %d 项" % ("OK" if not fails else "失败", n[0]))
    for f in fails:
        print("     !! %s" % f)
    return not fails


if __name__ == "__main__":
    print("=== updater 自检 ===")
    sys.exit(0 if _selftest() else 1)
