# -*- coding: utf-8 -*-
"""把 startup_dir 劫持到临时目录，完整测试自启的建/删/识别逻辑"""
import os
import shutil
import sys
import tempfile

import paths
import campus_login as C

EXE = paths.default_exe()
TMP = tempfile.mkdtemp(prefix="fake-startup-")
C.startup_dir = lambda: TMP
C.is_frozen = lambda: True
C.app_path = lambda: EXE

# ⚠️ 计划任务那套东西在测试里必须**挡住**：0.3.2 起 set_autostart() 会优先去建
#    一个真的计划任务，跑一次这个脚本就会在用户机器上留下一个指向真 exe 的
#    CampusLogin 任务（而且它会在下次登录时真跑起来）。
#    task_state() 是所有 task_* 判断的唯一入口，把它按成「没这个任务」就够了；
#    task_create 再按成「建不了」，于是下面的用例走的正是「退回启动文件夹」那条路。
#    计划任务那条路的逻辑在 [10] 里用假实现单独验。
C.task_state = lambda: {"exists": False, "command": "", "arguments": "", "logon": False}
C.task_create = lambda: (False, "测试里不建计划任务")
C.task_delete = lambda: (True, "")

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
# ⚠️ 别写字面量：参数的唯一来源是 C.AUTOSTART_ARGS（--auto --guard）。
#    以前这里写死 "--auto"，加了守护之后就会变成一条假的失败。
check("参数 = %s" % C.AUTOSTART_ARGS, args == C.AUTOSTART_ARGS, args)
check("图标 = exe 自身", (icon or "").replace("/", "\\").lower() == EXE.lower(), icon)
# 刚开完的自启**不该**被判成「参数陈旧」，否则界面上会挂着一条假警告
check("刚开启 -> 参数不陈旧", C.autostart_outdated() is False)

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

print("[9] 0.2.0 装的旧参数（--auto）要能被认成「陈旧」")
# 背景：0.3.0 把自启参数从 --auto 改成 --auto --guard。老用户的 .lnk 指向的
# exe 完全正确，所以 autostart_stale() 返回 False、勾照样打着、开机也照样登录，
# 只是**没有守护**（掉线不重连）。不单独判这一条，用户升级后会觉得「没修好」。
C.set_autostart(True)
old_link = os.path.join(TMP, C.AUTOSTART_LNK_NAME)
C.make_lnk(old_link, EXE, arguments="--auto", icon=EXE, work_dir=os.path.dirname(EXE))
check("旧参数 -> 参数陈旧 = True", C.autostart_outdated() is True)
check("旧参数但目标没变 -> 不算失效（stale=False）", C.autostart_stale() is False)
C.set_autostart(True)
check("重新勾选后 -> 参数不陈旧", C.autostart_outdated() is False)
C.set_autostart(False)

print("[10] 计划任务优先，建不了才退回启动文件夹")
# 背景（2026-09-20）：实测同一台机器上，登录触发的计划任务（GHelper / PowerToys）
# 在开机后 25~27 秒就跑起来了，而启动文件夹里的 .lnk 要等到 85 秒
# （Windows 先把 HKCU\Run 的 6 项跑完）。所以自启改成优先建计划任务。
# 真建任务会污染用户机器，这里用假实现验「选路」逻辑本身。
fake = {"exists": False}


def fake_create():
    fake["exists"] = True
    return True, ""


def fake_delete():
    fake["exists"] = False
    return True, ""


C.task_state = lambda: {"exists": fake["exists"], "command": EXE,
                        "arguments": C.AUTOSTART_ARGS, "logon": True}
C.task_create = fake_create
C.task_delete = fake_delete

