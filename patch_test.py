# -*- coding: utf-8 -*-
"""增量更新（0.5.0）的测试。

背景：完整包 10.6 MB，而**相邻版本之间真正变动的字节只占 5%**（实测 0.4.4→0.4.5
是 551 KB / 5.0%）。所以 0.5.0 内嵌一个 515 KB 的 `hpatchz.exe`（HDiffPatch，MIT），
更新时只下几百 KB 的补丁，在本地把新程序还原出来。

这个机制有**三处写错了也不报错**的地方，本测试专门钉住它们：

  1) 🔴 **降级必须是正常路径，不是错误。** 补丁缺失 / 工具缺失 / 下载失败 /
     还原失败 / 哈希对不上 —— 任何一条都只能「安静地退回完整下载」，
     绝不能让「更新」整个不可用。第 3 节用假工具把每条路都走一遍。
     （这也是为什么「先试补丁、失败降级」这段判断被从界面里抽到了 updater ——
     降级分支只在补丁出岔子时才会走到，靠手点界面撞上太难了。）

  2) 🔴 **真正的安全网是「还原结果」的 sha256，不是补丁自己的。**
     补丁的哈希只能说明「补丁没下坏」，说明不了「还原出来的是我们想要的程序」。
     所以第 2 节的重点不是成功路径，而是**失败时不许留下半成品** ——
     一个来路不明的 `.new` 躺在磁盘上，比没有它更危险。

  3) 🔴 **清单说有的补丁，站点上必须真的有；站点上有的，清单里也得有。**
     第 4 节做双向核对（跨模块：`make_site.py` 产出、`updater.py` 消费）。
     对不上的后果是「用户那边一条补丁都挑不到」—— 不报错，只是每次照旧下完整包，
     于是这个功能白做，而且没人会发现。

第 5 节是**真工具端到端**：拿 `dl_archive/` 里的旧版 + `site/patch/` 里的真补丁，
真跑一遍 hpatchz，断言还原出来的 sha256 等于 `site/versions.json` 里那一版记的。
这一节最值钱 —— 它是唯一能证明「发出去的补丁真的装得上」的检查。
⚠️ 找不到产物时**明说跳过**，不静默通过（`site/` 与 `dl_archive/` 是构建产物，
   单独克隆 `repo/` 的人不会有它们）。

⚠️ 本测试不碰真实数据目录（只写临时目录、不建窗口、不调 write_json）。
   仍然挂一层 `datasafe` 沙箱 + 收尾核对，理由是项目惯例：将来有人往这里加一行
   写盘代码时，那一道闸会立刻响，而不是安静地把用户数据改掉。

用法：
    python patch_test.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import datasafe          # noqa: E402
import updater as U      # noqa: E402

FAILS = []

# `repo/tools/` 里那两个文件与 HDiffPatch 官方 v5.1.3 Windows64 发行包**逐字节一致**
# （2026-10-08 核对过）。写死在这里是有意的：哪天工具被换掉，这条会红，
# 逼人**有意识地**更新这个值，而不是让一份来路不明的 exe 悄悄跟着发出去。
HPATCHZ_SHA256 = "9703c694b5955c576d9f0e26e98b60941f0bbb53b382b1f1988d75d461e580cb"


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-56s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print("  [%s] %-56s %s" % ("OK" if cond else "失败", name, detail))
    if not cond:
        FAILS.append(name)


def parse_patch_name(fn):
    """`0.4.4-to-0.5.0.hpatch` → ("0.4.4", "0.5.0")。解析不出返回 (None, None)。

    补丁文件名是**自描述**的：`make_manifest.py` 靠扫目录 + 解析文件名得出清单，
    所以「清单里有、站点上没有」这种错位不会发生。这里用同一套解析规则。
    """
    if not fn.endswith(".hpatch"):
        return None, None
    body = fn[:-len(".hpatch")]
    if "-to-" not in body:
        return None, None
    frm, to = body.split("-to-", 1)
    return frm.strip(), to.strip()


# ---------------------------------------------------------------- 替换工具
#
# 全部做成上下文管理器：用完**必须**还原。这几处替换的都是模块级名字
# （updater.subprocess / updater.download / updater.hpatchz_path），
# 漏还原的话后面几节（尤其真工具那节）会拿着假工具去跑，结论全废。

class patch_attr(object):
    """临时把 `obj.name` 换成别的值，退出时还原。"""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, *a):
        setattr(self.obj, self.name, self.old)
        return False


class Recorder(object):
    """记录调用、按预设返回。用来断言「这个方法该不该被调用」。"""

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self.result


class FakeSub(object):
    """替换 updater 里那个 `subprocess` 名字。

    `apply_patch` 用它跑 hpatchz，`_no_window()` 也从它身上取 CREATE_NO_WINDOW，
    所以这个替身得把这两个属性都带上。

    ⚠️ `CREATE_NO_WINDOW` 故意写成**真值**（0x08000000）而不是 0：
        `apply_patch` 把 `creationflags=_no_window()` 传给子进程，
        用真值才能验出「这个参数真的传了」。写成 0 的话，参数被删掉也是 0，白测。
    """
    CREATE_NO_WINDOW = 0x08000000
    PIPE = subprocess.PIPE
    STDOUT = subprocess.STDOUT
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append((argv, kw))
        return self.behavior(argv, kw)

    def __enter__(self):
        self._orig = U.subprocess
        U.subprocess = self
        return self

    def __exit__(self, *a):
        U.subprocess = self._orig
        return False


class FakeDownload(object):
    """替换 `updater.download` —— 只影响「下补丁」这一步。

    真 download 会联网 + 重试 + 校验，这里只关心「成功 / 失败」两态。
    """

    def __init__(self, ok=True, reason="假失败", body=b"PATCH-BYTES"):
        self.ok, self.reason, self.body = ok, reason, body
        self.calls = []

    def __enter__(self):
        self._orig = U.download
        U.download = self._fake
        return self

    def _fake(self, url, dest, sha256=None, size=None, timeout=None,
              on_progress=None, on_retry=None):
        self.calls.append({"url": url, "dest": dest, "sha256": sha256, "size": size})
        if self.ok:
            with open(dest, "wb") as f:
                f.write(self.body)
            return True, None
        return False, self.reason

    def __exit__(self, *a):
        U.download = self._orig
        return False


def _writes(data):
    """造一个「假 hpatchz」：把 data 写进命令行最后一个参数（输出文件），退出码 0。"""
    def fn(argv, kw):
        with open(argv[-1], "wb") as f:
            f.write(data)
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok\n")
    return fn


# ================================================================ 0. 工具本身

def test_tools():
    print("\n[0] 内嵌工具：找得到、没被换掉、打包路径与运行期路径一致")

    hz = U.hpatchz_path()
    check_true("源码检出里能找到 hpatchz.exe", hz is not None, repr(hz))
    if hz:
        got = U.sha256_file(hz)
        check_true("  与 HDiffPatch 官方 v5.1.3 逐字节一致", got == HPATCHZ_SHA256,
                   "%s…" % got[:16])
        check_true("  名字就是 PATCH_NAME", os.path.basename(hz), U.PATCH_NAME)

    # 🔴 跨模块：build.py 往包里塞的路径，和 updater 运行期找的路径必须对得上。
    #    对不上的后果是「打包成功、运行期永远找不到工具」—— 每次更新都下完整包，
    #    不报任何错、用户看不出异常。所以这里直接读 build.py 的常量来比。
    try:
        import build as B
        check("build.py 塞进包的目录名 == updater 找的目录名", B.TOOL_SUBDIR, "tools")
        check("build.py 塞进包的文件名 == updater 找的文件名", B.TOOL_NAME, U.PATCH_NAME)
        check_true("  而且源文件真的在", os.path.isfile(
            os.path.join(HERE, B.TOOL_SUBDIR, B.TOOL_NAME)), B.TOOL_SUBDIR)
    except Exception as e:                                    # noqa: BLE001
        check_true("能读到 build.py 的工具常量", False, "%s: %s" % (type(e).__name__, e))


# ================================================================ 1. apply_patch

def test_apply_patch():
    print("\n[1] apply_patch：子进程的各种表现都要变成「一句话原因」，不许抛异常")

    tmp = tempfile.mkdtemp(prefix="patch_ap_")
    try:
        old = os.path.join(tmp, "old.exe")
        pf = os.path.join(tmp, "x.hpatch")
        out = os.path.join(tmp, "out.exe")
        for p in (old, pf):
            with open(p, "wb") as f:
                f.write(b"x")

        # ① 找不到工具。这是**正常情况**（源码运行、或哪天打包漏了文件），
        #    所以它必须是一条干净的 False，不是异常。
        with patch_attr(U, "hpatchz_path", lambda: None):
            ok, why = U.apply_patch(old, pf, out)
        check("找不到工具 → False", ok, False)
        check("  原因写清楚了", why, "找不到补丁工具")

        # ② 退出码非 0：hpatchz 把原因写在 stdout，最后一行通常最有信息量
        #    （比如 oldPath 不匹配）。
        def rc1(argv, kw):
            return subprocess.CompletedProcess(argv, 1,
                                               stdout=b"blah\noldPath not match\n")

        with open(out, "wb") as f:                # 先造一个「上一轮的残骸」
            f.write(b"STALE")
        with FakeSub(rc1) as fs:
            ok, why = U.apply_patch(old, pf, out)
        check("退出码 1 → False", ok, False)
        check_true("  原因带退出码", "退出码 1" in why, why)
        check_true("  原因带上工具的最后一行输出", "oldPath not match" in why, why)
        check("  ★ 失败后不留残骸", os.path.exists(out), False)
        check_true("  命令行带 -s-64m（给还原留足内存）",
                   bool(fs.calls) and fs.calls[0][0][1] == "-s-64m",
                   repr(fs.calls[0][0] if fs.calls else None))
        check_true("  ★ 不弹控制台窗口（--windowed 下不加这个会闪黑窗）",
                   bool(fs.calls) and
                   fs.calls[0][1].get("creationflags") == FakeSub.CREATE_NO_WINDOW,
                   repr(fs.calls[0][1] if fs.calls else None))

        # ③ 退出码 0，但没写出文件 —— 工具「说自己成功了」却没干活
        def rc0(argv, kw):
            return subprocess.CompletedProcess(argv, 0, stdout=b"")

        with FakeSub(rc0):
            ok, why = U.apply_patch(old, pf, out)
        check("退出码 0 但没产出文件 → False", ok, False)
        check("  原因写清楚了", why, "补丁工具没有产出文件")

        # ④ 超时：进程可能还在往 out 里写，所以先清掉再说
        def boom(argv, kw):
            raise subprocess.TimeoutExpired(argv, 5)

        with open(out, "wb") as f:
            f.write(b"STALE")
        with FakeSub(boom):
            ok, why = U.apply_patch(old, pf, out)
        check("超时 → False", ok, False)
        check("  原因写清楚了", why, "应用补丁超时")
        check("  ★ 超时后也不留残骸", os.path.exists(out), False)

        # ⑤ 工具根本起不来（文件被杀软删了、没权限…）
        def oserr(argv, kw):
            raise OSError(2, "系统找不到指定的文件")

        with FakeSub(oserr):
            ok, why = U.apply_patch(old, pf, out)
        check("启动不了工具 → False", ok, False)
        check_true("  原因带前缀", why.startswith("无法启动补丁工具"), why)

        # ⑥ 成功路径 + 反向对照：上面那一串「失败」如果没有这一条兜着，
        #    「永远返回 False」也能全过。
        with open(out, "wb") as f:
            f.write(b"STALE")
        with FakeSub(_writes(b"NEW-EXE")):
            ok, why = U.apply_patch(old, pf, out)
        check("★ 反向对照：工具正常 → True", ok, True)
        check("  没有原因", why, None)
        check("  先清掉上一轮残骸、再写新的",
              open(out, "rb").read(), b"NEW-EXE")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 2. try_patch_update

def test_try_patch_update():
    print("\n[2] try_patch_update：安全网是「还原结果」的 sha256，失败不许留半成品")

    tmp = tempfile.mkdtemp(prefix="patch_tpu_")
    try:
        base = "campus-login-0.4.4.exe"
        old = os.path.join(tmp, base)
        with open(old, "wb") as f:
            f.write(b"OLD-EXE")

        GOOD = b"THE-REAL-NEW-EXE"
        BAD = b"A-CORRUPTED-RESULT"
        info = {
            "version": "0.5.0",
            "sha256": hashlib.sha256(GOOD).hexdigest(),   # 期望的**还原结果**哈希
            "size": 1000000,
            "patches": [{
                "from": "0.4.4",
                "url": "https://hobson2233.dpdns.org/patch/0.4.4-to-0.5.0.hpatch",
                "sha256": hashlib.sha256(b"PATCH-BYTES").hexdigest(),
                "size": 5000,
            }],
        }
        out = os.path.join(tmp, base + ".new")
        pf = os.path.join(tmp, base + ".hpatch")

        # ★ 本节最重要的一条：还原出来的东西哈希对不上 → 必须删掉。
        #   「补丁的 sha256 对」只说明补丁没下坏，说明不了还原结果是我们想要的程序。
        with FakeDownload(ok=True), FakeSub(_writes(BAD)):
            ok, why, url = U.try_patch_update(old, info, "0.4.4", work_dir=tmp)
        check("还原结果哈希不符 → False", ok, False)
        check("  地址不给（调用方据此走完整下载）", url, None)
        check_true("  原因点明是校验不过", "校验不过" in why, why)
        check("  ★ 半成品必须被删掉", os.path.exists(out), False)
        check("  补丁文件用完即删", os.path.exists(pf), False)

        # 反向对照：同一套流程，工具这次写对了 → 必须成功。
        with FakeDownload(ok=True), FakeSub(_writes(GOOD)):
            ok, why, url = U.try_patch_update(old, info, "0.4.4", work_dir=tmp)
        check("★ 反向对照：哈希对得上 → True", ok, True)
        check("  没有原因", why, None)
        check("  返回补丁地址", url, info["patches"][0]["url"])
        check("  成品留在原地等 apply_update 接手", os.path.exists(out), True)
        check("  而且内容就是新的", open(out, "rb").read(), GOOD)
        os.remove(out)

        # 补丁下载失败 → 不该去试还原（省一步，也不留 .new）
        with FakeDownload(ok=False, reason="HTTP 404") as fd, \
                FakeSub(_writes(GOOD)) as fs:
            ok, why, url = U.try_patch_update(old, info, "0.4.4", work_dir=tmp)
        check("补丁下载失败 → False", ok, False)
        check_true("  原因带上了下载失败的原因",
                   "补丁下载失败" in why and "404" in why, why)
        check("  确实下了补丁（不是没跑）", len(fd.calls), 1)
        check("  ★ 没去还原", len(fs.calls), 0)
        check("  补丁残骸被清掉", os.path.exists(pf), False)

        # 工具缺失 → 安静失败。源码运行时天天走这条，它必须干净。
        with FakeDownload(ok=True) as fd, patch_attr(U, "hpatchz_path", lambda: None):
            ok, why, url = U.try_patch_update(old, info, "0.4.4", work_dir=tmp)
        check("找不到补丁工具 → False", ok, False)
        check("  原因写清楚了", why, "找不到补丁工具")
        check("  ★ 连补丁都不下（工具都没有，下了也没用）", len(fd.calls), 0)

        # 其余几条「安静失败」：一律 (False, 原因, None)，不许抛异常
        ok, why, url = U.try_patch_update(os.path.join(tmp, "没有这个.exe"),
                                          info, "0.4.4", work_dir=tmp)
        check("当前程序文件不存在 → False", (ok, url), (False, None))
        ok, why, url = U.try_patch_update(old, info, "0.3.6", work_dir=tmp)
        check("没有从当前版本出发的补丁 → False", (ok, url), (False, None))
        ok, why, url = U.try_patch_update(old, None, "0.4.4", work_dir=tmp)
        check("info 不是字典 → False", (ok, url), (False, None))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 3. download_update

def test_download_update():
    print("\n[3] download_update：先试增量，不行就安静退回完整下载")

    tmp = tempfile.mkdtemp(prefix="patch_du_")
    try:
        exe = os.path.join(tmp, "campus-login-0.4.4.exe")
        with open(exe, "wb") as f:
            f.write(b"OLD")
        dest = os.path.join(tmp, "new.exe")
        info = {"version": "0.5.0", "url": "https://s/dl/x.exe",
                "sha256": "a" * 64, "size": 1000000}
        PATCH_URL = "https://s/patch/0.4.4-to-0.5.0.hpatch"

        # ① 增量成功 → **绝不能**再去下完整包。这是「真的省了流量」的唯一判据：
        #    只看返回值是看不出来的（两条路都返回 True）。
        multi = Recorder((True, None, "https://s/full.exe"))
        with patch_attr(U, "try_patch_update", Recorder((True, None, PATCH_URL))), \
                patch_attr(U, "download_multi", multi):
            ok, why, used = U.download_update(exe, info, "0.4.4", dest, work_dir=tmp)
        check("增量成功 → True", ok, True)
        check("  来源是补丁地址", used, PATCH_URL)
        check("  ★ 没有再去下完整包", len(multi.calls), 0)

        # ② 增量失败 → 降级到多源完整下载，而且**不把原因当错误**：
        #    用户看到的应该只是「多下了一会儿」，不是「更新失败」。
        multi = Recorder((True, None, "https://s/full.exe"))
        logs = []
        with patch_attr(U, "try_patch_update",
                        Recorder((False, "补丁下载失败：HTTP 404", None))), \
                patch_attr(U, "fetch_mirrors", Recorder([])), \
                patch_attr(U, "download_multi", multi):
            ok, why, used = U.download_update(exe, info, "0.4.4", dest, work_dir=tmp,
                                              on_log=logs.append)
        check("增量失败 → 仍能更新成功", ok, True)
        check("  来源是完整包地址", used, "https://s/full.exe")
        check("  ★ 真的去下了完整包", len(multi.calls), 1)
        check_true("  ★ 降级写进日志（不是弹给用户的错误）",
                   any("改下完整包" in t for t in logs), "；".join(logs))

        # ③ exe / 当前版本为空（源码运行时）→ 直接走完整下载，不碰补丁
        for label, e, v in [("exe 为空", "", "0.4.4"), ("当前版本为空", exe, "")]:
            tpu = Recorder((False, "不该被调用", None))
            multi = Recorder((True, None, "https://s/full.exe"))
            with patch_attr(U, "try_patch_update", tpu), \
                    patch_attr(U, "fetch_mirrors", Recorder([])), \
                    patch_attr(U, "download_multi", multi):
                ok, why, used = U.download_update(e, info, v, dest, work_dir=tmp)
            check("%s → 走完整下载" % label, ok, True)
            check("  ★ 根本没去试补丁", len(tpu.calls), 0)
            check("  下了一次完整包", len(multi.calls), 1)

        # ④ 两条路都失败 → 原因要来自**完整下载**那一边。
        #    把补丁的原因丢出来是错的：用户明明还可以下完整包。
        with patch_attr(U, "try_patch_update", Recorder((False, "没有补丁", None))), \
                patch_attr(U, "fetch_mirrors", Recorder([])), \
                patch_attr(U, "download_multi",
                           Recorder((False, "所有下载源都失败（主站）", "https://s/full.exe"))):
            ok, why, used = U.download_update(exe, info, "0.4.4", dest, work_dir=tmp)
        check("两条路都失败 → False", ok, False)
        check_true("  原因来自完整下载（不是补丁那条）",
                   "所有下载源都失败" in why, why)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 4. 站点产物核对

def _site_dir():
    return os.path.join(os.path.dirname(HERE), "site")


def test_artifact():
    """`site/version.json` 的 patches ↔ `site/patch/` 里的文件，双向核对。

    这一节是跨模块的：`make_site.py`（工作区根目录）产出、`updater.py` 消费。
    两边对不上的后果是「用户那边一条补丁都挑不到」—— 不报错，只是每次照旧下
    完整包。这种「功能白做但没人发现」的失败，正是这类检查存在的理由。

    ⚠️ 找不到产物时**明说跳过**，不静默通过。
    """
    print("\n[4] 站点产物核对（version.json 的 patches ↔ site/patch/）")

    site = _site_dir()
    vj = os.path.join(site, "version.json")
    pdir = os.path.join(site, "patch")
    if not os.path.isfile(vj):
        print("  [跳过] %s 不存在 —— 跑一次 python make_site.py 才有" % vj)
        return
    with open(vj, encoding="utf-8") as f:
        data = json.load(f)

    ver = data.get("version")
    full = data.get("size")
    entries = data.get("patches")
    on_disk = sorted(f for f in os.listdir(pdir) if f.endswith(".hpatch")) \
        if os.path.isdir(pdir) else []

    if not entries:
        # 没有补丁是**正常状态**（这一版跟前一版差太多时，全被比例筛掉了）。
        print("  [信息] 清单里没有 patches —— 当前版本 %s 没发布补丁；"
              "站点上 %d 个补丁文件" % (ver, len(on_disk)))
        return

    check_true("patches 是列表", isinstance(entries, list), repr(type(entries)))
    if not isinstance(entries, list):
        return

    base = (data.get("url") or "").rsplit("/", 2)[0]
    check_true("能从清单里推出站点根地址", base.startswith("http"), base)

    seen_from = []
    for it in entries:
        if not isinstance(it, dict):
            check("每一项都是字典", False, repr(it))
            continue
        frm = it.get("from")
        fn = "%s-to-%s.hpatch" % (frm, ver)
        seen_from.append(frm)

        # 客户端**真能挑到**这一条吗 —— 直接跑 pick_patch，不是看字段名猜。
        picked = U.pick_patch(data, frm)
        check_true("%s：客户端 pick_patch 挑得到" % frm,
                   isinstance(picked, dict) and picked.get("url") == it.get("url"),
                   repr(picked))

        check("%s：地址就是站点上那个文件" % frm, it.get("url"),
              "%s/patch/%s" % (base, fn))
        check_true("  ★ 文件真的在 site/patch/ 里", fn in on_disk, fn)
        p = os.path.join(pdir, fn)
        if os.path.isfile(p):
            check("%s：sha256 与清单一致" % frm, U.sha256_file(p),
                  (it.get("sha256") or "").lower())
            check("%s：大小与清单一致" % frm, os.path.getsize(p), it.get("size"))
        if isinstance(full, int) and full > 0 and isinstance(it.get("size"), int):
            # 客户端 pick_patch 会把「超过完整包一半」的补丁筛掉 ——
            # 发一个会被筛掉的补丁 = 白占空间、白花时间。
            check_true("%s：补丁 ≤ 完整包一半（否则客户端会拒收）" % frm,
                       it["size"] * 2 <= full,
                       "%d / %d" % (it["size"], full))

    check("from 不重复（重复的话后面那条永远挑不到）",
          len(seen_from), len(set(seen_from)))

    # 反向：站点上有的，清单里也得有。多余的文件没人会下（还占配额）。
    listed = set("%s-to-%s.hpatch" % (f, ver) for f in seen_from)
    extra = [f for f in on_disk if f not in listed]
    check_true("★ 站点上没有清单外的孤儿补丁", not extra, "、".join(extra) or "无")

    # 客户端挑不到别的版本（挑到就说明 from 的匹配规则太松）。
    check("挑一个不存在的旧版本 → 没有", U.pick_patch(data, "9.9.9"), None)


# ================================================================ 5. 真工具端到端

def _version_hashes():
    """{版本: sha256}，来自站点产物 `site/versions.json`。读不到返回 None。"""
    p = os.path.join(_site_dir(), "versions.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        return None
    out = {}
    for it in (data.get("versions") or []):
        if isinstance(it, dict) and it.get("version"):
            out[it["version"]] = (it.get("sha256") or "").lower()
    return out


def test_end_to_end():
    """真工具端到端：旧包 + 真补丁 → 新版，sha256 必须等于那一版发出去的哈希。

    这一节是**唯一**能证明「发出去的补丁真的装得上」的检查 ——
    前面的假工具只能证明逻辑分支对，证明不了补丁文件本身没问题。

    还顺带钉住一件事：`dl_archive/` 里的旧包**必须就是当时发出去的那一份**
    （与 `versions.json` 的 sha256 一致）。不然测的是「某个旧文件 + 补丁」，
    结论推不到用户身上。
    """
    print("\n[5] 真工具端到端：旧包 + 真补丁 → 新版（sha256 对得上）")

    site = _site_dir()
    pdir = os.path.join(site, "patch")
    arch = os.path.join(os.path.dirname(HERE), "dl_archive")
    if not os.path.isdir(pdir):
        print("  [跳过] %s 不存在 —— 跑一次 python make_site.py 才有" % pdir)
        return
    if not os.path.isdir(arch):
        print("  [跳过] %s 不存在（历史包归档）" % arch)
        return
    hashes = _version_hashes()
    if not hashes:
        print("  [跳过] site/versions.json 不在或读不出")
        return
    if not U.hpatchz_path():
        print("  [跳过] 找不到 hpatchz.exe，跑不了真还原")
        return

    names = sorted(f for f in os.listdir(pdir) if f.endswith(".hpatch"))
    if not names:
        print("  [跳过] site/patch/ 里一个补丁都没有")
        return

    tmp = tempfile.mkdtemp(prefix="patch_e2e_")
    tested, skipped = 0, 0
    try:
        for fn in names:
            frm, to = parse_patch_name(fn)
            if not frm or not to:
                check("%s：文件名能解析出 X-to-Y" % fn, (frm, to), ("X", "Y"))
                continue
            old = os.path.join(arch, "campus-login-%s.exe" % frm)
            if not os.path.isfile(old):
                print("  [跳过] %s：dl_archive 里没留 %s 的包" % (fn, frm))
                skipped += 1
                continue
            want = hashes.get(to)
            if not want:
                check("%s：versions.json 里有 %s 的 sha256" % (fn, to), False, True)
                continue

            # 先确认手里的旧包就是当时发出去的那一份
            if U.sha256_file(old) != hashes.get(frm):
                check("%s：dl_archive 里的 %s 与 versions.json 一致" % (fn, frm),
                      False, True)
                continue

            out = os.path.join(tmp, "campus-login-%s.exe" % to)
            ok, why = U.apply_patch(old, os.path.join(pdir, fn), out)
            if not ok:
                check("%s：还原成功" % fn, "失败：%s" % why, "成功")
                continue
            got = U.sha256_file(out)
            check_true("%s：还原出的 sha256 == %s 的 sha256" % (fn, to),
                       got == want, "%s…" % got[:12])
            tested += 1
            try:
                os.remove(out)
            except OSError:
                pass

        # 防止「一条都没跑」也算通过 —— 那是最容易骗人的绿灯。
        check_true("★ 至少真验了一对补丁", tested > 0,
                   "实测 %d 对，跳过 %d 对" % (tested, skipped))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 78)
    print("增量更新（0.5.0）测试")
    print("=" * 78)

    # 沙箱 + 收尾核对。本测试其实只写临时目录，但项目惯例如此：
    # 将来有人往这里加一行写盘代码时，那一道闸会立刻响。
    datasafe.sandbox(tag="patch_test")

    test_tools()
    test_apply_patch()
    test_try_patch_update()
    test_download_update()
    test_artifact()
    test_end_to_end()

    print()
    print("=" * 78)
    try:
        print(datasafe.assert_untouched())
    except Exception as e:                                    # noqa: BLE001
        print("!! %s" % e)
        FAILS.append("真实数据未改动")

    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
