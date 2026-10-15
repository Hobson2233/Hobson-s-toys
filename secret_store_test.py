# -*- coding: utf-8 -*-
"""密码存储：DPAPI 加密（2026-10-15 加）。

用户的原话是「密码加密一下吧」。所以这个测试的重点**不是**「加密函数能跑」，
而是三件用户真的会碰到的事：

  ① **磁盘上真的没有明文** —— 这是用户要的效果。必须**端到端**验：
     走真实写入口 → 读文件的**字节** → 搜明文。
     只调 `encrypt_secret()` 证明不了任何事（文件可能压根没写对）。
  ② **老文件里的明文照样能用** —— 升级不能把老用户挡在门外。
  ③ **解不开时不能把密文当密码发出去** —— 那串乱码会被发到门户服务器上。

⚠️ 每一组都配**阴性对照**（故意走不加密的那条路，必须能搜到明文）。
   没有对照的话，①里「搜不到明文」可能只是因为「搜错了地方」。

用法：
    python secret_store_test.py
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datasafe            # noqa: E402
import campus_login as C   # noqa: E402

# 🔴 必须在碰任何文件之前沙箱 —— 否则下面的写盘会落到真实数据目录上。
datasafe.sandbox(tag="secret_store_test")

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  %s %-54s got=%r want=%r" % ("[OK]" if ok else "[!!]", name, got, want))


def check_true(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-54s %s" % ("[OK]" if cond else "[!!]", name, extra))


def section(t):
    print("\n=== %s ===" % t)


def disk(path):
    """文件的**原始字节**（不是解析后的 JSON）。搜明文必须看字节。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _md5(path):
    """文件的 md5，用来验「该不动的文件确实一个字节都没动」。"""
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


# --------------------------------------------------------------- 0. 前提
section("0. 前提：这台机器上 DPAPI 能用")
_avail = C.dpapi_available()
check_true("DPAPI 可用（不可用的话下面全都测不出加密行为）", _avail, "")
if not _avail:
    print("  🔴 DPAPI 不可用 —— 下面的断言会红，那是对的（功能确实没生效）")


# ------------------------------------------------------------ 1. 往返
section("1. 加解密往返")
for pw in ("simple", "MyP@ssw0rd", "中文密码测试", "a" * 200,
           "有 空格 和\t制表符", "emoji🔐混排"):
    enc = C.encrypt_secret(pw)
    label = pw if len(pw) <= 14 else pw[:14] + "…"
    check_true("往返 %r" % label,
               C.decrypt_secret(enc) == pw and enc != pw and C.is_encrypted(enc), "")
check("空串原样", C.encrypt_secret(""), "")
check("None 原样", C.encrypt_secret(None), None)
check("数字原样（不是字符串就别碰）", C.encrypt_secret(123), 123)


# ------------------------- 2. 阳性对照：必须是真加密，不是固定变换
section("2. 阳性对照：它得是**真加密**，不是某个固定变换")
_a = C.encrypt_secret("same-input")
_b = C.encrypt_secret("same-input")
check_true("同一明文两次加密结果不同（DPAPI 每次都带随机盐）", _a != _b, "")
check("但都能解回同一个明文",
      (C.decrypt_secret(_a), C.decrypt_secret(_b)), ("same-input", "same-input"))
check_true("密文里搜不到明文", "same-input" not in _a, "")


# --------------------------------------------------- 3. 兼容与容错
section("3. 兼容与容错（升级路径）")
check("老文件里的明文原样返回（不挡老用户）", C.decrypt_secret("plain-pw"), "plain-pw")
check("已经是密文不会二次加密", C.encrypt_secret(_a), _a)
check("坏 base64 不崩，返回空串", C.decrypt_secret("enc:v1:!!!!"), "")
check("被截断的密文不崩", C.decrypt_secret("enc:v1:AQAA"), "")
check("前缀后面是空的不崩", C.decrypt_secret("enc:v1:"), "")
check("空串原样", C.decrypt_secret(""), "")
check("None 原样", C.decrypt_secret(None), None)


