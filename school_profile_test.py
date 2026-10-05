# -*- coding: utf-8 -*-
"""学校参数「数据驱动」测试 —— 换学校真的只改 config.json 就行吗？

背景（2026-10-05 可维护性体检第 5 项）：
    以前 `triggerUrl` / `checkTargets` / `loginExtra` 是**写死在源码里**的，
    换个学校 / 换个校区就得改 .py 再重新打包 —— README 里那句「改源码」就是这么来的。
    现在三个都走访问器读 config.json 覆盖，这个测试负责证明三件事：

      1) **没有 config 时**，取值和默认常量逐字相同（没有悄悄改变行为）
      2) **写了 config 时**，取值真的跟着变（否则「配置生效了」是句空话）
      3) **写坏了**（类型不对 / 空表 / 缺键）时退回默认，不崩、不变成空表

    ⚠️ 第 2 条最关键：只验第 1 条的话，「访问器压根没读 config」也能全绿。
       所以每一项都配了阳性对照（改过去 → 必须变；清掉 → 必须变回来）。

⚠️ 全程不碰真实数据目录：第一件事就是把 C.CONFIG_FILE 指到临时文件
   （跟 `fresh_test.py` 一个手法），末尾再核对真实 config.json 的 md5 没变。
"""
import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  [%s] %-50s got=%r want=%r" % ("OK" if ok else "!!", name, got, want))


def section(t):
    print("\n=== %s ===" % t)


