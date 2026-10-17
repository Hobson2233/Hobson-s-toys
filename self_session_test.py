# -*- coding: utf-8 -*-
"""「上次的登录态」存盘层（2026-10-17 加）。**不联网、不碰真实数据。**

用户的原话是「看能不能登录后一段时间免登录」。做法是把自助服务的会话 cookie
（`JSESSIONID-BOSS-0`）**加密**存下来，下次打开先试着复用。

所以这个测试的重点**不是**「能不能存下来」，而是**它在什么情况下必须拒绝**：

  ① 🔴 **磁盘上不能有明文** —— cookie 等价于一次登录态。和密码一样走 DPAPI。
  ② 🔴 **账号对不上就必须丢掉** —— 拿 A 的会话去查 B，服务端会给 B 的数据，
     而界面上的数字**看着完全正常**，用户根本发现不了。这是最危险的一条。
  ③ 🔴 **过期 / 时间在未来的存档不能信** —— 存档只说明「多久以前存的」，
     不说明「服务端还认不认」。超过 `SELF_SESSION_AGE` 就别试了。
  ④ 🔴 **解不开要整份作废**，不能半份复用 —— 半个 cookie 集合等于一个坏会话，
     它会以「莫名其妙的 errcode=-1」在后面出现。
  ⑤ **坏形状不吃** —— `"zzz"` 这种字符串不能被当成 `("z", "z")` 塞进 jar。

⚠️ 每一组配**阴性对照**（故意走不加密那条路，必须能搜到明文）——
   没有对照的话，「搜不到明文」可能只是因为搜错了地方。

用法：
    python self_session_test.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datasafe            # noqa: E402
import campus_login as C   # noqa: E402
import self_service as S   # noqa: E402

# 🔴 必须在碰任何文件之前沙箱 —— 否则下面的写盘会落到真实数据目录上。
datasafe.sandbox(tag="self_session_test")

PASS, FAIL = [], []
NOW = 1760000000.0          # 固定「现在」，测试才确定
COOK = [("JSESSIONID-BOSS-0", "ABCDEF123456")]
# 🔴 **绝不用真实学号**（红线 1）。和 `selfwin_test.py` 用同一批假号。
ACCT = "2025000000"
BASE = "http://202.196.169.166"


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  %s %-52s got=%r want=%r" % ("[OK]" if ok else "[!!]", name, got, want))


def check_true(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("[OK]" if cond else "[!!]", name, extra))


def section(t):
    print("\n" + t)


def _raw_text():
    if not os.path.isfile(C.SELF_SESSION_FILE):
        return ""
    with open(C.SELF_SESSION_FILE, "rb") as f:
        return f.read().decode("utf-8", "replace")


def _wipe():
    C.clear_self_session()


# ------------------------------------------------------------ 1. 存 + 读
section("1. 存下来能原样读回来")
_wipe()
ok, why = C.save_self_session(ACCT, BASE, COOK, now=NOW)
check("保存成功", (ok, why), (True, ""))
check_true("文件真的写出来了", os.path.isfile(C.SELF_SESSION_FILE), "")
pairs, why2 = C.read_self_session(ACCT, BASE, now=NOW + 60)
check("读回来的 cookie", pairs, COOK)
check("读回来没有原因（= 一切正常）", why2, "")

# 大小写/前后空格：账号不该因为多了个空格就认不出来
pairs2, _ = C.read_self_session("  %s  " % ACCT, BASE + "/", now=NOW + 60)
check("账号带空格、地址带斜杠也能认出来", pairs2, COOK)


# ------------------------------------------------------------ 2. 明文检查
section("2. 🔴 磁盘上不能有 cookie 明文（含阴性对照）")
text = _raw_text()
check_true("文件里搜不到 cookie 的值", "ABCDEF123456" not in text, "")
check_true("文件里确实有 `enc:v1:` 前缀（说明真加密了）", "enc:v1:" in text, "")
check_true("文件里有账号（要按账号比对，故意留的是明文）",
           ACCT in text, "")

# 阴性对照：故意写一份**不加密**的，必须能搜到 —— 否则上面那条等于没测
with open(C.SELF_SESSION_FILE, "w", encoding="utf-8") as f:
    json.dump({"v": 1, "account": ACCT, "base": BASE, "savedAt": int(NOW),
               "cookies": [[COOK[0][0], COOK[0][1]]]}, f)
check_true("【对照】不加密时确实搜得到明文",
           "ABCDEF123456" in _raw_text(), "")
_wipe()


# ------------------------------------------------------------ 3. 账号串号
section("3. 🔴 账号对不上必须丢掉（防串号）")
C.save_self_session(ACCT, BASE, COOK, now=NOW)
pairs, why = C.read_self_session("2026000001", BASE, now=NOW + 60)
check("换成别的账号读 ⇒ 不返回任何 cookie", pairs, [])
check_true("并且说明了原因（不是「没存过」）", "另一个账号" in why, why)
check_true("文件**没被删**（还没确认它无效，只是不给我们用）",
           os.path.isfile(C.SELF_SESSION_FILE), "")
# 连「顺手清掉」都不许：别人账号的存档，凭什么被当前界面删掉
check("带账号清 ⇒ 别人的存档不归我删",
      C.clear_self_session("2026000001"), False)
check_true("所以文件还在", os.path.isfile(C.SELF_SESSION_FILE), "")
check("带对的账号清 ⇒ 删掉", C.clear_self_session(ACCT), True)


# ------------------------------------------------------------ 4. 时间
section("4. 过期 / 时间在未来")
# 上一节末尾把它删了，这里重新存一份
C.save_self_session(ACCT, BASE, COOK, now=NOW)
pairs, why = C.read_self_session(ACCT, BASE,
                                 now=NOW + C.SELF_SESSION_AGE - 60)
check("差 1 分钟到期 ⇒ 还能用", pairs, COOK)
pairs, why = C.read_self_session(ACCT, BASE, now=NOW + C.SELF_SESSION_AGE + 60)
check("超过 24 小时 ⇒ 丢掉", pairs, [])
check_true("原因是「超过 24 小时」", "小时" in why, why)
pairs, why = C.read_self_session(ACCT, BASE, now=NOW - 3600)
check("存档时间是「未来」⇒ 丢掉（系统时间被改过）", pairs, [])
check_true("原因是「未来」", "未来" in why, why)


# ------------------------------------------------------------ 5. 坏存档
section("5. 坏存档一律丢掉，不许崩")
_wipe()
pairs, why = C.read_self_session(ACCT, BASE, now=NOW)
check("文件不在 ⇒ 空 + 没存过", (pairs, why), ([], "没存过"))

with open(C.SELF_SESSION_FILE, "w", encoding="utf-8") as f:
    f.write("{这不是 json")
check("文件是坏 JSON ⇒ 空", C.read_self_session(ACCT, BASE, now=NOW)[0], [])

C.save_self_session(ACCT, BASE, COOK, now=NOW)
raw = json.loads(_raw_text())
raw["v"] = 999
with open(C.SELF_SESSION_FILE, "w", encoding="utf-8") as f:
    json.dump(raw, f)
check("版本号不认 ⇒ 空", C.read_self_session(ACCT, BASE, now=NOW)[0], [])

raw["v"] = C.SELF_SESSION_VERSION
raw["savedAt"] = "不是数字"
with open(C.SELF_SESSION_FILE, "w", encoding="utf-8") as f:
    json.dump(raw, f)
check("savedAt 不是数字 ⇒ 空", C.read_self_session(ACCT, BASE, now=NOW)[0], [])

raw["savedAt"] = int(NOW)
raw["cookies"] = [["JSESSIONID-BOSS-0", "enc:v1:bm90LWEtcmVhbC1ibG9i"]]
with open(C.SELF_SESSION_FILE, "w", encoding="utf-8") as f:
    json.dump(raw, f)
pairs, why = C.read_self_session(ACCT, BASE, now=NOW)
check("解不开（换机器/换用户）⇒ 整份作废", pairs, [])
check_true("原因是「解不开」", "解不开" in why, why)


# ------------------------------------------------------------ 6. 写入边界
section("6. 该拒绝存的时候要拒绝")
_wipe()
check("没账号 ⇒ 不存", C.save_self_session("", BASE, COOK, now=NOW)[0], False)
check_true("而且真的没写文件", not os.path.isfile(C.SELF_SESSION_FILE), "")
check("没有 cookie ⇒ 不存",
      C.save_self_session(ACCT, BASE, [], now=NOW)[0], False)
check("cookie 值全是空串 ⇒ 不存",
      C.save_self_session(ACCT, BASE, [("a", ""), ("", "b")], now=NOW)[0], False)


# ------------------------------------------------------------ 7. 清除
section("7. 清除要幂等，而且进隐私清单")
C.save_self_session(ACCT, BASE, COOK, now=NOW)
check_true("第一次清：删掉了", C.clear_self_session(), "")
check_true("第二次清：不报错", C.clear_self_session() is False, "")
check_true("🔴 `self-session.json` 在 `PRIVATE_FILES` 里（清凭据会连它一起删）",
           "self-session.json" in C.PRIVATE_FILES, C.PRIVATE_FILES)
check_true("数据目录就是那个文件所在的目录",
           os.path.dirname(C.SELF_SESSION_FILE) == os.path.abspath(C.data_dir()),
           C.SELF_SESSION_FILE)


# ------------------------------------------------------------ 8. jar 搬运
section("8. `SelfService` 的 cookie 搬运（不联网）")
svc = S.SelfService(base=BASE)
check("新会话是空的", svc.export_cookies(), [])
check("塞两条", svc.import_cookies([["JSESSIONID-BOSS-0", "X1"],
                                    ["other", "Y2"]]), 2)
check("导出来就是塞进去的", sorted(svc.export_cookies()),
      [("JSESSIONID-BOSS-0", "X1"), ("other", "Y2")])
# 🔴 坏形状不许被当成 (name, value)：`"zzz"` 取 item[0]/item[1] 会得到 "z"/"z"
svc2 = S.SelfService(base=BASE)
check("坏形状一条都不收", svc2.import_cookies(
    [None, 123, "zzz", ("", "v"), ("k", ""), ["a", "b", "c"], {"a": 1}]), 0)
check("jar 仍然是空的", svc2.export_cookies(), [])


# ------------------------------------------------------------ 9. 收尾
section("9. 收尾：真实数据一个字节都没动")
print("  " + datasafe.assert_untouched())

print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
sys.exit(1 if FAIL else 0)
