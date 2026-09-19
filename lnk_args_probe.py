# -*- coding: utf-8 -*-
"""先探一下真实的 .lnk 能不能被 _lnk_arguments 正确读出参数。

这一步是**先验证读法本身**，再写正式测试 —— 否则正式测试里两个变量
（读法对不对 + 我的期望值对不对）搅在一起，失败了分不清是谁的错。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C

d = C.startup_dir()
print("启动文件夹:", d)
print("是否存在:", os.path.isdir(d))
links = C.autostart_links()
print("自启链接:", links)
for p in links:
    raw = open(p, "rb").read()
    print("-" * 60)
    print("文件:", os.path.basename(p), len(raw), "字节")
    print("  _lnk_target()   :", repr(C._lnk_target(p)))
    print("  _lnk_points_to_me:", C._lnk_points_to_me(p))
    print("  _lnk_arguments() :", repr(C._lnk_arguments(p)))
print("-" * 60)
print("autostart_args()   :", repr(C.autostart_args()))
print("autostart_stale()  :", C.autostart_stale())
print("autostart_outdated():", C.autostart_outdated())
print("AUTOSTART_ARGS     :", repr(C.AUTOSTART_ARGS))
print("-" * 60)
print("guard_running():", C.guard_running())
print("GUARD_FLAG:", C.GUARD_FLAG, "存在:", os.path.isfile(C.GUARD_FLAG))
print("logged_out_recently():", C.logged_out_recently())