# ------------------------------- 4. 🔴 端到端：config.json
section("4. 🔴 端到端：config.json 的**磁盘字节**里搜不到明文")
SECRET = "Zj7-Dpapi-Probe-9x"
C.update_config({"userId": "2026000001", "passwd": SECRET})
_raw = disk(C.CONFIG_FILE)
check_true("磁盘上搜不到明文密码", SECRET.encode("utf-8") not in _raw,
           "文件 %d 字节" % len(_raw))
check_true("磁盘上确实是 enc:v1: 密文",
           json.loads(_raw.decode("utf-8"))["passwd"].startswith(C.SECRET_PREFIX), "")
check("业务代码读回来还是明文", C.load_config()["passwd"], SECRET)

# 阴性对照：绕过加密那条路直接写明文 ⇒ 必须**能**搜到。
# 没有这一步，「搜不到」可能只是因为搜错了地方 / 压根没写文件。
C.write_json(C.CONFIG_FILE, {"userId": "2026000001", "passwd": SECRET})
check_true("阴性对照：明文直写时确实搜得到（证明上面那条真的在查）",
           SECRET.encode("utf-8") in disk(C.CONFIG_FILE), "")


# ------------------------------- 5. 🔴 端到端：accounts.json
section("5. 🔴 端到端：accounts.json 也一样（两个账号都查）")
ACC1, ACC2 = "Zj7-Account-Probe-9x", "second-pw-probe"
C.save_accounts({"accounts": {
    "2026000001": {"passwd": ACC1, "lastSuccess": "", "lastAttempt": "",
                   "lastResult": ""},
    "2026000002": {"passwd": ACC2, "lastSuccess": "", "lastAttempt": "",
                   "lastResult": ""}},
    "lastOnline": "2026000001"})
_ab = disk(C.ACCOUNTS_FILE)
check_true("磁盘上搜不到第 1 个账号的密码", ACC1.encode("utf-8") not in _ab, "")
check_true("磁盘上搜不到第 2 个账号的密码", ACC2.encode("utf-8") not in _ab, "")
_st = C.load_accounts()
check("两个账号都读回明文",
      (_st["accounts"]["2026000001"]["passwd"],
       _st["accounts"]["2026000002"]["passwd"]), (ACC1, ACC2))

# 阴性对照
C.write_json(C.ACCOUNTS_FILE, {"accounts": {"2026000001": {"passwd": ACC1}},
                               "lastOnline": ""})
check_true("阴性对照：accounts.json 明文直写时也搜得到",
           ACC1.encode("utf-8") in disk(C.ACCOUNTS_FILE), "")


# ------------------------------ 6. 🔴 update_config 返回值
section("6. 🔴 `update_config` 返回的那份必须是**明文**")
# 为什么单列一节：如果写的时候把入参就地加密，返回给调用方的那份也成了密文，
# 而调用方常常拿它直接去登录 ⇒ 表现为「明明存对了却登录失败」。
# 这个 bug 不报错、不看返回值也发现不了。
_back = C.update_config({"passwd": "Return-Probe"})
check("返回值里的 passwd 是明文", _back["passwd"], "Return-Probe")
check("读回来也是明文", C.load_config()["passwd"], "Return-Probe")


# ------------------------------------------- 7. 老文件升级
section("7. 老文件升级：明文能用 → 下次写盘自动加密")
LEGACY = "Legacy-Plain-Pw"
C.write_json(C.CONFIG_FILE, {"userId": "2026000003", "passwd": LEGACY})
check("老明文文件照样读得出来", C.load_config()["passwd"], LEGACY)
C.set_school("zjxu")                      # 走一次真实的写入口
check_true("写盘之后磁盘上不再有明文",
           LEGACY.encode("utf-8") not in disk(C.CONFIG_FILE), "")
check("而且密码没丢（用户无感）", C.load_config()["passwd"], LEGACY)


