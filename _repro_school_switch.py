# -*- coding: utf-8 -*-
"""复现：「在界面上换了学校，可登录还是打到原来那所学校」。

🔴 这个坑是**静默的** —— 没有任何报错，界面上选中的学校名也变了，
   只有点「切换到此账号」时才会发现请求发去了错误的 portal 地址。

成因（两段历史叠出来的）：
  ① 老版本的 `on_save` / `on_switch` / `set_active_account` 都是
     `cfg = load_config(); cfg[...] = ...; save_config(cfg)`。
     而 `load_config()` 返回的是「DEFAULTS + 用户写的那份」**合并后的整份**，
     于是 DEFAULTS 里的 4 个键（portalHost / wlanAcIp / wlanAcName / timeoutSec）
     被**固化**进了 config.json —— 用户从来没写过它们，但文件里有了。
     （本机实测：`C:\\ProgramData\\CampusLogin\\config.json` 里确实躺着这 4 个键。）

  ② 新加的 `load_config()` 优先级是「DEFAULTS < 学校档案 < config.json 里的键」。
     第 ① 步留下的 4 个键属于「config.json 里的键」，**优先级最高**，
     于是学校档案里那套地址永远被它们盖住。

⇒ 结论：**选了嘉兴，`portalHost` 还是 `http://202.196.169.166`（郑航的）**。

跑法（必须用带 tkinter 的 build 解释器）：
    python _repro_school_switch.py
退出码 0 = 已修好，1 = 还能复现（即 bug 仍在）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import datasafe          # noqa: E402  —— 会写 config.json，必须先沙箱
datasafe.sandbox(tag="repro_school_switch")

import campus_login as C  # noqa: E402

FAIL = []


def check(name, got, want):
    ok = got == want
    print("  [%s] %s" % ("OK" if ok else "失败", name))
    if not ok:
        print("        期望 %r，实际 %r" % (want, got))
        FAIL.append(name)
    return ok


# ---- 造一份「老版本写出来的」config.json ------------------------------------
# 键和值都照抄本机真实文件（凭据用假的）—— 关键在于那 4 个键**不是用户写的**。
OLD_STYLE = {
    "userId": "2025000000", "passwd": "fake-pw",
    "portalHost": "http://202.196.169.166",
    "wlanAcIp": "10.0.1.10",
    "wlanAcName": "ZZHK-ZAX-BRAS",
    "timeoutSec": 8,
}
C.save_config(dict(OLD_STYLE))
print("初始 config.json 的键: %s" % sorted(OLD_STYLE))

print("\n" + "=" * 70)
print("1. 现状：老用户换到嘉兴，portalHost 该不该跟着换")
# 这就是新界面「学校下拉框」要做的事：写一个 school 键。
C.set_school("zjxu")
got = C.load_config().get("portalHost")
check("set_school('zjxu') 后 portalHost 变成嘉兴的",
      got, "http://wlan.zjxu.edu.cn")
check("set_school('zjxu') 后 portalType 是 srun",
      C.load_config().get("portalType"), "srun")
check("school_key() 认得出 zjxu", C.school_key(), "zjxu")
check("portal_type() 认得出 srun", C.portal_type(), "srun")

print("\n" + "=" * 70)
print("2. 化石键必须被清掉（它们是老版本自动写的，不是用户的选择）")
raw = C.read_json(C.CONFIG_FILE)
for k in ("portalHost", "wlanAcIp", "wlanAcName", "timeoutSec"):
    check("config.json 里的化石键 %-12s 已被清掉" % k, k in raw, False)
check("school 键写进去了", raw.get("school"), "zjxu")
check("凭据没被动过", (raw.get("userId"), raw.get("passwd")),
      ("2025000000", "fake-pw"))

print("\n" + "=" * 70)
print("3. 用户手写的「单项覆盖」必须留着（高级用户的路不能被堵死）")
C.save_config(dict(OLD_STYLE))
C.update_config({"timeoutSec": 20})          # 用户自己写的，不是化石
C.set_school("zjxu")
raw = C.read_json(C.CONFIG_FILE)
check("手写的 timeoutSec=20 被保住", raw.get("timeoutSec"), 20)
check("手写的 timeoutSec 仍然生效", C.load_config().get("timeoutSec"), 20)
check("但 portalHost 已经是嘉兴的",
      C.load_config().get("portalHost"), "http://wlan.zjxu.edu.cn")

print("\n" + "=" * 70)
print("4. 写配置不许再把档案值「固化」回文件（否则坑会重新长出来）")
C.save_config({"userId": "u1", "passwd": "p1"})
C.update_config({"userId": "u2", "passwd": "p2"})
raw = C.read_json(C.CONFIG_FILE)
check("update_config 之后文件里只有它自己写的键",
      sorted(raw), ["passwd", "userId"])
check("没有 portalHost 化石", "portalHost" in raw, False)

print("\n" + "=" * 70)
print("5. 认不出来的学校 key → 安静退回郑航（不能起不来）")
C.save_config({"school": "不存在的学校"})
check("未知 key 退回郑航 portalHost",
      C.load_config().get("portalHost"), "http://202.196.169.166")
check("school_key() 也退回 zua", C.school_key(), "zua")
check("portal_type() 退回 webauth", C.portal_type(), "webauth")

print("\n" + "=" * 70)
print("6. 沙箱收尾")
print("  " + datasafe.assert_untouched())

print("\n" + "=" * 70)
if FAIL:
    print("仍能复现 / 未通过 %d 项：" % len(FAIL))
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("全部通过 —— 「换学校不生效」这个坑已经堵上")
sys.exit(0)
