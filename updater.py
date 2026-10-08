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
import threading
import time
import urllib.error
import urllib.request

# 发布清单的地址。域名定下来之后改这一行即可（也可以由调用方传入覆盖）。
# 域名 = hobson2233.dpdns.org（DigitalPlat 免费二级域名，NS 托管在 Cloudflare），
# 站点托管在 **Cloudflare Worker `campus-login` 的静态资源**上（⚠️ 不是 Pages 项目，
# 原 Pages 已并入 Workers；自定义域名绑的是**服务名**，所以那个名字不能改）。
# 改这里时记得同步改 make_manifest.py 的 BASE_URL。
MANIFEST_URL = "https://hobson2233.dpdns.org/version.json"

# GitHub 仓库。**三处副本**：这里、make_manifest.py（拼第二个下载源）、
# make_site.py（页面上的 GitHub 链接）。repo/ 会被单独打包，不能 import 工作区
# 根目录的东西，所以只能保留副本 —— 和站点域名同一个处理办法（见
# domain_drift_test.py 的说明）。⚠️ 这里的值**必须**和那两个文件一致，
# 写错不会报错，只会让第二个下载源静默 404（客户端会退回主站，看不出来）。
GITHUB_REPO = "Hobson2233/Hobson-s-toys"

# GitHub Release 里那个 exe 的**资产名**。注意它**不带版本号** ——
# 是发布脚本上传时定的，别照主站 dl/ 的命名习惯想当然。
GITHUB_ASSET = "campus-login.exe"

# 兜底清单地址：主域名被回收 / 解析不了时还能查到版本号。
# 只有 GitHub 的 api 域名在国内可用性还不错（raw.* 和 release 下载都实测过很差），
# 这里只用来**查版本号**，下载仍旧走主域名 —— 所以这条兜底不需要下载能力。
# 实测数据见 .workbuddy-ai/memory/2026-09-18.md「GitHub Release 下载基本不可用」一节。
#
# ⚠️ 2026-10-08 起：**下载**也能走 GitHub 了（清单里的 urls 第二项），
#    和这条兜底是两回事 —— 兜底只管查版本号、没有 sha256、永远不用于下载。
FALLBACK_MANIFEST_URL = ("https://api.github.com/repos/%s/releases/latest"
                         % GITHUB_REPO)

# MANIFEST_URL 去掉 /version.json 之后的站点根，用来拼下载地址给用户看。
BASE_URL_HINT = MANIFEST_URL.rsplit("/", 1)[0]

UA = "CampusLogin-Updater/1.0"
TIMEOUT = 10

# 允许的最大安装包体积。防的是「清单被改坏 / 指向了一个几百 MB 的文件」
# 这种把用户流量耗光的情况 —— 我们的 exe 一直稳定在 10 MB 上下。
MAX_SIZE = 60 * 1024 * 1024

OLD_SUFFIX = ".old"

# 更新残留「多老才算废料」。比它新的 `.new` / `.new.part` **一律不许动**。
#
# 🔴 这条是 2026-09-20 实测踩出来的，不是保守估计：
#    启动清理原本无条件删 `<exe名>.new` / `.new.part`。于是只要在**一次更新进行中**
#    有第二个实例启动（守护进程、用户又双击了一次图标、开机自启任务），
#    它的启动清理就会把第一个实例**刚下好的 12 MB 安装包删掉** ——
#    接着 apply_update 的 os.replace 报 `WinError 2 系统找不到指定的文件`，
#    更新整个失败（用户看到「升级失败」/「桌面没有出现新版本」）。
#    复现记录：_repro_real_path.py，日志里能直接看到那句 WinError 2。
#
# 为什么是 10 分钟：12 MB 的包正常几十秒就下完，连上重试 3 次也远用不到 10 分钟。
# 所以「比 10 分钟还新」几乎必然是**正在下载**，不是残骸。
#
# ⚠️ 只对 `.new` / `.part` 生效。`.old` 不设门槛：它只可能是「替换已经完成」留下的
#    废料（替换前 apply_update 自己会先 _unlink 一遍），而且替换进行中它是被
#    当前进程占用的、本来也删不掉。
STALE_MIN_AGE = 10 * 60

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

