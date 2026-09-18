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
