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


# ------------------------------------- 3b. 每日明细解析（2026-10-12 加）
#
# 这里的数据**照抄真实返回的形状**（2026-10-12 实测那份 dump.json），
# 尤其保留两个坑：日期有缺口、今天不在数组里。

# 真实那天的数（2026-10-11，实测）：up + down 正好等于 flow，分毫不差
REAL_20261011 = (3936.1761, 973.9739, 2962.2022, 20)


def fake_chart(dates, flows, ups, downs, hours, use=None):
    c = {
        "errcode": "0", "errmsg": "操作成功", "success": True,
        "baseChartVo": {"dates": dates, "allFlows": flows,
                        "uploadFlows": ups, "downloadFlows": downs,
                        "allUseTime": hours},
    }
    if use is not None:
        c["baseUseVo"] = use
    return c


def test_parse_days():
    print("\n--- 3b. 每日明细解析（日期有缺口 / 今天不在数组里） ---")
    TODAY = "2026-10-12"

    # ① 正常：三天，且**照实测那样** up + down == flow
    f, u, d, h = REAL_20261011
    c = fake_chart(["2026-10-09", "2026-10-10", "2026-10-11"],
                   [3906.4059, 3463.7843, f],
                   [683.3043, 1186.5343, u],
                   [3223.1016, 2277.2500, d],
                   [22, 18, h])
    days, summary = S.parse_days(c, today=TODAY)
    check("解析出 3 天", len(days), 3)
    check("按日期升序", [x["date"] for x in days],
          ["2026-10-09", "2026-10-10", "2026-10-11"])
    check("最后一天的值", (days[-1]["flow"], days[-1]["up"], days[-1]["down"]),
          (3936.1761, 973.9739, 2962.2022))
    check("时长也解析了", days[-1]["hours"], 20.0)
    # 🔴 实测结论：上下行和合计同一口径
    for x in days:
        check_true("%s 上行+下行 == 合计" % x["date"],
                   abs(x["up"] + x["down"] - x["flow"]) < 0.01,
                   "%.4f vs %.4f" % (x["up"] + x["down"], x["flow"]))

    # ② 日期乱序 ⇒ 排好
    c2 = fake_chart(["2026-10-11", "2026-10-09"], [3, 1], [1, 1], [2, 0], [1, 1])
    check("乱序也能排好", [x["date"] for x in S.parse_days(c2, today=TODAY)[0]],
          ["2026-10-09", "2026-10-11"])

    # ③ 🔴 日期有缺口（实测：9-30 直接跳到 10-07）⇒ **不许补**成中间那些天
    c3 = fake_chart(["2026-09-30", "2026-10-07"], [1, 2], [1, 1], [0, 1], [1, 1])
    got3 = S.parse_days(c3, today=TODAY)[0]
    check("缺口不补（只有服务端给的那两天）", [x["date"] for x in got3],
          ["2026-09-30", "2026-10-07"])
    check_true("没有凭空多出 10-01~10-06",
               all(x["date"] not in ("2026-10-01", "2026-10-06") for x in got3), "")

    # ④ 平行数组不等长 ⇒ 按最短的截断（**不能**错位）
    c4 = fake_chart(["2026-10-01", "2026-10-02", "2026-10-03"],
                    [10, 20, 30], [1, 2], [9, 18], [1, 2, 3])
    got4 = S.parse_days(c4, today=TODAY)[0]
    check("不等长时按最短的截断", len(got4), 2)
    check("截断后没串位", [(x["date"], x["flow"], x["up"]) for x in got4],
          [("2026-10-01", 10.0, 1.0), ("2026-10-02", 20.0, 2.0)])

    # ⑤ 单个数读不动 ⇒ 只有那一个字段受影响
    c5 = fake_chart(["2026-10-01"], ["abc"], ["x"], [None], [""])
    got5 = S.parse_days(c5, today=TODAY)[0][0]
    check("合计读不动 → 0（合计必须有个数）", got5["flow"], 0.0)
    check("上行读不动 → None（不是 0）", got5["up"], None)
    check("下行 null → None", got5["down"], None)
    check("时长读不动 → None", got5["hours"], None)

    # ⑥ 今天：从 baseUseVo 补，且**上下行是 None**
    c6 = fake_chart(["2026-10-11"], [3936.1761], [973.9739], [2962.2022], [20],
                    use={"sameDayFlow": 2428.691523, "sameDayTime": 14.002222,
                         "sameMonthFlow": 18454.782751, "sameMonthTime": 104.017223,
                         "cumulativeFlow": 690876.003675,
                         "cumulativeTime": 3355.498325,
                         "limtFlows": "不限制", "limtTimes": "不限制"})
    days6, s6 = S.parse_days(c6, today=TODAY)
    check("今天那条的日期用的是传进来的 today", s6["today"]["date"], TODAY)
    check("今天的合计来自 sameDayFlow", s6["today"]["flow"], 2428.691523)
    check("今天的时长", s6["today"]["hours"], 14.002222)
    check("🔴 今天的上行是 None（服务端不给，不是 0）", s6["today"]["up"], None)
    check("🔴 今天的下行是 None", s6["today"]["down"], None)
    check("今天不在 days 里（和实测一致）",
          all(x["date"] != TODAY for x in days6), True)
    # 🔴 再传**一个不可能是今天的日期**验一遍（2026-10-13 补的）。
    #    原来只传 TODAY="2026-10-12"，而跑测试那天**恰好就是 10-12** ⇒
    #    「参数 `today` 被同名局部变量覆盖」这个 bug 也是**绿的**
    #    （真日期 == 传进来的日期，两者分不开）。过了午夜才红。
    #    ⇒ 凡「传进去的参数 vs 函数自己算的默认值」，测试必须让两者**不可能相等**。
    _, s99 = S.parse_days(c6, today="1999-01-01")
    check("传一个不可能的日期，必须原样用（这条才抓得住参数被覆盖）",
          s99["today"]["date"], "1999-01-01")
    check("本月合计", s6["month"]["flow"], 18454.782751)
    check("累计合计", s6["total"]["flow"], 690876.003675)
    check("限额", s6["limit"]["flow"], "不限制")

    # ⑦ today 不传时取本机当天（只验格式，不验具体值 —— 验具体值会「今天绿明天红」）
    days7, s7 = S.parse_days(c6)
    check_true("不传 today 也能给出一个日期", bool(s7["today"]["date"]), s7["today"]["date"])
    check_true("且长得像 YYYY-MM-DD",
               len(s7["today"]["date"]) == 10 and s7["today"]["date"][4] == "-", "")

    # ⑧ 空 / 坏输入一律不崩，而且**两条路径的形状必须一致**
    #    （第一版就是「坏输入」那条路少了几层，被这条断言抓出来的）
    empty_days, empty_sum = S.parse_days(None, today=TODAY)
    check("坏输入 → 没有明细", empty_days, [])
    check("坏输入 → today 是 None", empty_sum["today"], None)
    check("空汇总的键一个都不少", sorted(empty_sum),
          ["limit", "month", "today", "total"])
    check("两条路径形状一致（空 dict 和 None 走的是同一条）",
          S.parse_days({}, today=TODAY), (empty_days, empty_sum))
    check("空汇总里 total 的键也在", sorted(empty_sum["total"]), ["flow", "hours"])
    check("空汇总里 month 的键也在", sorted(empty_sum["month"]), ["flow", "hours"])
    check("空汇总里 limit 的键也在", sorted(empty_sum["limit"]), ["flow", "hours"])
    check("baseChartVo 是字符串", S.parse_days({"baseChartVo": "x"}, today=TODAY)[0], [])
    check("dates 是字符串", S.parse_days(
        {"baseChartVo": {"dates": "x"}}, today=TODAY)[0], [])
    check("没有 baseUseVo ⇒ 今天为空", S.parse_days(
        {"baseChartVo": {"dates": []}}, today=TODAY)[1]["today"], None)

    # ⑨ 同一天出现两次 ⇒ 后写的赢（别让重复把图表搞出两根柱子）
    c9 = fake_chart(["2026-10-01", "2026-10-01"], [1, 9], [1, 1], [0, 8], [1, 1])
    check("同一天去重（后写的赢）", S.parse_days(c9, today=TODAY)[0],
          [{"date": "2026-10-01", "flow": 9.0, "up": 1.0, "down": 8.0,
            "hours": 1.0}])

    # ⑩ `chart()` **只调一个接口** —— 假数据里没准备 getindexinfo，
    #    真去调了就会抛「测试没准备这个接口的假数据」
    svc = make({S.EP_CHART: c6})
    out = svc.chart("2507210230", today=TODAY)
    check("chart() 拿得到 days", len(out["days"]), 1)
    check("chart() 拿得到 summary", out["summary"]["today"]["date"], TODAY)



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
    # ⚠️ `datetime` 是 2026-10-12 加进来的（每日明细要标「今天是哪一天」）。
    #    它**不碰文件系统**，所以不破坏下面那条「不落盘」的断言 ——
    #    但它是**唯一**允许的例外，别顺手再加别的（尤其 os / pathlib / shutil）。
    check("用到的标准库模块", sorted(mods),
          ["base64", "datetime", "hashlib", "http.cookiejar", "json",
           "urllib.error", "urllib.request"])

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
    test_parse_days()
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