# ------------------------------------------- 8. 自检那行
section("8. 自检输出「密码加密: …」的三种状态")
# ① 混着明文时**必须如实报**，不能装作「已加密」。
#    这一条是**免费得到的** —— 第 5 节的阴性对照刚往 accounts.json 里写了一条明文，
#    所以此刻磁盘上正是「一条密文 + 一条明文」。第一版我把这条断言写成了
#    「报已加密」，结果它红了 —— 红得对，是**我的期望**错了，不是代码错了。
check_true("混着明文时如实提醒（不装作已加密）",
           C.secret_storage_text().startswith("⚠️ 还有"),
           C.secret_storage_text())
# ② 全都走一遍真实写入口之后 → 报「已加密」
C.update_config({"passwd": "S8-Probe"})
C.save_accounts({"accounts": {"2026000001": {"passwd": "S8-Probe"}},
                 "lastOnline": ""})
check_true("全部加密后报「已加密」",
           C.secret_storage_text().startswith("DPAPI 已加密"),
           C.secret_storage_text())
# ③ 一条都没存过
C.write_json(C.CONFIG_FILE, {})
C.write_json(C.ACCOUNTS_FILE, {})
check("一条都没存过时的说法", C.secret_storage_text(),
      "DPAPI 可用；本机还没存过密码")


# ---------------------------- 9. 启动时主动升级明文
section("9. 启动时主动升级明文（老用户不点保存也会被升）")
# 为什么单列一节：只在「写盘时加密」是不够的 —— 老用户升级后只要不再点一次
# 「仅保存」，那串明文就会**一直躺着**。而用户要的正是「别让它明文躺在那」。
C.write_json(C.CONFIG_FILE, {"userId": "2026000009", "passwd": "Boot-Plain-1"})
C.write_json(C.ACCOUNTS_FILE, {"accounts": {
    "2026000009": {"passwd": "Boot-Plain-2"},
    "2026000010": {"passwd": "Boot-Plain-3"}}, "lastOnline": ""})
check("报告处理了 3 条（config 1 + accounts 2）",
      C.upgrade_secrets_to_encrypted(), 3)
_b1, _b2 = disk(C.CONFIG_FILE), disk(C.ACCOUNTS_FILE)
check_true("config.json 里的明文没了", b"Boot-Plain-1" not in _b1, "")
check_true("accounts.json 里两个明文都没了",
           b"Boot-Plain-2" not in _b2 and b"Boot-Plain-3" not in _b2, "")
check("密码一个都没丢（能解回来）",
      (C.load_config()["passwd"],
       C.load_accounts()["accounts"]["2026000010"]["passwd"]),
      ("Boot-Plain-1", "Boot-Plain-3"))
# 幂等：再跑一次必须**一个字节都不写**。
# 否则每次开机都白改一次文件 —— 既没必要，也会让「文件被动过」这种排查信号失真。
_m1, _m2 = _md5(C.CONFIG_FILE), _md5(C.ACCOUNTS_FILE)
check("第二次报告 0 条（幂等）", C.upgrade_secrets_to_encrypted(), 0)
check("幂等：两个文件都没被改动",
      (_md5(C.CONFIG_FILE), _md5(C.ACCOUNTS_FILE)), (_m1, _m2))

# 🔴 自检入口**不许**动真实数据。这条是**保命**的：
#    build.py 的冒烟测试会拿刚打出来的新 exe 跑 --selftest，而那时用户手上
#    还在用的旧版 exe 读不懂 enc:v1: —— 真在自检里升级了，用户的登录当场就废。
check("普通启动（无参数）→ 要升级", C.should_upgrade_secrets([]), True)
check("--auto --guard（开机自启那条路）→ 要升级",
      C.should_upgrade_secrets(["--auto", "--guard"]), True)
check("--selftest → **不**升级", C.should_upgrade_secrets(["--selftest"]), False)
check("--guitest → **不**升级", C.should_upgrade_secrets(["--guitest"]), False)
check("--selftest 混在别的参数里也认得出",
      C.should_upgrade_secrets(["--auto", "--selftest"]), False)


# ------------------------------------------------------------- 10. 收尾
section("10. 收尾：真实数据一个字节都没动")
print("  " + datasafe.assert_untouched())

print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
sys.exit(1 if FAIL else 0)
