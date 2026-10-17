# -*- coding: utf-8 -*-
"""日志轮转（2026-10-17 加）。**不联网、不碰真实数据。**

为什么要有它：
    日志**只增不减**，而 `log()` 每次都 open/write/close 整个文件 ——
    实测三周 400 KB、一年 5~6 MB，文件越大越慢（红线 61 量过 1~2.5 秒），
    而界面线程也会写日志。所以满 1 MB 就轮转成 `campus-login.log.1`。

这个测试盯的是**轮转最容易出错的三件事**（每一件都能静默坏掉）：

  ① 🔴 **轮转不能把日志弄丢** —— 旧内容必须完整地出现在 `.1` 里。
     写错成「清空」的话，用户报问题时最需要的那段历史正好没了。
  ② 🔴 **`.1` 必须进 `PRIVATE_FILES`** —— 否则「清除本机保存的账号密码与日志」
     只删了当前那份，旧的那份（同样有学号、本机 IP、失败原因）还留在盘上。
     这是「以为清干净了」的典型。
  ③ 🔴 **`.1` 必须进 `datasafe._PATH_ATTRS`** —— 否则测试/探针的沙箱盖不住它，
     轮转一发生就会去动**真实**数据目录里的文件。

⚠️ 配阴性对照：purge 不能是「把目录清空」，只删清单里列的东西。
   没有对照的话，「`.1` 被删了」也可能只是因为整个目录被清了。

用法：
    python log_rotate_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datasafe            # noqa: E402
import campus_login as C   # noqa: E402

# 🔴 必须在碰任何文件之前沙箱 —— 否则下面的写盘会落到真实数据目录上。
datasafe.sandbox(tag="log_rotate_test")

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  %s %-56s got=%r want=%r" % ("[OK]" if ok else "[!!]", name, got, want))


def check_true(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-56s %s" % ("[OK]" if cond else "[!!]", name, extra))


def section(t):
    print("\n" + t)


def _wipe():
    for p in (C.LOG_FILE, C.LOG_BACKUP_FILE):
        try:
            os.remove(p)
        except OSError:
            pass


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return -1


def _read(p):
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return b""


# ------------------------------------------------------------ 1. 沙箱盖住了
section("1. 沙箱盖住了这两条路径（否则会去动真实日志）")
check_true("LOG_FILE 在临时目录里",
           os.path.dirname(os.path.abspath(C.LOG_FILE)) ==
           os.path.dirname(os.path.abspath(C.CONFIG_FILE)),
           C.LOG_FILE)
check_true("LOG_BACKUP_FILE 也在同一目录",
           os.path.dirname(os.path.abspath(C.LOG_BACKUP_FILE)) ==
           os.path.dirname(os.path.abspath(C.LOG_FILE)),
           C.LOG_BACKUP_FILE)
check_true("LOG_BACKUP_FILE 已登记进 datasafe._PATH_ATTRS",
           "LOG_BACKUP_FILE" in datasafe._PATH_ATTRS, "")


# ------------------------------------------------------------ 2. 没满就不轮转
section("2. 没到上限 ⇒ 不轮转（阴性对照）")
_wipe()
with open(C.LOG_FILE, "wb") as f:
    f.write(b"a" * 1024)          # 1 KB，远低于上限
C.log("这一行不该触发轮转")
check("当前日志还在原处", _size(C.LOG_FILE) > 1024, True)
check("没有生成 .1", os.path.exists(C.LOG_BACKUP_FILE), False)


# ------------------------------------------------------------ 3. 满了就轮转
section("3. 超过上限 ⇒ 轮转到 .1，一个字都不丢")
_wipe()
old = b"OLD-LINE-" * 200000       # ~1.8 MB，稳超 LOG_MAX_BYTES
with open(C.LOG_FILE, "wb") as f:
    f.write(old)
C.log("轮转之后写进去的那一行")

check_true(".1 生成了", os.path.exists(C.LOG_BACKUP_FILE), C.LOG_BACKUP_FILE)
# ⚠️ 别把内容直接丢进 check() —— 1.8 MB 的 got/want 会把整条输出刷爆。
#    分开断言：先比字节数（出错时人看得懂），再比内容。
check("旧内容字节数一致", len(_read(C.LOG_BACKUP_FILE)), len(old))
check_true("旧内容逐字节一致", _read(C.LOG_BACKUP_FILE) == old, "")
check_true("当前日志变小了（重新开始写）", _size(C.LOG_FILE) < 4096,
           "%d 字节" % _size(C.LOG_FILE))
check_true("新写的那一行在当前日志里", "轮转之后写进去的那一行" in
           _read(C.LOG_FILE).decode("utf-8", "replace"), "")


# ------------------------------------------------------------ 4. 只留一份旧的
section("4. 再满一次 ⇒ 只留一份 .1，不累积")
first_backup = _read(C.LOG_BACKUP_FILE)
with open(C.LOG_FILE, "wb") as f:
    f.write(b"NEW-ROUND-" * 200000)
C.log("第二轮")
check_true("第二轮也轮转了", _read(C.LOG_BACKUP_FILE).startswith(b"NEW-ROUND-"), "")
check_true(".1 的内容换成了第二轮那份", _read(C.LOG_BACKUP_FILE) != first_backup, "")
check("没有 .2（只留一份）", os.path.exists(C.LOG_BACKUP_FILE + ".1"), False)
check_true("总占用有上限（当前 + .1 < 4 MB）",
           _size(C.LOG_FILE) + _size(C.LOG_BACKUP_FILE) < 4 * 1024 * 1024,
           "%d 字节" % (_size(C.LOG_FILE) + _size(C.LOG_BACKUP_FILE)))


# ------------------------------------------------------------ 5. 清凭据要连它一起删
section("5. 清凭据必须连 .1 一起删（隐私清单）")
check_true("PRIVATE_FILES 里有 campus-login.log.1",
           "campus-login.log.1" in C.PRIVATE_FILES, "")
check_true(".1 确实存在（下面要被删掉的那个）",
           os.path.exists(C.LOG_BACKUP_FILE), "")
# 阴性对照：一个**不在清单里**的文件，purge 不许碰它
keep = os.path.join(os.path.dirname(C.LOG_FILE), "not-in-private-list.txt")
with open(keep, "w", encoding="utf-8") as f:
    f.write("别删我")

d, removed, failed = C.purge_local_data(legacy=False)
check_true("当前日志被删了", not os.path.exists(C.LOG_FILE), "")
check_true("轮转出来的 .1 也被删了", not os.path.exists(C.LOG_BACKUP_FILE), "")
check("清单外的文件没被动", os.path.exists(keep), True)
check("删除过程没有失败项", failed, [])


# ------------------------------------------------------------ 6. 收尾
section("6. 收尾：真实数据一个字节都没动")
print("  " + datasafe.assert_untouched())

print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
sys.exit(1 if FAIL else 0)