def _md5(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except Exception:
        return None                      # 不存在也算一种「状态」，前后一致即可


# 真实配置路径 —— 只读看一眼，用来证明这个测试没动它。
try:
    import datasafe
    _REAL_DIR = datasafe.real_dir()
except Exception:
    _REAL_DIR = C.data_dir()
_REAL_CFG = os.path.join(_REAL_DIR, "config.json")
_REAL_BEFORE = _md5(_REAL_CFG)

# 🔴 **第一件事**：把配置指向临时目录。下面所有读写都落在那儿。
TMP = tempfile.mkdtemp(prefix="schoolprofile_")
C.CONFIG_FILE = os.path.join(TMP, "config.json")
assert not os.path.isfile(C.CONFIG_FILE)

DEFAULTS_ALL = {
    "trigger_url": C.TRIGGER_URL,
    "check_targets": list(C.CHECK_TARGETS),
    "login_extra": dict(C.LOGIN_EXTRA),
    "campus_subnets": list(C.DEFAULT_CAMPUS_SUBNETS),
}


def all_default():
    """四个访问器是不是**全部**回到了默认值。"""
    return (C.trigger_url() == DEFAULTS_ALL["trigger_url"]
            and C.check_targets() == DEFAULTS_ALL["check_targets"]
            and C.login_extra() == DEFAULTS_ALL["login_extra"]
            and C.campus_subnets() == DEFAULTS_ALL["campus_subnets"])


# ---------------------------------------------------------------- 1. 默认态
section("1. 没有 config 时，取值必须与默认常量逐字相同")
check("trigger_url() == TRIGGER_URL", C.trigger_url(), C.TRIGGER_URL)
check("check_targets() == CHECK_TARGETS", C.check_targets(), list(C.CHECK_TARGETS))
check("login_extra() == LOGIN_EXTRA", C.login_extra(), dict(C.LOGIN_EXTRA))
check("campus_subnets() == DEFAULT_CAMPUS_SUBNETS",
      C.campus_subnets(), list(C.DEFAULT_CAMPUS_SUBNETS))
check("（顺带）SCHOOL_KEYS 无重复", len(set(C.SCHOOL_KEYS)), len(C.SCHOOL_KEYS))

# ------------------------------------------------------- 2. 覆盖态（阳性对照）
section("2. 写了 config 之后，每一项都必须真的跟着变（阳性对照）")
C.save_config({
    "portalHost": "http://10.99.99.99",
    "wlanAcIp": "10.88.0.1",
    "wlanAcName": "OTHER-BRAS",
    "timeoutSec": 3,
    "campusSubnets": ["172.16.", "10.7."],
    "triggerUrl": "http://captive.example.edu/",
    "checkTargets": [["http://n.example.edu/generate_204", None, "ex"]],
    "loginExtra": {"pageid": "9", "templatetype": "2", "vendorNote": "别的学校"},
})
_cfg = C.load_config()
check("portalHost 跟着变", _cfg.get("portalHost"), "http://10.99.99.99")
check("wlanAcIp 跟着变", _cfg.get("wlanAcIp"), "10.88.0.1")
check("wlanAcName 跟着变", _cfg.get("wlanAcName"), "OTHER-BRAS")
check("timeoutSec 跟着变", _cfg.get("timeoutSec"), 3)
check("campusSubnets 跟着变", C.campus_subnets(), ["172.16.", "10.7."])
check("triggerUrl 跟着变", C.trigger_url(), "http://captive.example.edu/")
check("checkTargets 跟着变", C.check_targets(),
      [("http://n.example.edu/generate_204", None, "ex")])
_extra = C.login_extra()
check("loginExtra 覆盖了已有键 pageid", _extra.get("pageid"), "9")
check("loginExtra 覆盖了已有键 templatetype", _extra.get("templatetype"), "2")
check("loginExtra 能加新键", _extra.get("vendorNote"), "别的学校")
check("loginExtra 没提到的键仍是默认", _extra.get("scheme"), C.LOGIN_EXTRA["scheme"])
check("loginExtra 是浅合并（键数 = 默认 + 1）",
      len(_extra), len(C.LOGIN_EXTRA) + 1)

# ------------------------------------------- 3. 真正发出去的东西用上了覆盖值
section("3. 表单 / 登录参数真的用上了覆盖值（不只是访问器自嗨）")
_a = C.auth_form("2026000001", "secret")
check("auth_form 带上了自定义 pageid", _a.get("pageid"), "9")
check("auth_form 仍带 userId/passwd",
      (_a.get("userId"), _a.get("passwd")), ("2026000001", "secret"))
_l = C.logout_form("2026000001", "TK")
check("logout_form 的 url 用了自定义 triggerUrl",
      _l.get("url"), "http://captive.example.edu/")
check("logout_form 仍去掉 remInfo/desc_lb",
      ("remInfo" in _l, "desc_lb" in _l), (False, False))
_u = C.url_parameter()
check("url_parameter() 里带上了自定义 triggerUrl",
      "captive.example.edu" in _u, True)

# ------------------------------------------------- 4. 写坏的配置要退回默认
section("4. 写坏的配置必须退回默认，而不是把程序弄瘸")
for bad, why in [
    ({"triggerUrl": ""}, "空串"),
    ({"triggerUrl": 123}, "不是字符串"),
    ({"triggerUrl": None}, "null（load_config 会整个丢掉）"),
    ({"checkTargets": []}, "空表"),
    ({"checkTargets": "http://x/"}, "不是列表"),
    ({"checkTargets": [123, None]}, "元素类型全不对"),
    ({"loginExtra": "not-a-dict"}, "不是字典"),
    ({"loginExtra": []}, "是列表"),
    ({"campusSubnets": []}, "空表"),
    ({"campusSubnets": 5}, "不是列表"),
]:
    C.save_config(bad)
    check("坏配置(%-22s) → 全部退回默认" % why, all_default(), True)

# --------------------------------- 5. SCHOOL_KEYS 每个键都要有访问器接上
section("5. SCHOOL_KEYS 里每个键都必须接线（防「加了键忘了接访问器」）")
_probes = {
    "portalHost":    lambda: C.load_config().get("portalHost"),
    "wlanAcIp":      lambda: C.load_config().get("wlanAcIp"),
    "wlanAcName":    lambda: C.load_config().get("wlanAcName"),
    "timeoutSec":    lambda: C.load_config().get("timeoutSec"),
    "campusSubnets": C.campus_subnets,
    "triggerUrl":    C.trigger_url,
    "checkTargets":  C.check_targets,
    "loginExtra":    C.login_extra,
}
_sentinel = {
    "portalHost": "http://probe.invalid", "wlanAcIp": "9.9.9.9",
    "wlanAcName": "PROBE-BRAS", "timeoutSec": 1,
    "campusSubnets": ["77."], "triggerUrl": "http://probe.invalid/t",
    "checkTargets": [["http://probe.invalid/204", None, "probe"]],
    "loginExtra": {"pageid": "probe"},
}
check("探针表覆盖了 SCHOOL_KEYS 全部键",
      sorted(_probes) == sorted(C.SCHOOL_KEYS), True)
for _k in C.SCHOOL_KEYS:
    C.save_config({_k: _sentinel[_k]})
    _on = _probes[_k]()
    C.save_config({})                    # 阳性对照：清掉配置必须变回去
    _off = _probes[_k]()
    check("%-14s 覆盖生效、清掉即回默认" % _k, _on != _off, True)

# ------------------------------------------------------------------ 6. 收尾
section("6. 收尾：真实数据目录一个字节都没动")
check("CONFIG_FILE 指向临时目录",
      os.path.abspath(os.path.dirname(C.CONFIG_FILE)).startswith(
          os.path.abspath(TMP)), True)
check("真实 config.json 未被改动（md5 前后一致）", _md5(_REAL_CFG), _REAL_BEFORE)

shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 70)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
sys.exit(1 if FAIL else 0)
