# -*- coding: utf-8 -*-
"""远程附加下载源（0.4.5）的**确定性**测试。

这个机制的出发点：0.4.4 的下载源写死在发布清单里，想加一个源就得发新版 ——
而「想加源」最常发生在「某个源挂了、用户正下不动」的时候，那时候恰恰发不了新版。
所以 0.4.5 把附加源抽成站点上一个独立的 `mirrors.json`（no-store），
客户端每次下载前拉一次。

它同时引入了一个**新的攻击面**：一个从网上取回来的文件，可以影响
「程序去哪个地址下载可执行文件」。所以这里钉住的不是「能不能用」，而是**边界**：

  1. 🔴 **主源永远是清单里的第一项，附加源只能往后追加。**
     远程文件不许改变「首选从哪儿下」—— 否则「站点上那个文件被改坏」
     就等于「用户被引到一个陌生地址」。
  2. 🔴 **坏数据不许带崩更新流程。** 不是列表、是数字、是乱码、项不是字典、
     URL 是 file:// —— 一律当作「没有这条附加源」，不抛异常。
  3. **拉不到附加源表 = 照常更新。** 它是锦上添花，不是必需品。

真正的安全网是清单里的 sha256（主站给出、强制校验）：被污染的附加源表
最坏只能让下载失败，装不上坏东西。这条由 updater.py 的 _selftest 钉着。

用法：
    python mirrors_test.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import updater as U            # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-54s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print("  [%s] %-54s %s" % ("OK" if cond else "失败", name, detail))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 假网络
#
# 只替换 updater._get 这一层：fetch_mirrors 的下游全是纯逻辑，喂确定字节就够。
# ⚠️ 必须还原 —— 后面的小节（尤其产物核对那节）会用到真的 _get。

class FakeGet(object):
    def __init__(self, ok=True, body=b"", reason="假失败"):
        self.ok = ok
        self.body = body
        self.reason = reason
        self.calls = []

    def __enter__(self):
        self._orig = U._get
        U._get = self._fake
        return self

    def _fake(self, url, timeout=None):
        self.calls.append(url)
        if self.ok:
            return True, self.body
        return False, self.reason

    def __exit__(self, *a):
        U._get = self._orig
        return False


def body_of(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ================================================================ 1. 拉取健壮性

def test_fetch():
    print("\n[1] fetch_mirrors：拉不到 / 格式不对 → 一律空列表，不抛异常")

    with FakeGet(ok=False) as f:
        check("网络失败 → 空列表", U.fetch_mirrors(), [])
        check_true("确实发起了请求（不是没跑）", len(f.calls) == 1, str(f.calls))

    cases = [
        ("不是 JSON", b"<html>404</html>"),
        ("JSON 但不是对象", b"[1,2,3]"),
        ("顶层是数字", b"123"),
        ("缺 mirrors 字段", body_of({"note": "x"})),
        ("mirrors 不是列表", body_of({"mirrors": "x"})),
        ("mirrors 是 null", body_of({"mirrors": None})),
        ("空字节", b""),
        ("非法 UTF-8", b"\xff\xfe\x00"),
    ]
    for label, payload in cases:
        with FakeGet(ok=True, body=payload):
            check("%s → 空列表" % label, U.fetch_mirrors(), [])

    # 非字典项被丢掉，字典项保留 —— 一条坏的不能让整张表作废。
    with FakeGet(ok=True, body=body_of({"mirrors": [1, None, "x", {"url": "https://m/a"}]})):
        got = U.fetch_mirrors()
    check("混合项：只留字典", got, [{"url": "https://m/a"}])

    # 正常路径。
    with FakeGet(ok=True, body=body_of({"mirrors": [{"url": "https://m/a"}]})):
        check("正常表能读出来", U.fetch_mirrors(), [{"url": "https://m/a"}])

    # 确认请求打的是站点上那个地址（改了域名这里会红）。
    with FakeGet(ok=False) as f:
        U.fetch_mirrors()
    check_true("默认拉的是站点上的 mirrors.json",
               f.calls[0].endswith("/mirrors.json") and
               f.calls[0].startswith(U.BASE_URL_HINT),
               f.calls[0])


# ================================================================ 2. 合并边界

def test_merge():
    print("\n[2] 合并：主源不许被顶掉、坏数据不许带崩")

    PRIMARY = "https://hobson2233.dpdns.org/dl/campus-login-1.2.3.exe"
    GH = "https://github.com/x/y/releases/download/v1.2.3/z.exe"

    # 🔴 本节最重要的一条。
    check("★ 附加源只能追加，主源仍在第一位",
          U.merge_sources([PRIMARY, GH], [{"url": "https://m/a"}], "1.2.3"),
          [PRIMARY, GH, "https://m/a"])

    # 附加源里**故意重复**主源：不能变成两项，也不能把主源挪走。
    check("★ 附加源与主源重复 → 去重，主源仍在第一位",
          U.merge_sources([PRIMARY], [{"url": PRIMARY}, {"url": "https://m/a"}], "1.2.3"),
          [PRIMARY, "https://m/a"])

    # 附加源想「插队」是做不到的 —— 它只能出现在已有项之后。
    check("★ 附加源无法插到主源前面",
          U.merge_sources([PRIMARY], [{"url": "https://evil/first"}], "1.2.3")[0],
          PRIMARY)

    check("没有附加源 → 清单原样返回",
          U.merge_sources([PRIMARY, GH], [], "1.2.3"), [PRIMARY, GH])
    check("清单为空 + 有附加源 → 附加源成为唯一项",
          U.merge_sources([], [{"url": "https://m/a"}], "1.2.3"), ["https://m/a"])

    # 坏数据不许抛异常（这几条是 2026-10-08 自检抓出来的真 bug：
    # 少了 mirror_urls 里那句 isinstance 检查，一个数字就能抛 TypeError）。
    for label, bad in [("数字", 123), ("字符串", "垃圾"), ("None", None), ("字典", {"a": 1})]:
        try:
            got = U.merge_sources([PRIMARY], bad, "1.2.3")
            check("mirrors=%s → 不影响主源" % label, got, [PRIMARY])
        except Exception as e:
            check("mirrors=%s → 不影响主源" % label, "抛了 %s" % type(e).__name__, [PRIMARY])

    for label, bad in [("数字", 123), ("None", None), ("字典", {"a": 1})]:
        try:
            got = U.candidate_urls({"url": PRIMARY, "version": "1.2.3"}, bad)
            check("candidate_urls(mirrors=%s) → 只有主源" % label, got, [PRIMARY])
        except Exception as e:
            check("candidate_urls(mirrors=%s) → 只有主源" % label,
                  "抛了 %s" % type(e).__name__, [PRIMARY])

    # 不传 mirrors 时必须和 0.4.4 行为完全一致（老调用方不受影响）。
    info = {"url": PRIMARY, "urls": [PRIMARY, GH], "version": "1.2.3"}
    check("★ 不传 mirrors → 与 0.4.4 一致", U.candidate_urls(info), [PRIMARY, GH])
    check("传 mirrors → 追加在后面",
          U.candidate_urls(info, [{"url": "https://m/a"}]), [PRIMARY, GH, "https://m/a"])

    print("\n[2b] 单条记录的整理规则")

    check("{version} 被替换",
          U.mirror_urls([{"url": "https://m/v{version}/x.exe"}], "9.9.9"),
          ["https://m/v9.9.9/x.exe"])
    check("没有 {version} 就原样用",
          U.mirror_urls([{"url": "https://m/x.exe"}], "9.9.9"),
          ["https://m/x.exe"])
    check("★ enabled=false 被跳过",
          U.mirror_urls([{"url": "https://m/x", "enabled": False}], "1.0.0"), [])
    check("enabled=true 保留",
          U.mirror_urls([{"url": "https://m/x", "enabled": True}], "1.0.0"),
          ["https://m/x"])
    check("enabled 缺失 → 默认启用",
          U.mirror_urls([{"url": "https://m/x"}], "1.0.0"), ["https://m/x"])
    check("缺 url → 跳过", U.mirror_urls([{"name": "x"}], "1.0.0"), [])
    check("url 是空串 → 跳过", U.mirror_urls([{"url": "   "}], "1.0.0"), [])
    check("url 不是字符串 → 跳过", U.mirror_urls([{"url": 123}], "1.0.0"), [])
    # 表也是从网上取回来的，不该让它把我们引到本地协议上去。
    check("★ 只留 http(s)",
          U.mirror_urls([{"url": "file:///c:/evil.exe"},
                         {"url": "ftp://m/x"},
                         {"url": "https://ok/x"}], "1.0.0"),
          ["https://ok/x"])
    check("去重保序",
          U.mirror_urls([{"url": "https://m/x"}, {"url": "https://m/x"},
                         {"url": "https://n/y"}], "1.0.0"),
          ["https://m/x", "https://n/y"])


# ================================================================ 3. 产物核对

def test_artifact():
    """核对**构建产物**能被客户端读懂。

    这一节是跨模块的：`make_site.py`（工作区根目录）写出来的文件，
    要能被 `updater.py` 读懂。两边字段名对不上时，客户端会静默地
    「一条附加源都没有」—— 不报错，只是这个机制白做了。

    ⚠️ 找不到产物时**明说跳过**，不静默通过：site/ 是构建产物，
        单独克隆 repo/ 的人不会有它（那时这一节无从谈起）。
    """
    print("\n[3] 构建产物核对（site/mirrors.json）")

    path = os.path.join(os.path.dirname(HERE), "site", "mirrors.json")
    if not os.path.exists(path):
        print("  [跳过] %s 不存在 —— 跑一次 python make_site.py 才有" % path)
        return

    raw = open(path, encoding="utf-8").read()
    try:
        data = json.loads(raw)
    except ValueError as e:
        check("产物是合法 JSON", "解析失败：%s" % e, "合法 JSON")
        return
    check("产物是合法 JSON", True, True)

    check("顶层是对象", isinstance(data, dict), True)
    check_true("有 mirrors 字段且是列表",
               isinstance(data.get("mirrors"), list), repr(type(data.get("mirrors"))))

    entries = data.get("mirrors") or []
    # 客户端读得懂吗 —— 真跑一遍 updater 的解析，不是看字段名猜。
    with FakeGet(ok=True, body=raw.encode("utf-8")):
        parsed = U.fetch_mirrors()
    check("客户端能解析出 %d 条记录" % len(entries), len(parsed), len(entries))

    # 🔴 最关键的一条：这份产物**不许**能把主源顶掉。
    PRIMARY = "https://hobson2233.dpdns.org/dl/campus-login-9.9.9.exe"
    merged = U.merge_sources([PRIMARY], parsed, "9.9.9")
    check_true("★ 用真实产物合并后，主源仍在第一位",
               bool(merged) and merged[0] == PRIMARY, repr(merged[:2]))

    # 启用的条数：0 是**正常状态**（当前只有一条关掉的格式示例）。
    live = U.mirror_urls(parsed, "9.9.9")
    print("  [信息] 产物里启用的附加源 %d 条：%s"
          % (len(live), "、".join(live) if live else "（无，符合当前预期）"))
    # 但**格式示例必须还在** —— 它是「格式没写错」的活证据。删了要说明为什么。
    #
    # ⚠️ detail 是**无条件打印**的（见 check_true），所以这里只能写中性描述。
    #    原来写的是「没有示例记录 —— 是特意删掉的吗？」—— 那是失败时该说的话，
    #    检查通过时也照样打出来，读起来像坏了（实测把人骗去查了一遍产物）。
    check_true("产物里保留着格式示例（enabled=false 的那条）",
               any(isinstance(e, dict) and e.get("enabled") is False for e in entries),
               "%d 条记录，enabled 取值 %s"
               % (len(entries),
                  [e.get("enabled") for e in entries if isinstance(e, dict)]))


def main():
    print("=" * 78)
    print("远程附加下载源（0.4.5）测试")
    print("=" * 78)
    test_fetch()
    test_merge()
    test_artifact()
    print()
    print("=" * 78)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
