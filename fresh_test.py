# -*- coding: utf-8 -*-
"""模拟「拷到一台全新电脑」的首次运行。

本机存在老数据（%APPDATA%\\CampusLogin，以及可能的 C:\\tools），所以直接跑
exe 时迁移逻辑会把旧账号继承过来，测不出真正的全新环境。
这里把程序眼里的一切路径都指到空目录、并把所有「老位置」也指向不存在的路径，
等价于一台没有任何历史数据的机器。

用法：
    python fresh_test.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print("%-46s %s %s" % (name, "PASS" if cond else "FAIL", extra))
    if not cond:
        ok = False


# ---------- 0) 先验真实环境：数据目录应该落在 C:\ProgramData ----------
real_dir = C.data_dir()
check("数据目录落在 C:\\ProgramData 下",
      real_dir.lower().startswith("c:\\programdata"), real_dir)

# ---------- 把程序眼里的一切路径搬到空目录 ----------
TMP = tempfile.mkdtemp(prefix="campus_fresh_test_")
NOPE = os.path.join(TMP, "no_such_legacy_dir")

C.CONFIG_FILE = os.path.join(TMP, "config.json")
C.ACCOUNTS_FILE = os.path.join(TMP, "accounts.json")
C.LOG_FILE = os.path.join(TMP, "campus-login.log")
C.RESULT_FILE = os.path.join(TMP, "last-result.json")
C.LEGACY_DIR = NOPE
# 「清过凭据」标记（0.3.5 加）也要指到临时目录：它是模块级常量，不拦住的话
# migrate_legacy_data() 会去看**真实**数据目录里有没有它 —— 那台机器上清过
# 一次凭据，这个测试的行为就跟着变了，等于测试依赖了运行环境。
C.PURGED_FLAG = os.path.join(TMP, "purged.flag")
# 关键：把「所有老位置」都指到不存在的地方。只改 LEGACY_DIR 已经不够了 ——
# 迁移还会去读 %APPDATA%\CampusLogin，本机那儿是有数据的。
C.legacy_data_dirs = lambda: [NOPE]
C.data_dir = lambda: TMP

# 1) 前置：目录确实是空的
leftover = [f for f in os.listdir(TMP) if f in
            ("config.json", "accounts.json", "last-result.json")]
check("前置：临时目录里没有历史数据", not leftover, leftover)

# 2) 首次运行不应该凭空造出配置
check("migrate_legacy_data() 返回 False", C.migrate_legacy_data() is False)
check("迁移没有生成 config.json", not os.path.isfile(C.CONFIG_FILE))

# 3) 没有配置时读出来的应该是空账号 + 正确的内置默认值
cfg = C.load_config()
check("load_config 账号为空", cfg["userId"] == "" and cfg["passwd"] == "")
check("load_config portal 默认值正确",
      cfg["portalHost"] == "http://202.196.169.166", cfg["portalHost"])
check("load_config AC 参数正确",
      cfg["wlanAcIp"] == "10.0.1.10" and cfg["wlanAcName"] == "ZZHK-ZAX-BRAS")

# 4) 账号列表也是空的
acc = C.load_accounts()
check("load_accounts 为空", acc.get("accounts") == {})

# 5) 界面能在「无配置」状态下正常构建
#
# 受控/无人值守会话里真建 Tk 窗口可能直接挂住（实测同一个二进制时而 0.9 秒、
# 时而十几秒、时而不返回），那是环境问题不是代码问题。所以给个开关跳过它，
# 免得整个测试跑不完 —— 用 --no-gui 时这一项报 SKIP 而不是 PASS，别假装测过。
if "--no-gui" in sys.argv:
    print("%-46s SKIP (--no-gui)" % "无配置时界面可构建")
else:
    # 受控会话里建 Tk 可能直接挂住，而 `run_gui(smoke=True)` 是**进程内**调用，
    # 外面没有超时能拦它 —— 2026-09-17 实测挂了 28 分钟没返回（tail 一直等，
    # 看起来像「测试很慢」，其实是永远不结束）。
    # 所以给它套一个看门狗：到点硬退，退出码 4（和「断言失败 1」区分开），
    # 让人一眼看出是卡住而不是测试没过。**超时不算通过**，别假装测过。
    import proc_tree
    disarm = proc_tree.arm_watchdog(120, "fresh_test 建 Tk 窗口（受控会话可能挂住）")
    try:
        C.run_gui(smoke=True)
        check("无配置时界面可构建", True)
    except Exception as e:
        check("无配置时界面可构建", False, repr(e))
    finally:
        disarm()

# 6) 存一个账号后能正常读回，并自动补全其余字段
C.save_config({"userId": "1234567890", "passwd": "pw"})
back = C.load_config()
check("保存后能读回账号",
      back["userId"] == "1234567890" and back["passwd"] == "pw")
check("保存后默认字段仍在", back["portalHost"] == "http://202.196.169.166")

# 7) 空账号时本地 IP 探测不能崩
try:
    ip = C.local_ipv4()
    check("local_ipv4 可用", bool(ip), ip)
except Exception as e:
    check("local_ipv4 可用", False, repr(e))

# 8) 迁移幂等：已经有 config.json 时不该再动它
C.save_config({"userId": "9999999999", "passwd": "keep"})
check("已有配置时迁移直接返回 False", C.migrate_legacy_data() is False)
check("已有配置没被覆盖", C.load_config()["userId"] == "9999999999")

# 9) --purge 的底层函数能清干净隐私文件
C.save_accounts({"accounts": {"111": {"passwd": "secret"}}, "lastOnline": "111"})
C.write_result("auto", "OK", True, "x")
d, removed, failed = C.purge_local_data()
check("purge 删掉了 config.json", "config.json" in removed)
check("purge 删掉了 accounts.json", "accounts.json" in removed)
check("purge 删掉了 last-result.json", "last-result.json" in removed)
check("purge 没有失败项", not failed, failed)
check("purge 后账号文件确实不存在", not os.path.isfile(C.ACCOUNTS_FILE))
check("purge 后目录里没有残留隐私文件",
      not any(os.path.isfile(os.path.join(TMP, n)) for n in C.PRIVATE_FILES))

print()
print("结论:", "全部通过" if ok else "有失败项")

# 收尾：删掉这次造的临时目录。
# 之前用 mkdtemp 却没有清理，跑 N 次就在 %TEMP% 里留 N 个目录。
# 失败时保留，方便进去看现场。
if ok:
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print("临时目录保留待查:", TMP)
sys.exit(0 if ok else 1)
