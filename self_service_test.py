# -*- coding: utf-8 -*-
"""`self_service.py`（郑航自助服务取数层）的**确定性**测试。**不联网。**

这里钉的不是「能不能取到数」，而是**边界**：

  1. 🔴 **敏感字段一个都不许漏出去。** 实测确认接口返回里带**身份证号、手机号、
     真实姓名、以及一个明文密码**。所以测试往假响应里塞几个**哨兵字符串**
     （`SENSITIVE_*`），断言它们在输出里**一个都找不到** ——
     包括把输出整个 JSON 序列化之后再找一遍。
  2. 🔴 **不许出现任何「写」接口。** 直接扫源码，出现 `kickonline` /
     `clearusermac` / 缴费 / 改密码 就报失败。要加必须是**明确改这里**，
     不能顺手带进来。
  3. 🔴 **密码算法要和前端逐字节一致。** 前端的输入是「每个 UTF-16 码元取低 8 位」，
     **不是 UTF-8** —— 中文密码下两者不同。这条错了，登录必失败，
     而且失败信息看起来像「密码错」。
  4. **单位换算的边界**：学校那个 `flows` 过滤器在 `1024` 时返回 `undefined`（他们的 bug），
     我们不许照抄。
  5. **失败要分得清「可以重试」和「别重试」**：验证码错可以换一张重来；
     其它登录失败**必须停下来**（密码连错可能被学校风控锁号）。

用法：
    python self_service_test.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import self_service as S        # noqa: E402

FAILS = []

# 哨兵：这些字符串**只应该出现在假响应里**，绝不该出现在输出里。
SENTINELS = ["SENSITIVE_IDCARD", "SENSITIVE_PHONE", "SENSITIVE_NAME",
             "SENSITIVE_PASSWORD", "SENSITIVE_OPERATOR"]

FAKE_INDEX = {
    "errcode": "0", "errmsg": "操作成功", "success": True,
    "data": {
        # --- 这些是**必须被丢掉**的 ---
        "idCardNum": "SENSITIVE_IDCARD",
        "accountMobile": "SENSITIVE_PHONE",
        "accountName": "SENSITIVE_NAME",
        "accountPwd": "SENSITIVE_PASSWORD",
        "operatorAccount": "SENSITIVE_OPERATOR",
        "operatorPwd": "SENSITIVE_PASSWORD",
        "openid": "SENSITIVE_OPERATOR",
        # --- 这些是**要留下**的 ---
        "accountId": "2507210230",
        "accountStatus": "正常",
        "areaName": "电信",
        "internetTacticsName": "校园网电信融合套餐",
        "chargeType": "0",
        "remainMoney": 0,
        "billingcycletime": "2026-10-06",
        "expirationDate": "2038-01-01",
        "terminalCount": 1,
        "showFlag": "X,S,G,I,Q,R,A,B",
    },
}

FAKE_CHART = {
    "errcode": "0", "errmsg": "操作成功", "success": True,
    "baseChartVo": {
        "dates": ["2026-10-11"],
        "allFlows": [3936.2],
        "uploadFlows": [1.0],
        "downloadFlows": [2.0],
        "allUseTime": ["20"],
    },
    "baseInfoVo": None, "data": None,
    "baseUseVo": {
        "accountStatus": "正常",
        "sameDayFlow": 2428.691523,
        "sameMonthFlow": 18454.782751,
        "cumulativeFlow": 690876.003675,
        "sameDayTime": 14.002222,
        "sameMonthTime": 104.017223,
        "cumulativeTime": 3355.498325,
        "sameMonthMoney": 0, "cumulativeMoney": 0, "remainMoney": 0,
        "totalMoney": 0,
        "limtFlows": "不限制", "limtTimes": "不限制",
    },
}

FAKE_ONLINE = {
    "errcode": "0", "errmsg": "获取在线清单成功", "success": True,
    "rows": [{
        "accountId": "2507210230",
        "accountMac": "84:9E:56:40:69:1D",
        "accountIp": "10.15.116.156",
        "onlineTime": "2026-10-12 09:37:49",
        "osInfo": "Windows",
        "nasId": "ZZHK-ZAX-BRAS",
        "terminalType": "a",
        # 🔴 这两个是 kickonline 要的参数 —— 必须被丢掉
        "billingId": "SENSITIVE_OPERATOR",
        "serverIp": "SENSITIVE_OPERATOR",
        "broswerType": "Chrome",
        "externalFlag": "1",
    }],
    "total": 0,
}


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-56s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    ok = bool(cond)
    print("  [%s] %-56s %s" % ("OK" if ok else "失败", name, detail))
    if not ok:
        FAILS.append(name)


def make(fake=None, raise_on=None):
    """造一个 SelfService，把 `_post` 换成假的。**不联网。**"""
    svc = S.SelfService()
    fake = fake or {}

    def _post(path, payload=None):
        if raise_on and path in raise_on:
            raise raise_on[path]
        if path not in fake:
            raise AssertionError("测试没准备这个接口的假数据：%s" % path)
        return fake[path]

    svc._post = _post
    return svc


# ------------------------------------------------------------------ 1. 密码算法

def test_md5():
    print("\n--- 1. 密码算法（前端 c() 的复刻） ---")
    import hashlib

    # 标准 MD5 已知向量
    check("md5('123456') 是标准值", S.js_md5("123456"),
          "e10adc3949ba59abbe56e057f20f883e")
    check("md5('') 是标准值", S.js_md5(""),
          "d41d8cd98f00b204e9800998ecf8427e")
    check("md5('abc') 是标准值", S.js_md5("abc"),
          "900150983cd24fb0d6963f7d28e17f72")

    # 🔴 中文：前端按 UTF-16 码元取**低** 8 位 ⇒ 不能写 s.encode("utf-8")。
    #    "密" = U+5BC6 → UTF-16LE 是 C6 5B → 低字节 0xC6
    #    "码" = U+7801 → UTF-16LE 是 01 78 → 低字节 0x01
    #    ⚠️ 注意是**低**字节（每两个字节取第一个），别取成高字节 ——
    #       这个错法算出来的值看起来同样像 MD5，只有真登录才会失败。
    want = hashlib.md5(bytes([0xC6, 0x01])).hexdigest()
    utf8 = hashlib.md5("密码".encode("utf-8")).hexdigest()
    check("中文密码按 UTF-16 取低字节", S.js_md5("密码"), want)
    check_true("中文密码 ≠ 按 UTF-8 算的 MD5", S.js_md5("密码") != utf8,
               "（这两个相等就说明复刻错了，登录会失败）")

    # 纯 ASCII 时两者应当**等价**（所以只有中文才暴露问题）
    check_true("纯 ASCII 时与 UTF-8 等价",
               S.js_md5("Abc123") == hashlib.md5(b"Abc123").hexdigest(),
               "Abc123")


# ------------------------------------------------------------- 2. 敏感字段不外漏

def test_no_sensitive_leak():
    print("\n--- 2. 🔴 敏感字段一个都不许漏出去 ---")
    svc = make({S.EP_INDEX: FAKE_INDEX, S.EP_CHART: FAKE_CHART,
                S.EP_ONLINE: FAKE_ONLINE})
    out = svc.load("2507210230")
    blob = json.dumps(out, ensure_ascii=False)

    for s in SENTINELS:
        check_true("输出里没有 %s" % s, s not in blob, "")

    # 输出里也不该出现这些**字段名**（值被换成哨兵了，但字段名本身也是线索）
    for key in ("idCardNum", "accountMobile", "accountName", "accountPwd",
                "operatorAccount", "operatorPwd", "openid",
                "billingId", "serverIp", "accountId", "showFlag",
                "broswerType", "externalFlag"):
        check_true("输出里没有键 %s" % key, key not in blob, "")

    # 该留下的必须在
    check("status 取到了", out["status"], "正常")
    check("plan 取到了", out["plan"], "校园网电信融合套餐")
    check("expire 取到了", out["expire"], "2038-01-01")
    check("balance 取到了", out["balance"], 0.0)
    check("flow.month 取到了", out["flow"]["month"], 18454.782751)
    check("time.month 取到了", out["time"]["month"], 104.017223)
    check("flow.limit 取到了", out["flow"]["limit"], "不限制")

    # 🔴 online() 只留展示字段 ⇒ **结构上凑不出 kickonline 的请求**
    d = out["devices"][0]
    check("在线设备条数", len(out["devices"]), 1)
    check("设备字段集合", sorted(d.keys()),
          ["ip", "kind", "mac", "nas", "os", "since"])
    check("设备 IP", d["ip"], "10.15.116.156")


# ------------------------------------------------------------------ 3. 单位换算

def test_fmt():
    print("\n--- 3. 单位换算（含学校那个 bug 的边界） ---")
    check("0 MB", S.fmt_flow(0), "0.00 MB")
    check("512.5 MB", S.fmt_flow(512.5), "512.50 MB")
    check("1023.99 MB", S.fmt_flow(1023.99), "1023.99 MB")
    # 🔴 学校前端 `e < 1024 ? MB : e > 1024 ? GB : void 0` ⇒ 1024 返回 undefined。
    #    我们不许照抄这个坑。
    check("1024 MB（学校会返回 undefined）", S.fmt_flow(1024), "1.00 GB")
    check("2048 MB", S.fmt_flow(2048), "2.00 GB")
    check("18454.782751 MB（本月）", S.fmt_flow(18454.782751), "18.02 GB")
    check("690876.003675 MB（累计）", S.fmt_flow(690876.003675), "674.68 GB")
    check("1 TB", S.fmt_flow(1024 * 1024), "1.00 TB")
    check("脏数据不崩（None）", S.fmt_flow(None), "0.00 MB")
    check("脏数据不崩（字符串）", S.fmt_flow("abc"), "0.00 MB")
    check("脏数据不崩（中文数字）", S.fmt_flow("12.5"), "12.50 MB")

    # 时长：>24 是**学校自己的数据口径问题**，我们照原样显示、不"修正"
    check("14.002222 小时", S.fmt_hours(14.002222), "14.00 小时")
    check("42 小时（学校自己会出这种值）", S.fmt_hours(42), "42.00 小时")
    check("3355.498325 小时", S.fmt_hours(3355.498325), "3355.50 小时")


# --------------------------------------------------------------- 4. 失败要分类

def test_error_kinds():
    print("\n--- 4. 失败分类（决定「能不能重试」） ---")
    # 验证码错 ⇒ 可以换一张重来
    svc = make(raise_on={S.EP_LOGIN: S.SelfError("other", "验证码错误")})
    try:
        svc.login("2507210230", "pw", "0000")
        check_true("验证码错应当抛异常", False, "却没抛")
    except S.SelfError as e:
        check("验证码错 → kind", e.kind, "captcha")

    # 其它登录失败 ⇒ 🔴 必须停下来，不许重试
    svc = make(raise_on={S.EP_LOGIN: S.SelfError("other", "账号或密码不正确")})
    try:
        svc.login("2507210230", "pw", "0000")
        check_true("密码错应当抛异常", False, "却没抛")
    except S.SelfError as e:
        check("密码错 → kind（不许重试）", e.kind, "login")

    # 会话过期 ⇒ 重新走验证码流程
    svc = make(raise_on={S.EP_INDEX: S.SelfError("session", "请重新登录")})
    check("会话过期 has_session → False", svc.has_session("x"), False)

    # 网络不通 ⇒ **抛**（"不知道"不能当成"没有"，否则会莫名其妙弹验证码）
    svc = make(raise_on={S.EP_INDEX: S.SelfError("net", "连不上自助服务")})
    try:
        svc.has_session("x")
        check_true("网络不通应当抛异常", False, "却没抛")
    except S.SelfError as e:
        check("网络不通 → kind", e.kind, "net")

    # 取验证码
    svc = make({S.EP_TOLOGIN: {"errcode": "0", "data": {
        "verifyCode": "iVBORw0KGgo="}}})
    png, logged = svc.fetch_captcha()
    check("取码：不是已登录", logged, False)
    check_true("取码：解出 PNG 字节", png is not None and png[:4] == b"\x89PNG",
               repr(png))
    svc = make({S.EP_TOLOGIN: {"errcode": "0", "data": {
        "user": {"accountId": "x"}}}})
    png, logged = svc.fetch_captcha()
    check("已有会话时不用输码", (png, logged), (None, True))


# ------------------------------------------------------- 5. 源码里不许有写接口

SRC = os.path.join(HERE, "self_service.py")

# 允许出现的全部接口。**加一个就要改这里** —— 这是故意的：
# 改白名单这个动作本身就是一次「停下来想一想」。
ALLOWED_EP = ["getindexchart.do", "getindexinfo.do", "getonline.do",
              "login.do", "tologin.do"]


def _scan_source():
    """解析 `self_service.py`，返回 (所有**非 docstring**的字符串字面量, AST)。

    🔴 **为什么要排除 docstring**：文档字符串里**故意**写着
    「绝不加 kickonline / clearusermac」——那是给后来人看的警告。
    把警告当违规就变成了「越写清楚越报错」，反而会逼人删掉警告。
    所以判据是「**代码里实际出现的接口名**」，不是「文件里出现过这几个字」。
    """
    import ast
    tree = ast.parse(open(SRC, encoding="utf-8").read())

    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ids.add(id(body[0].value))

    lits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in doc_ids]
    return lits, tree


def test_no_write_endpoints():
    print("\n--- 5. 🔴 源码里不许出现任何「写」接口 ---")
    import ast
    import re
    lits, tree = _scan_source()
    joined = "\n".join(lits)

    # ① 最强的判据：代码里出现的 `.do` 接口名，**必须正好**是白名单
    found = sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9]*\.do", joined)))
    check("代码里实际用到的接口集合", found, ALLOWED_EP)
    check_true("集合里没有多余接口",
               len(found) == len(ALLOWED_EP), "（多一个就说明加了新接口）")

    # ② 逐个点名「写」接口（防止有人写成不带 .do 的变量名）
    banned = ["kickonline", "clearusermac", "changepassword", "userdatachange",
              "onlinePay", "cardcharge", "moneytransfer", "buybusinesspackage",
              "selfstopping", "selfrecovery", "subscribeOrder", "canceluse",
              "userpwdreset", "modifyoperators", "usergroupchange",
              "uploadimg", "getcode", "surfinternet", "payway", "wxcodepay"]
    for b in banned:
        check_true("字面量里没有 %s" % b, b not in joined, "")

    # ③ 不许调 open()/print()/exec()/eval() 这些
    bad = []
    for n in ast_walk_calls(tree):
        f = n.func
        if isinstance(f, ast.Name) and f.id in ("open", "print", "exec",
                                                "eval", "compile", "input"):
            bad.append(f.id)
    check("没有 open/print/exec/eval 这类调用", sorted(set(bad)), [])

    # ④ 不许引入日志 / 网络以外的重家伙
    mods = set()
    for n in ast_walk_imports(tree):
        mods.add(n)
    check("没有引入 logging", [m for m in sorted(mods) if "log" in m], [])
    check("用到的标准库模块", sorted(mods),
          ["base64", "hashlib", "http.cookiejar", "json", "urllib.error",
           "urllib.request"])

    # ⑤ cookie 必须是**内存** jar，不能落盘。
    #    ⚠️ 这条**必须走 AST**，不能搜源码原文 —— 源码的注释里**故意**写着
    #       「用内存 CookieJar（不是 LWPCookieJar）」，搜原文会永远判失败。
    #       判据是「实际用到了哪个 jar 类」。
    jar_attrs = sorted({n.attr for n in ast.walk(tree)
                        if isinstance(n, ast.Attribute)
                        and n.attr.endswith("CookieJar")})
    check("用到的 cookie jar 类型（只许内存那种）", jar_attrs, ["CookieJar"])
    check_true("没有用到 os / pathlib（不碰文件系统）",
               "os" not in mods and "pathlib" not in mods, "")


def ast_walk_calls(tree):
    import ast
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def ast_walk_imports(tree):
    import ast
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.extend(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            out.append(n.module or "")
    return out


def main():
    print("=" * 78)
    print("self_service.py 自检（不联网）")
    print("=" * 78)
    test_md5()
    test_no_sensitive_leak()
    test_fmt()
    test_error_kinds()
    test_no_write_endpoints()
    print()
    print("=" * 78)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
