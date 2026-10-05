# -*- coding: utf-8 -*-
"""站点域名「多处不漂移」测试。

为什么要单独一个测试（2026-10-05）：

    发布站域名 `hobson2233.dpdns.org` 被写在了 **7 个文件 / 9 处** ——
    程序内置的使用说明里写给用户看、更新功能拿它拼清单地址、
    站点构建拿它拼下载链接、发布脚本拿它做发布后校验、wrangler 配置里
    还有一句注释提醒「改 Worker 名会让这个域名失联」。

    这些地方**必须字面一致**，但没有任何机制保证。以前只在文档里写了一句
    「三处必须同源」—— 而实际是 9 处，靠人记。

    ⚠️ 为什么不干脆合并成一处：
       `repo/` 是要被**单独打包**的（PyInstaller onefile），
       `updater.py` 不能去 import 工作区根目录的东西。
       所以这里的选择是：**保留各自的副本，用测试守住一致性** ——
       比强行合并便宜得多，也不会动打包结构。

金标准 = 「所有提到它的地方说的是同一个域名」，不是某个理想值。
真要换域名时，**每一处都改**，这个测试自然会绿；
只改一处会红 —— 那正是它存在的意义。

用法：
    python domain_drift_test.py             # 检查
    python domain_drift_test.py --selftest  # 阳性对照：证明它真的会拦人
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 工作区根目录（repo/ 的上一级）

# 只认这个后缀 —— 不去猜「哪些字符串像域名」，那会误伤 github.com 之类。
# 换域名时连这个常量一起改（测试会因为「一处都找不到」而报错，不会静默通过）。
SUFFIX = "dpdns.org"
HOST_RE = re.compile(r"[a-z0-9][a-z0-9.\-]*\." + SUFFIX.replace(".", r"\."), re.I)

# (相对路径, 这份文件里为什么会有这个域名, 在不在 repo 检出里)
#
# 🔴 「在不在 repo 里」这一列是有意义的：`repo/` 会被单独 clone 出去，
#    那时工作区根目录的文件（make_site.py / deploy/…）根本不存在。
#    repo 内的必须存在（缺了就是漂移），repo 外的缺了只跳过。
SOURCES = [
    ("repo/updater.py",        "更新功能拼清单地址（MANIFEST_URL）",      True),
    ("repo/campus_login.py",   "内置「使用说明」正文里写给用户看的",      True),
    ("repo/help_check.py",     "打包门槛核对 exe 里那份说明",             True),
    ("make_site.py",           "站点根地址 + 页面文案",                   False),
    ("make_manifest.py",       "清单里的下载地址",                        False),
    ("deploy/deploy.py",       "发布后校验用的地址",                      False),
    ("wrangler.toml",          "Worker 配置注释里提醒的那个域名",         False),
]


def hosts_in(text):
    """返回这段文本里出现的全部站点域名（小写、去重、已排序）。"""
    return sorted({m.group(0).lower() for m in HOST_RE.finditer(text)})


def scan(overrides=None):
    """扫全部来源。overrides: {相对路径: 文本}，用来在自检里注入假内容。

    返回 (每个来源的结果列表, 问题列表)。
    """
    overrides = overrides or {}
    rows, problems = [], []
    for rel, why, must in SOURCES:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if rel in overrides:
            text = overrides[rel]
        elif not os.path.isfile(path):
            if must:
                problems.append("缺文件 %s（%s）—— repo 检出里必须有它" % (rel, why))
            rows.append((rel, why, None, "跳过（不在这个检出里）"))
            continue
        else:
            text = open(path, encoding="utf-8", errors="replace").read()

        found = hosts_in(text)
        rows.append((rel, why, found, ""))
        if not found:
            problems.append("%s 里一处都没提到 %s（%s）—— 是删了还是改域名漏了？"
                            % (rel, SUFFIX, why))
    return rows, problems


def report(rows, problems):
    print("=== 各处提到的站点域名 ===")
    for rel, why, found, note in rows:
        if found is None:
            shown = note
        elif not found:
            shown = "**一处都没有**"
        else:
            shown = "、".join(found)
        print("  %-24s %s" % (rel, shown))
        if found:
            print("  %-24s └ %s" % ("", why))

    allhosts = sorted({h for _r, _w, f, _n in rows if f for h in f})
    print()
    print("=== 汇总 ===")
    print("  扫到的域名：%s" % ("、".join(allhosts) if allhosts else "（一个都没有）"))
    print("  出现过的文件数：%d / %d"
          % (sum(1 for _r, _w, f, _n in rows if f), len(SOURCES)))

    if len(allhosts) > 1:
        problems.append("域名**不止一个**：%s —— 各处已经漂移了，改成一致再发版"
                        % "、".join(allhosts))
    if not allhosts:
        problems.append("所有来源里都没找到 %s —— 要么域名换了（连本测试的 "
                        "SUFFIX 一起改），要么提取规则失效了" % SUFFIX)
    return problems


def run():
    """当门槛跑：**不读 sys.argv**，返回 0/1。

    `build.py` 直接 import 这个函数调 —— 用 main() 的话会把 build.py 自己的
    命令行参数（比如 --selftest）误当成给自己的，行为随调用方漂移。
    """
    rows, problems = scan()
    report(rows, problems)
    print()
    if problems:
        print("🔴 失败 %d 项：" % len(problems))
        for p in problems:
            print("  · %s" % p)
        return 1
    # 注意措辞：这里数的是「文件数」，不是「出现次数」—— 同一个文件里写几遍
    # 都会被去重成一个域名，说成「N 处」会误导。
    print("✅ 全部一致 —— %d 个文件都指向 %s"
          % (sum(1 for _r, _w, f, _n in rows if f), SUFFIX))
    return 0


def main():
    print("=== 站点域名一致性（%s）===" % SUFFIX)
    print()
    if "--selftest" in sys.argv:
        return selftest_run()
    return run()


# ---------------------------------------------------------------- 阳性对照
#
# 项目惯例（见 wiring_gate_test.py / ui_contrast_test.py）：
# **门槛本身也要被验** —— 一个永远返回 0 的测试比没有测试更糟，
# 因为它会让人以为「这里没问题」。下面注入两种坏情况，断言它确实会报错。

def selftest_run():
    fails = []

    def expect(name, cond):
        print("  [%s] %s" % ("OK" if cond else "失败", name))
        if not cond:
            fails.append(name)

    print("=== 阳性对照 1：把某个来源改成别的域名，必须被拦下 ===")
    rows, problems = scan(overrides={
        "make_site.py": 'BASE_URL = "https://evil.example.%s"\n' % SUFFIX,
    })
    problems = report(rows, problems)
    expect("注入第二个域名后报错", bool(problems))
    expect("报的是「不止一个域名」",
           any("不止一个" in p for p in problems))
    print()

    print("=== 阳性对照 2：把某个来源里的域名整个删掉，必须被拦下 ===")
    rows, problems = scan(overrides={"deploy/deploy.py": "# 这里什么都没写\n"})
    problems = report(rows, problems)
    expect("某处一处都不提时被拦下", bool(problems))
    print()

    print("=== 阴性对照：原样扫描必须通过 ===")
    rows, problems = scan()
    problems = report(rows, problems)
    expect("原样扫描零问题", not problems)
    print()

    if fails:
        print("🔴 阳性/阴性对照失败 %d 项：%s" % (len(fails), ", ".join(fails)))
        return 1
    print("✅ 对照全过 —— 这个门槛确实会拦人，而且不误报")
    return 0


if __name__ == "__main__":
    sys.exit(main())
