# -*- coding: utf-8 -*-
"""把 startup_dir 劫持到临时目录，完整测试自启的建/删/识别逻辑"""
import os
import shutil
import tempfile

import paths
import campus_login as C

EXE = paths.default_exe()
TMP = tempfile.mkdtemp(prefix="fake-startup-")
C.startup_dir = lambda: TMP
C.is_frozen = lambda: True
C.app_path = lambda: EXE

from pylnk3 import parse  # noqa: E402


def read(p):
    with open(p, "rb") as f:
        l = parse(f)
    return l.path, l.arguments, l.icon


def names():
    return sorted(os.path.basename(p) for p in C.autostart_links())


ok_all = True


def check(name, cond, extra=""):
    global ok_all
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -> " + str(extra)) if (extra != "" and not cond) else ""))
    if not cond:
        ok_all = False


print("[1] 空目录")
check("is_autostart_on = False", C.is_autostart_on() is False)

print("[2] 先放两个快捷方式：一个指向老 .bat（老版本留下的），一个无关")
C.make_lnk(os.path.join(TMP, "campus-login.bat - 快捷方式.lnk"),
           r"C:\tools\campus-login.bat", work_dir=r"C:\tools")
C.make_lnk(os.path.join(TMP, "无关程序.lnk"), r"C:\Windows\System32\notepad.exe")
check("识别出 1 个（老 .bat 那个）", len(C.autostart_links()) == 1, names())
check("is_autostart_on = True", C.is_autostart_on() is True)

print("[3] 开启自启：应替换成指向 exe 的")
ok, why = C.set_autostart(True)
check("返回 ok", ok, why)
check("只剩 1 个自启项", len(C.autostart_links()) == 1, names())
check("无关程序没被删", os.path.isfile(os.path.join(TMP, "无关程序.lnk")))
new = os.path.join(TMP, C.AUTOSTART_LNK_NAME)
check("新快捷方式已生成", os.path.isfile(new))
tgt, args, icon = read(new)
# pylnk3 的解析器会把最后一段文件名丢掉，所以目标可能是 exe 本身或其所在目录
check("目标指向 exe", tgt in (EXE, os.path.dirname(EXE)), tgt)
check("参数 = --auto", args == "--auto", args)
check("图标 = exe 自身", (icon or "").replace("/", "\\").lower() == EXE.lower(), icon)

print("[4] 重复开启：幂等")
ok, why = C.set_autostart(True)
check("返回 ok", ok, why)
check("仍是 1 个", len(C.autostart_links()) == 1, names())

print("[5] 用户把快捷方式改成别的名字，也要认得出")
renamed = os.path.join(TMP, "我自己改的名字.lnk")
os.replace(new, renamed)
check("改名后仍识别", len(C.autostart_links()) == 1, names())
os.replace(renamed, new)

print("[6] 关闭：清干净，但不误伤")
ok, why = C.set_autostart(False)
check("返回 ok", ok, why)
check("已判定为关闭", C.is_autostart_on() is False)
check("无关程序还在", os.path.isfile(os.path.join(TMP, "无关程序.lnk")))

print("[7] 重复关闭：幂等")
ok, why = C.set_autostart(False)
check("返回 ok", ok, why)

print("[8] exe 被挪走之后，要能认出「自启已失效」")
# 背景：快捷方式里存的是**绝对路径**，挪动 exe 它就指不到了；而 autostart_links()
# 的判定里有「文件名就是我们建的那个」这条，所以 is_autostart_on() 照样返回 True
# —— 用户看到勾打着，以为没问题，实际开机什么都不会发生。2026-09-17 真踩过。
ok, why = C.set_autostart(True)
check("开启后 stale = False", C.autostart_stale() is False, why)
link = os.path.join(TMP, C.AUTOSTART_LNK_NAME)
check("链接确实指向当前 exe", C._lnk_points_to_me(link) is True)

real_app_path = C.app_path
C.app_path = lambda: os.path.join(TMP, "别处", "校园网自动登录.exe")
check("exe 换位置后 -> 不再指向我", C._lnk_points_to_me(link) is False)
check("exe 换位置后 -> stale = True（该报警）", C.autostart_stale() is True)

# 「取消再勾一次」应该把它修好
ok, why = C.set_autostart(True)
check("重新勾选后 stale = False", C.autostart_stale() is False, why)
C.app_path = real_app_path

# 源码运行不该报警（否则 ui_shot.py 的截图里会多出一条假警告）
C.is_frozen = lambda: False
check("源码运行时 stale = False", C.autostart_stale() is False)
C.is_frozen = lambda: True

# 读不出来 = 不知道，不能妄断成失效
check("不存在的链接 -> None（不妄断）",
      C._lnk_points_to_me(os.path.join(TMP, "根本没有这个.lnk")) is None)
C.set_autostart(False)

print("\n结果:", "全部通过" if ok_all else "有失败")
if ok_all:
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print("临时目录保留待查:", TMP)