# ---- 多源下载（2026-10-08 加，0.4.4）----------------------------------------
#
# 起因：用户反馈「室友在软件里检查更新，网络正常但下载超级慢」。
# 查下来是两件事叠在一起：
#   ① 主站是 Cloudflare Worker 的静态资源，**不支持 Range**（实测 Range 请求
#      返回的是 200 + 完整文件，不是 206）→ 断了就得从 0 重来。
#   ② 更新器显式不走系统代理（见模块开头第 1 条），用户如果靠代理/VPN 才快，
#      程序偏偏直连 —— 慢是必然的。
# 所以：给清单加第二个源（GitHub Release，**支持 Range**），下载前各测一下速度，
# 从快的那个下，失败了自动换另一个。谁快完全取决于用户所在网络，猜不得。
#
# 🔴 探测**不需要服务端支持 Range**：只读开头一小段就主动断开，普通的 200
#    响应也能测。别把「能不能续传」和「能不能测速」混成一件事。
#
# 为什么要有最小采样时长：一条 200 Mbps 的线路读 256 KB 只要 10 毫秒，
#    这点时间全被 TCP 慢启动和调度抖动吃掉，量出来的数能差好几倍。
#    所以「读满 PROBE_MAX_BYTES **或** 读够 PROBE_MIN_SECONDS」谁先满足就停。
#    ⚠️ 握手时间不算进吞吐（t0 取在 open() 之后）—— 和 speed_probe 踩过的坑
#       同源：把 TLS 握手算进去，16.8 Mbps 的线路会量成 2.4 Mbps，而两个数
#       看着都「像正常数字」，根本发现不了。
PROBE_MIN_SECONDS = 0.5
PROBE_MAX_BYTES = 256 * 1024
PROBE_TIMEOUT = 6

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


def _clean_urls(raw, primary):
    """把清单里的 `urls` 收拾成一个可用的列表。

    - 缺失 / 不是列表 / 项不是字符串 → 跳过（老清单只有 `url` 字段，很正常）
    - 只留 http(s) 开头的：清单是**从网上取回来的**，不该让它把我们引到
      `file:///` 之类的本地协议上去。多一道过滤不亏。
    - **`primary`（也就是 `url`）一定排第一**。老客户端只读 `url`、新客户端读
      `urls`，两边必须从同一个源开始 —— 否则同一份清单在两种客户端上首选源
      不同，出了事没法对照复现。
    """
    out = []
    if isinstance(raw, list):
        for u in raw:
            if not isinstance(u, str):
                continue
            u = u.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                continue
            if u not in out:
                out.append(u)
    if primary:
        if primary in out:
            out.remove(primary)
        out.insert(0, primary)
    return out


def _host_of(url):
    """从地址里取主机名，给用户看的（「从 github.com 下载」）。取不到就原样返回。"""
    try:
        rest = url.split("//", 1)[-1]
        return rest.split("/", 1)[0] or url
    except Exception:
        return url


def candidate_urls(info):
    """从 `evaluate()` 给的 info 里取出全部下载源。返回列表，第一项是主源。

    优先 `urls`（0.4.4 起的清单），没有就退回 `url`（老清单 / 老调用方）。
    """
    if not isinstance(info, dict):
        return []
    return _clean_urls(info.get("urls"), (info.get("url") or "").strip())


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
        # 多源列表。老清单里没有 urls → 这里就是 [url] 一项，调用方不必分情况。
        "urls": _clean_urls(manifest.get("urls"), url),
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