ok, why = C.set_autostart(True)
check("建了任务 -> ok", ok, why)
check("任务在 -> is_autostart_on = True", C.is_autostart_on() is True)
check("任务在 -> autostart_mode = task", C.autostart_mode() == "task")
# 这条最要紧：任务和 .lnk 同时留着 = 开机时起两份 --auto --guard
check("任务在时不留 .lnk（否则双触发）", len(C.autostart_links()) == 0, names())
check("任务在 -> 不算失效", C.autostart_stale() is False)
check("任务在 -> 参数不陈旧", C.autostart_outdated() is False)
ok, why = C.set_autostart(False)
check("关闭 -> ok", ok, why)
check("关闭 -> 任务没了", fake["exists"] is False)
check("关闭 -> is_autostart_on = False", C.is_autostart_on() is False)
check("关闭 -> autostart_mode = ''", C.autostart_mode() == "")

print("[11] 计划任务建不了时，退回启动文件夹也要能用")
C.task_create = lambda: (False, "模拟：组策略禁用")
ok, why = C.set_autostart(True)
check("仍然返回 ok（退回 .lnk）", ok, why)
check("退回后 .lnk 在", len(C.autostart_links()) == 1, names())
check("退回后 autostart_mode = lnk", C.autostart_mode() == "lnk")
C.set_autostart(False)
check("关闭后清干净", C.is_autostart_on() is False and len(C.autostart_links()) == 0,
      names())

print("[12] 从别的位置跑时，自启迁移必须**什么都不动**")
# 背景（2026-09-20 真踩到，一次构建就把用户机器搞坏了）：
#   build.py 的冒烟测试会拿 dist21 里的新 exe 跑一次 --selftest。那次运行如果
#   也去做「.lnk 换计划任务」的迁移，就会把自启指向**构建产物**
#   （...\campus-login-app\dist21\CampusLogin.exe），还顺手删掉用户的 .lnk。
#   判据：.lnk 记着用户装在哪 —— 当前 exe 不是那一份，就一律别动。
saved_mark = C.AUTOSTART_UPGRADE_MARK
C.AUTOSTART_UPGRADE_MARK = os.path.join(TMP, "autostart-upgrade.json")
fake["exists"] = False
C.task_state = lambda: {"exists": False, "command": "", "arguments": "", "logon": False}
C.task_create = fake_create
C.task_delete = fake_delete
C.set_autostart_lnk(True)
check("前置：.lnk 已就位", len(C.autostart_links()) == 1, names())

C.app_path = lambda: os.path.join(TMP, "别处", "校园网自动登录.exe")
changed, why = C.autostart_upgrade()
check("从别的位置跑 -> 不迁移", changed is False, why)
check("从别的位置跑 -> 没建任务", fake["exists"] is False)
check("从别的位置跑 -> .lnk 还在", len(C.autostart_links()) == 1, names())
check("从别的位置跑 -> **不写升级标记**（否则真装的那份被跳过）",
      not os.path.isfile(C.AUTOSTART_UPGRADE_MARK))

C.app_path = lambda: EXE
changed, why = C.autostart_upgrade()
check("回到用户装的那份 -> 迁移", changed is True, why)
check("迁移后任务在", fake["exists"] is True)
check("迁移后 .lnk 被清掉（留着会双触发）", len(C.autostart_links()) == 0, names())
check("迁移后写了升级标记", os.path.isfile(C.AUTOSTART_UPGRADE_MARK))
changed, why = C.autostart_upgrade()
check("再跑一次 -> 不重复迁移", changed is False, why)

# 收尾：把桩恢复成「没任务、没自启」，别影响后面的判定
C.AUTOSTART_UPGRADE_MARK = saved_mark
C.task_state = lambda: {"exists": False, "command": "", "arguments": "", "logon": False}
fake["exists"] = False
C.set_autostart_lnk(False)

print("\n结果:", "全部通过" if ok_all else "有失败")
if ok_all:
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print("临时目录保留待查:", TMP)
# ⚠️ 必须带退出码。原来这里什么都不返回 —— 脚本永远以 0 退出，
#    也就是说「检查通过」和「检查失败」在调用方看来一模一样，
#    失败会被静默吞掉（这正是「检查通过 ≠ 检查真的在跑」那类坑）。
sys.exit(0 if ok_all else 1)
