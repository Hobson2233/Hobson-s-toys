# -*- coding: utf-8 -*-
"""确认新加的使用说明文字真的在 exe 里（并带阳性/阴性对照）。

用法：
    python help_check.py [exe]

为什么不能直接对 exe grep：PyInstaller 的 CArchive 是压缩的，明文搜索永远搜不到
（连 "CampusLogin" 都是 0 命中）。必须解开归档再搜。
"""
import os
import sys

import paths

EXE = sys.argv[1] if len(sys.argv) > 1 else \
    paths.default_exe()

# 守护分钟数**不写字面量**，从 GUARD_SECONDS 反算 —— 改常量忘了改文案时，
# 这一条会立刻报 BAD。这正是「说明必须和实现一致」的落地方式。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_HAVE_SRC = True
try:
    import campus_login as _C
    _GUARD_MIN = _C.GUARD_SECONDS // 60
except Exception:                                    # 源码不在旁边就别拦
    _HAVE_SRC = False
    _GUARD_MIN = 30

_GUARD_NOTE = ("守护时长（应与 GUARD_SECONDS=%d 秒一致）" % _C.GUARD_SECONDS
               if _HAVE_SRC else "守护时长（没读到源码，按默认 30 分钟查）")

CASES = [
    ("【密码框怎么用】", True, "新加的说明小节"),
    ("显示成一串小星号", True, "密码框说明正文"),
    ("刚敲进去的那一个字符会露出来一下", True, "密码框说明正文"),
    ("从别处复制密码粘进来，用 Ctrl+V", True, "密码框粘贴说明"),
    ("【三个按钮】", True, "原有小节仍在"),
    # 「检查更新」这一节（2026-09-18 加）。这两句是这一节的关键承诺：
    # 失败时必须说「失败」，不能说成「已是最新」—— 用户最容易误解的地方。
    ("【检查更新】", True, "检查更新的说明小节"),
    ("不会含糊地显示「已是最新版本」", True, "更新说明里的三态承诺"),
    # 更新临时文件不再落 exe 同目录（0.3.3，用户反馈「桌面上莫名多了个 .old」）。
    # 这是给用户的承诺 —— 文案被改回去，就等于把那个坑又挖开了。
    ("不会写到你放 exe 的地方", True, "0.3.3 更新临时文件不再落 exe 同目录"),
    # 旧版本备份会「过一会儿再试几次」自动清掉（0.3.4）。用户 2026-09-20 反馈的
    # 「更新完 .old 还在」就是因为清理只做一次、失败就算了 —— 这句是给用户的承诺，
    # 说明他不用自己去桌面上找那个文件。
    ("过一会儿会自己再试几次", True, "0.3.4 残留清理失败会重试"),
    # 网络状态那四档（2026-09-19 改）。原来是「已连接/未连接」两行，而
    # 「已连接校园网」在宿舍 Wi-Fi 上也会出现 —— 现在拆成四档，文案必须跟着变。
    # 这两句是新增档位独有的，只查它们就够区分新旧说明。
    ("已连校园网，未认证", True, "在校园网但未认证那一档"),
    ("现在连的不是校园网", True, "不在校园网的说明"),
    # 守护（0.3.0 加）。
    # ⚠️ 分钟数必须写成**字面量**才查得到：help_check 是在打包后的归档里
    #    按字节搜明文，运行时用 replace/%-格式化拼出来的句子，归档里只存模板，
    #    搜渲染结果必然落空（第一版就是这么报了一条假失败的）。
    #    所以文案里写死 30，这里再拿 GUARD_SECONDS 反算比对来防漂移。
    ("联网之后它还会继续守 %d 分钟" % _GUARD_MIN, True, _GUARD_NOTE),
    ("这段时间里你要是主动点了「退出当前账号」，它不会把你登回去",
     True, "守护不跟用户对着干的承诺"),
    ("升级到新版本后如果提示「旧设置」", True, "自启参数升级的说明"),
    # 自启改用计划任务（0.3.2）。用户会在「任务计划程序」里看到这个任务，
    # 而任务管理器→启动应用 里**看不到**了 —— 说明里必须讲清楚，
    # 否则他以为自启丢了。这条就是钉住那句话的。
    ("一个叫 CampusLogin 的任务", True, "0.3.2 自启改用计划任务后的说明"),
    # 命令行开关清单（0.3.1 加）。它是**另一个常量 SWITCH_HELP**，由 --help
    # 接在 HELP_TEXT 后面打印 —— 分开放是为了不动 HELP_TEXT 的字节。
    # 分开的代价是「没人看着它」，所以这里补上核对：不然哪天误删整段也没人知道。
    ("【命令行开关】", True, "0.3.1 补的命令行开关小节"),
    ("--switch <学号>", True, "开关清单里的一条"),
    ("不带任何开关（直接双击）就是正常开界面", True, "开关清单的收尾说明"),
    ("这段文字不存在ZZZ", False, "阴性对照：不该命中"),
]

# PYZ 层阳性对照：这几句只在标准库模块里，命中了才说明 PYZ 真的被解开了。
# 没有它的话，ZlibArchiveReader 一旦改返回类型（它返回 code 对象而不是 bytes），
# 异常被吞掉，检查会静默退化成「只查了顶层」还照样报「通过」。
PYZ_CONTROLS = ["unknown encoding: ", "invalid syntax"]


def scan(exe):
    """把归档里所有条目（含 PYZ 内模块）拼成一大块字节，供明文搜索。

    复用 leak_check.iter_archive_blobs —— 顶层 + PYZ 两层的解压逻辑只留一份，
    否则两个检查脚本迟早会因为「一个查了 PYZ、一个没查」而对不上。
    """
    import leak_check
    blob = b""
    top = pyz = 0
    for name, data in leak_check.iter_archive_blobs(exe):
        blob += data
        if ":" in name:
            pyz += 1
        else:
            top += 1
    return top, pyz, blob


def main():
    print("exe: %s  %d 字节" % (EXE, os.path.getsize(EXE)))
    top, pyz, blob = scan(EXE)
    print("顶层条目: %d  PYZ 内模块: %d  扫描字节数: %d" % (top, pyz, len(blob)))
    bad = 0
    if pyz == 0:
        bad += 1
        print("  [BAD] PYZ 层一条都没解出来 —— 检查退化成只查顶层了")
    for s, want, note in CASES:
        got = s.encode("utf-8") in blob
        if got != want:
            bad += 1
        print("  [%s] %-30s 命中=%-5s (期望 %-5s) %s"
              % ("OK " if got == want else "BAD", s, got, want, note))
    for s in PYZ_CONTROLS:
        got = s.encode("utf-8") in blob
        if not got:
            bad += 1
        print("  [%s] %-30s 命中=%-5s (期望 True ) PYZ 层阳性对照"
              % ("OK " if got else "BAD", s, got))
    print("结论:", "通过" if bad == 0 else "有 %d 项不符" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