def _probe(url, timeout=PROBE_TIMEOUT):
    """读一小段来估这个源的吞吐。返回字节/秒；测不出来返回 None。

    只读开头一小段就主动断开 —— **不需要服务端支持 Range**，普通 200 也能测。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _opener().open(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            # ⚠️ 用 perf_counter 而不是 time.time()：后者在 Windows 上只有
            #    ~15ms 的粒度，一个「秒回」的源两次读可能落在同一个刻度上，
            #    dt 量成 0 —— 于是最快的源被判成「测不出来」、排到**最后**去试，
            #    结果正好反了。perf_counter 是单调的高精度计数器，没有这个问题。
            #    （这条是 2026-10-08 写测试时踩出来的，不是推理出来的。）
            t0 = time.perf_counter()   # ⚠️ 取在 open() **之后**，握手不计入吞吐
            got = 0
            while got < PROBE_MAX_BYTES:
                b = r.read(1 << 15)
                if not b:
                    break
                got += len(b)
                if time.perf_counter() - t0 >= PROBE_MIN_SECONDS:
                    break
            dt = time.perf_counter() - t0
    except (OSError, ValueError):
        # 探测失败**不是错误**，也不该让这个源出局 —— 可能只是一次抖动或一次
        # 超时。调用方会把它排到最后去试（见 order_by_speed），而不是丢掉它。
        # （OSError 已经覆盖 URLError / HTTPError / SSLError，不用逐个列。）
        return None
    if got <= 0:
        return None
    if dt <= 0:
        # 计时器精度不够（极快的本地响应仍可能量到 0）→ 当成「非常快」。
        # 返回 None 会让最快的那个源被排到最后，正好反了。
        dt = 1e-6
    return got / dt


def probe_sources(urls, timeout=PROBE_TIMEOUT):
    """**并发**探测每个源的吞吐。返回 [(url, 字节每秒或 None), ...]，顺序与输入一致。

    为什么并发：串行的话总耗时是各源之和，而「连不上」在国内是常态
    （连 github.com 超时很常见），一个源就要白等满 timeout 才轮到下一个 ——
    用户点完「是」之后干等十几秒，看着就是卡死。并发时总耗时是**最慢的那个**。

    线程都是 daemon、各自带 socket 超时，不会把进程吊住。
    """
    results = [None] * len(urls)

    def one(i, u):
        results[i] = _probe(u, timeout)

    threads = []
    for i, u in enumerate(urls):
        t = threading.Thread(target=one, args=(i, u), daemon=True)
        t.start()
        threads.append(t)
    # 统一给一个总期限，别一个线程一个期限地累加。
    deadline = time.time() + timeout + 2
    for t in threads:
        t.join(max(0.05, deadline - time.time()))
    return list(zip(urls, results))


def order_by_speed(pairs):
    """按速度从快到慢排。**测不出速度的一律排在最后，但不丢掉。**

    丢掉就等于「另一个源本来能下，被我们主动排除了」。探测失败往往只是一次
    抖动，重试一次可能就成了 —— 所以这里是**排序**，不是筛选。

    全测不出来时保持原顺序（第一项仍是主源）。
    """
    known = sorted([p for p in pairs if p[1]], key=lambda p: p[1], reverse=True)
    unknown = [p[0] for p in pairs if not p[1]]
    return [p[0] for p in known] + unknown


def download_multi(urls, dest, sha256=None, size=None, timeout=TIMEOUT,
                   on_progress=None, on_retry=None, on_pick=None, probe=True):
    """多源下载：先并发测速挑最快的，失败就换下一个源。

    返回 `(ok, 原因, 用过的源)`。失败时第三项是**最后试的那个地址** ——
    调用方拿它给用户一个「用浏览器打开下载页」的出口。

    `on_pick(url, 第几个, 共几个)` 在**每次尝试之前**回调，让界面能说清
    「现在从哪儿下」。回调抛异常会被吞掉：它只是提示，不该带崩下载。

    🔴 只在**失败**时换源，不在**慢**的时候换源。
       主站不支持 Range，换源 = 已下的字节全丢、从 0 重来。一个 60 KB/s 但
       一直在走的连接，比「切来切去、每次下 10%」快得多。慢由测速那一步解决，
       失败才由换源解决 —— 两件事别混在一起。
    """
    urls = [u for u in (urls or []) if u]
    if not urls:
        return False, "清单里没有可用的下载地址", ""

    if len(urls) == 1 or not probe:
        order = urls
    else:
        order = order_by_speed(probe_sources(urls, timeout))

    tried = []
    for i, u in enumerate(order):
        if on_pick:
            try:
                on_pick(u, i + 1, len(order))
            except Exception:
                pass
        ok, reason = download(u, dest, sha256, size, timeout=timeout,
                              on_progress=on_progress, on_retry=on_retry)
        if ok:
            return True, None, u
        tried.append("%s：%s" % (_host_of(u), reason))
    return False, "所有下载源都失败（%s）" % "；".join(tried), order[-1]


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


def _age(path):
    """文件有多旧（秒）。读不到属性返回 None（当「不知道」处理）。"""
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def cleanup_stale_all(exe, work_dir=None, min_age=STALE_MIN_AGE):
    """删掉更新残留。返回 `(删掉的路径列表, 没删掉的路径列表)`。

    为什么要拆出这个：老签名 cleanup_stale 只报「删掉的第一个名字」，
    调用方没法知道**还剩什么** —— 而后台重试恰恰需要这个信息。
    （同类的坑：把「没报错」当成「清干净了」。）

    🔴 `.new` / `.new.part` 加**年龄门槛**：比 `min_age` 新的不动。
       理由见 STALE_MIN_AGE —— 那可能是另一个实例正在下载的文件，
       删掉它会让那次更新直接失败（2026-09-20 实测）。
    ⚠️ `.old` 不设门槛：它只可能是替换完成后的废料，见 STALE_MIN_AGE。

    删不掉**不是异常**：有别的实例正从那个文件跑着（典型：守护进程还活着），
    Windows 会拒绝删除。这种要交给调用方重试，所以这里把 left 一并返回。
    """
    if not exe:
        return [], []
    base = os.path.basename(exe)
    exe_dir = os.path.dirname(os.path.abspath(exe)) or "."
    cands = []
    for d in (work_dir_for(exe, work_dir), exe_dir):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            is_old = name.startswith(base + OLD_SUFFIX)
            # 老版本（≤0.3.2）的下载文件叫 `campus-login-<版本>.new`，不含 exe 名，
            # 单独扫一遍，把这些历史残留也收掉。
            is_download = name.startswith(base + ".new") or (
                name.startswith("campus-login-")
                and (name.endswith(".new") or name.endswith(".new.part")))
            if not (is_old or is_download):
                continue
            p = os.path.join(d, name)
            if p in cands:
                continue
            if is_download:
                a = _age(p)
                if a is not None and a < min_age:
                    # 太新 = 很可能正在下载 → 碰它会毁掉那次更新
                    continue
            cands.append(p)
    removed, left = [], []
    for p in cands:
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            left.append(p)
    return removed, left


def cleanup_stale(exe, work_dir=None):
    """启动时调用：把上次更新留下的临时文件删掉。返回删掉的第一个文件名或 None。

    为什么现在才删：`.old` 在被替换的进程退出前一直是被占用的，当时删不掉，
    只能留到下次启动。

    ⚠️ **两个位置都要看**：0.3.2 及之前 `.old` 就丢在 exe 同目录（用户的桌面），
       那些老残留升级后还躺在那儿 —— 只清新位置等于没修好。
    ⚠️ 用**前缀匹配**，不是几个写死的文件名。apply_update 在 `.old` 被占着时
       会退到带时间戳的备用名 `app.exe.old.<时间戳>` —— 写死名字就会漏掉它，
       于是它永远躺在数据目录里、越积越多。
    ⚠️ **清不掉是常态，不是错误**：有别的实例（守护进程）正从那个文件跑着。
       所以这个函数只报「删掉了什么」，**不报「还剩什么」** —— 需要那个信息
       用 cleanup_stale_all。调用方（campus_login）会在后台重试，
       别把这里的一次性结果当成「清干净了」。
    """
    removed, _left = cleanup_stale_all(exe, work_dir)
    return os.path.basename(removed[0]) if removed else None


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


def source_hint():
    """给用户看的一句话：更新包是从哪儿来的。

    为什么要有（2026-09-21）：用户点「检查更新」时界面上看不到任何地址，
    只能凭信任点下去。一个要联网下载、还会**替换自身文件**的程序，
    至少该说清它会去哪儿取文件 —— 这比多写十句「请放心」都管用。

    ⚠️ 域名不在这里硬编码：唯一来源是上面的 MANIFEST_URL，改一处就够。
       （HELP_TEXT 里那份写死的域名是「给用户在归档里搜得到的副本」，
        由 help_check.py 拿这里的 BASE_URL_HINT 反算比对，防漂移。）
    """
    host = BASE_URL_HINT.split("//", 1)[-1]
    return "更新源：%s 与 GitHub（下载前各测一下速度，从快的那个下）" % host


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

    # --- 多源选路（2026-10-08 加，0.4.4）---
    # 这一组全是**纯函数**，不联网。理由是「哪个源快」取决于用户所在网络，
    # 本机测出来的结论对别人没用 —— 能测的是**规则**，不是结果。
    # 真实选路那部分由 updater_test.py 的假服务覆盖（要真回环，MANUAL 档）。
    ck("urls 缺失 → 退回 url", _clean_urls(None, "https://a/x"), ["https://a/x"])
    ck("urls 不是列表 → 退回 url",
       _clean_urls("https://a/x", "https://a/x"), ["https://a/x"])
    ck("urls 去重保序",
       _clean_urls(["https://a/x", "https://a/x"], "https://a/x"), ["https://a/x"])
    # 清单是从网上取回来的，不该让它把我们引到本地协议上去。
    ck("urls 只留 http(s)",
       _clean_urls(["ftp://a/x", "file:///c:/x", "https://b/y"], "https://a/x"),
       ["https://a/x", "https://b/y"])
    ck("非字符串项丢掉",
       _clean_urls([1, None, "https://b/y"], "https://a/x"),
       ["https://a/x", "https://b/y"])
    # 老客户端只读 url、新客户端读 urls，两边必须从同一个源开始。
    ck("★ primary（url）一定排第一",
       _clean_urls(["https://b/y", "https://a/x"], "https://a/x"),
       ["https://a/x", "https://b/y"])

    pairs = [("https://slow/x", 1000.0), ("https://fast/x", 900000.0),
             ("https://dead/x", None)]
    ck("按速度从快到慢排", order_by_speed(pairs),
       ["https://fast/x", "https://slow/x", "https://dead/x"])
    # 丢掉测不出来的源 = 「另一个源本来能下，被我们主动排除了」。
    ck("★ 测不出速度的源不许被丢掉", len(order_by_speed(pairs)), 3)
    ck("全测不出来 → 保持原顺序",
       order_by_speed([("https://a/x", None), ("https://b/y", None)]),
       ["https://a/x", "https://b/y"])

    ck("candidate_urls 认 urls 字段",
       candidate_urls({"url": "https://a/x",
                       "urls": ["https://a/x", "https://b/y"]}),
       ["https://a/x", "https://b/y"])
    ck("candidate_urls 认老清单（只有 url）",
       candidate_urls({"url": "https://a/x"}), ["https://a/x"])
    ck("candidate_urls 空 info → 空列表", candidate_urls({}), [])
    ck("candidate_urls 非字典 → 空列表", candidate_urls(None), [])

    st, info = evaluate({"version": "0.2.0", "url": "https://a/x", "sha256": "a" * 64,
                         "urls": ["https://b/y", "https://a/x"]}, "0.1.0")
    ck("evaluate 透传 urls，并把 url 排第一", info["urls"],
       ["https://a/x", "https://b/y"])
    st, info = evaluate({"version": "0.2.0", "url": "https://a/x",
                         "sha256": "a" * 64}, "0.1.0")
    ck("老清单没有 urls → info 里也有一项", info["urls"], ["https://a/x"])

    ck("_host_of 取主机名", _host_of("https://github.com/a/b"), "github.com")
    ck("_host_of 对畸形串不炸", _host_of("乱七八糟"), "乱七八糟")

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

        # --- 正在下载的文件不许被清掉（2026-09-20 实测踩到）---
        # 场景：一次更新正在下 12 MB，此时第二个实例启动（守护进程 / 用户又双击了
        # 图标 / 开机自启任务），它的启动清理把第一个实例刚下好的 `.new` 删掉 ——
        # 接着 os.replace 报 WinError 2「系统找不到指定的文件」，更新整个失败，
        # 用户看到的是「升级失败 / 桌面没有出现新版本」。所以新的下载文件必须留着。
        fresh = os.path.join(work, "app.exe.new")
        with open(fresh, "wb") as f:
            f.write(b"x")
        ck("★ 刚下好的 .new 不许被清掉（可能正在下载）",
           cleanup_stale(fake, work), None)
        ck("  它确实还在", os.path.isfile(fresh), True)

        # 反过来：明显过期的 `.new` / `.new.part` 是残骸，必须收掉。
        # 用改 mtime 的方式把它变旧 —— 比 sleep 快，也不依赖时钟精度。
        def _backdate(p, secs=STALE_MIN_AGE + 60):
            t_old = time.time() - secs
            os.utime(p, (t_old, t_old))

        for nm in ("app.exe.new", "app.exe.new.part"):
            p = os.path.join(work, nm)
            with open(p, "wb") as f:
                f.write(b"x")
            _backdate(p)
        # ⚠️ 断言用**集合**，不用「第一个删掉的名字」：候选是按 os.listdir 顺序扫的，
        #    顺序不保证，拿第一个名字做断言会随机假红。
        rem, _left = cleanup_stale_all(fake, work)
        ck("cleanup_stale 收掉过期的 .new / .new.part 残骸",
           sorted(os.path.basename(x) for x in rem),
           ["app.exe.new", "app.exe.new.part"])
        ck("  .new / .new.part 都没了",
           [x for x in os.listdir(work) if ".new" in x], [])

        # 老版本（≤0.3.2）的下载文件名不含 exe 名，落在 exe 同目录 ——
        # 那些用户升级后还躺在桌面上，必须一并收掉，否则等于没修。
        legacy = os.path.join(exe_dir, "campus-login-0.3.2.new")
        with open(legacy, "wb") as f:
            f.write(b"x")
        _backdate(legacy)
        ck("cleanup_stale 收得掉老命名的历史残留",
           cleanup_stale(fake, work), "campus-login-0.3.2.new")

        # cleanup_stale_all 必须把「没删掉的」也报出来 —— 后台重试靠它决定要不要再试。
        # 造一个删不掉的：建个同名目录，os.remove 对目录会抛 OSError。
        stuck = os.path.join(work, "app.exe.old")
        os.makedirs(stuck)
        rem2, left2 = cleanup_stale_all(fake, work)
        ck("cleanup_stale_all 报出删不掉的（重试的依据）",
           [os.path.basename(x) for x in left2], ["app.exe.old"])
        ck("  没删掉的不会同时出现在「删掉了」里",
           [x for x in rem2 if os.path.basename(x) == "app.exe.old"], [])
        os.rmdir(stuck)
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
