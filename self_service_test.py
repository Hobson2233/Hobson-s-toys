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
        "accountId": "2025000000",
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
        "accountId": "2025000000",
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
    out = svc.load("2025000000")
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
    out = svc.chart("2025000000", today=TODAY)
    check("chart() 拿得到 days", len(out["days"]), 1)
    check("chart() 拿得到 summary", out["summary"]["today"]["date"], TODAY)



# ------------------------------------------- 3c. A 档解析（2026-10-15 加）
#
# 数据**照抄真实返回的形状**（2026-10-15 实测那份 dump.json，227 条里的两条）。
# 保留三个坑：① 时长单位是**秒**（不是小时）；② `total` 是服务端说的总数、
# `fetched` 是我们真拉到的；③ 服务端会发字符串 `"(null)"`。

# 实测：chargingDuration == endDate − startDate，分毫不差
REAL_SESSION = {
    "id": 1,
    "accountId": "2025000000",
    "startDate": "2026-10-11 08:00:00",
    "endDate": "2026-10-11 20:00:08",
    "chargingDuration": 43208,          # = 12h0m8s
    "uploadFlows": 973.9739,
    "downloadFlows": 2962.2022,
    "fee": 0.0,
    "terminateCause": "用户请求断开",
    "terminalType": "a",
    "osInfo": "Windows",
    "accountMac": "84:9E:56:40:69:1D",
    "accountIp": "10.15.116.156",
    # --- 必须被丢掉的 ---
    "serverIp": "SENSITIVE_OPERATOR",
    "nasId": "SENSITIVE_OPERATOR",
    "vlanId": "SENSITIVE_OPERATOR",
    "broswerType": "Chrome",
    "areaName": "电信",
    "internetTacticsName": "校园网电信融合套餐",
}

FAKE_BILL = {
    "errcode": "0", "errmsg": "操作成功", "success": True,
    "rows": [REAL_SESSION],
    "total": 227,
    "data": {
        "allUpFlows": 35321.13,
        "allDownFlows": 261458.92,
        "allFees": 0.0,
        "onlineHour": 12,
        "onlineMin": 0,
        # --- 必须被丢掉的 ---
        "accountName": "SENSITIVE_NAME",
    },
}


# ============ A 档第二批（2026-10-16）的假数据 ============
#
# ⚠️ 这几份**不是实测拿到的**（真登录要人肉读验证码），是照着
#    `app.js` 模板里逆出的字段名编的。作用有两个：
#      ① 钉住「白名单确实丢掉了敏感字段」
#      ② 钉住「字段名对不上时不会崩」（下面的脏值测试）
#    🔴 **不能**拿它证明「真接口就长这样」—— 那要实测。

FAKE_BASEINFO = {
    "errcode": "0", "errmsg": "操作成功", "success": True,
    "data": {
        "authMessageVo": {
            # --- 必须被丢掉的（就在同一个对象里！）---
            "operatorAccount": "SENSITIVE_OPERATOR",
            "operatorPwd": "SENSITIVE_PASSWORD",
            # --- 要留下的：文本类 ---
            "openingDate": "2024-09-01",
            "effectiveDate": "2026-10-06",
            "expirationDate": "2038-01-01",
            "firstUsetime": "2024-09-01 10:00:00",
            "latestEditDate": "2026-10-06 12:00:00",
            "lastmonthlyChargetime": "2026-10-06",
            "authResult": "认证成功",
            "operationAuthResult": "认证成功",
            "operationAuthTime": "2026-10-16 19:00:00",
            "groupChangeDate": "2026-10-06",
            "groupChangeFlag": "0",
            "proxyCtrlResult": "正常",
            "proxyCtrlStopTime": "",
            "shutdownReason": "",
            "stopDate": "",
            "flowtimeflag": "0",
            # --- 要留下的：数字类 ---
            "accumulateFlow": 690876.003675,
            "accumulateTime": 3355.498325,
            "accumulateMoney": 0,
            "currMonthFlow": 18454.782751,
            "currMonthTime": 104.017223,
            "currMonthMoney": 0,
            "lastMonthFlow": 30000.5,
            "lastMonthTime": 200.25,
            "lastMonthMoney": 0,
            "currAccumulateFlow": 12345.6,
            "remainflow": 0,
            "remaintime": 0,
            "loginCount": 42,
            "webLoginFailTimes": 0,
            "proxyCtrlTimes": 0,
        },
        "bindingMessageVo": {
            "bindMsgVo": [
                {"clearType": "1", "accountMac": "84:9E:56:40:69:1D",
                 "terminalType": "a", "osInfo": "Windows"},
                # 空槽位（accountMac 为空）—— 解析要跳过它
                {"clearType": "2", "accountMac": "", "terminalType": "",
                 "osInfo": ""},
                {"clearType": "3", "accountMac": "AA:BB:CC:DD:EE:FF",
                 "terminalType": "b", "osInfo": "Android"},
            ],
            "bindTerminalVo": [],
        },
        # 🔴 整块都不要（前端模板只用了它 idCardNum / idCardType 两个字段，
        #    恰好都是身份信息）
        "baseMessageVo": {
            "idCardNum": "SENSITIVE_IDCARD",
            "idCardType": "1",
            "accountMobile": "SENSITIVE_PHONE",
            "accountName": "SENSITIVE_NAME",
        },
    },
}

FAKE_FEE = {
    "errcode": "0", "success": True, "total": 1,
    "data": {"sumFixMoney": 0.0, "sumMothMoney": 0.0,
             "sumBusMoney": 0.0, "sumFlowTimeMoney": 0.0},
    "rows": [{
        "accountId": "2025000000",
        "feeType": "月租",
        "internetTacticsName": "校园网电信融合套餐",
        "areaName": "电信",
        "feeMoney": 39.0,
        "feeMonth": "2026-10",
        "feeTime": "2026-10-06 08:00:00",
        # 收费人 —— 是学校的工作人员，不是用户信息（但仍不展示）
        "feeAccount": "王老师",
    }],
}

FAKE_CHARGE = {
    "errcode": "0", "success": True, "total": 1,
    "rows": [{
        "id": "1",
        "accountId": "2025000000",
        "chargeType": "充值",
        "chargeMoney": 50.0,
        "chargeFlag": "已缴费",
        "chargeTime": "2026-10-06 09:00:00",
        "chargeAccount": "王老师",
        "subCode": "SUB123",
        "accNbr": "电信",
        "internetTacticsName": "校园网电信融合套餐",
        "areaName": "电信",
        "ecpId": "ECP1",
        # 🔴 这两个是**第二个**带身份证号的接口 —— 必须丢
        "idCardNum": "SENSITIVE_IDCARD",
        "accountMobile": "SENSITIVE_PHONE",
        "state": "已完成",
        "createTime": "2026-10-06 08:59:00",
        "handleTime": "2026-10-06 09:00:00",
    }],
}

FAKE_BUSPKG = {
    "errcode": "0", "success": True, "total": 1,
    "rows": [{"businessPackageId": "9", "businessPackageName": "30元30M",
              "money": 30.0, "flow": 0}],
}


def test_parse_bill():
    print("\n--- 3c. A 档：上网详单解析（单位是秒 / total≠fetched） ---")
    sessions, summary = S.parse_bill(FAKE_BILL["rows"], FAKE_BILL["data"],
                                     FAKE_BILL["total"])

    check("解析出 1 条会话", len(sessions), 1)
    s = sessions[0]
    check("会话字段集合", sorted(s.keys()),
          ["cause", "device", "down", "end", "fee", "ip", "mac", "os",
           "seconds", "start", "up"])
    check("时长（秒）", s["seconds"], 43208)
    check("上行 MB", s["up"], 973.9739)
    check("下行 MB", s["down"], 2962.2022)
    check("起止时间", (s["start"], s["end"]),
          ("2026-10-11 08:00:00", "2026-10-11 20:00:08"))
    check("断开原因", s["cause"], "用户请求断开")
    check("设备类型", s["device"], "a")
    check("设备 MAC 保留（要区分哪台设备）", s["mac"], "84:9E:56:40:69:1D")

    # 🔴 构造式输出：敏感 / 噪音字段一个都不许出现
    #    ⚠️ 用**字典成员判断**而不是子串搜索 —— `"id"` 这种短词做子串会误判。
    for key in ("id", "accountId", "serverIp", "nasId", "vlanId",
                "broswerType", "areaName", "internetTacticsName"):
        check_true("会话里没有键 %s" % key, key not in s, "")
    check_true("汇总里没有 accountName", "accountName" not in summary, "")
    blob = json.dumps({"s": sessions, "m": summary}, ensure_ascii=False)
    for sent in SENTINELS:
        check_true("输出里没有 %s" % sent, sent not in blob, "")

    # 汇总：来自服务端 data，是**全量**口径
    check("total 是服务端说的总数", summary["total"], 227)
    check("fetched 是真拉到的", summary["fetched"], 1)
    check_true("total ≠ fetched（两者都要留着，别合并）",
               summary["total"] != summary["fetched"], "")
    check("累计上行", summary["up"], 35321.13)
    check("累计下行", summary["down"], 261458.92)
    check("累计费用", summary["fee"], 0.0)
    check("累计时长换算成秒（12h0m）", summary["seconds"], 43200)

    # 🔴 单位陷阱：秒 vs 小时，差 3600 倍且**不会报错**
    check("43208 秒 → 「12 小时 0 分」", S.fmt_duration(43208), "12 小时 0 分")
    check_true("用 fmt_duration(秒) ≠ 拿 fmt_hours 去量秒那种错法",
               S.fmt_duration(3600) != S.fmt_hours(3600), "")

    # 空 / 坏输入不崩
    check("坏输入 → 空会话", S.parse_bill(None)[0], [])
    check("坏输入 → summary 键一个不少",
          sorted(S.parse_bill(None)[1]),
          ["down", "fee", "fetched", "seconds", "total", "up"])
    check("坏输入 → total 是 None（不是 0）", S.parse_bill(None)[1]["total"], None)
    check("rows 是字符串", S.parse_bill("x")[0], [])
    check("rows 里有非 dict", S.parse_bill([None, 1, "x"])[0], [])
    check("total 读不动 → None", S.parse_bill([], None, "abc")[1]["total"], None)


def test_fmt_duration():
    print("\n--- 3c. A 档：时长（秒）换算 ---")
    check("0 秒", S.fmt_duration(0), "0 秒")
    check("59 秒", S.fmt_duration(59), "59 秒")
    check("60 秒 → 1 分 0 秒", S.fmt_duration(60), "1 分 0 秒")
    check("3599 秒", S.fmt_duration(3599), "59 分 59 秒")
    check("3600 秒 → 1 小时 0 分", S.fmt_duration(3600), "1 小时 0 分")
    check("43208 秒", S.fmt_duration(43208), "12 小时 0 分")
    check("脏数据不崩（None）", S.fmt_duration(None), "0 秒")
    check("脏数据不崩（负数）", S.fmt_duration(-5), "0 秒")
    check("脏数据不崩（字符串）", S.fmt_duration("abc"), "0 秒")


def test_parse_stop():
    print("\n--- 3c. A 档：停机状态解析 ---")
    check("pageType=0 → 正常", S.parse_stop({"pageType": "0"})["state"], "正常")
    check("pageType=1 → 停机中", S.parse_stop({"pageType": "1"})["state"], "停机中")
    check("pageType=2 → 复机中", S.parse_stop({"pageType": "2"})["state"], "复机中")

    # 🔴 没见过的值 ⇒ 必须说「未知」，**绝不能兜底成「正常」**
    #    （把「停机了」显示成「正常」是这类功能里最糟的一种错）
    unknown = S.parse_stop({"pageType": "9"})
    check("没见过的 pageType → 未知", unknown["state"], "未知")
    check("未知时原始值要留着（好排查）", unknown["page_type"], "9")
    check_true("没见过的值绝不显示成正常", unknown["state"] != "正常", "")

    check("停机原因", S.parse_stop(
        {"pageType": "1", "shutdownReason": "欠费"})["reason"], "欠费")
    check("坏输入不崩", S.parse_stop(None)["state"], "未知")
    check("坏输入的原始值是空串", S.parse_stop(None)["page_type"], "")


def test_parse_package():
    print("\n--- 3c. A 档：套餐概览解析（🔴 必须丢掉真实姓名） ---")
    p = S.parse_package({"accountState": "正常", "chargeNumber": 0,
                         "remainMoney": 12.5,
                         "accountName": "SENSITIVE_NAME"})
    check("套餐字段集合", sorted(p.keys()),
          ["available_flow", "balance", "state"])
    check("状态", p["state"], "正常")
    check("可用流量", p["available_flow"], 0.0)
    check("余额", p["balance"], 12.5)
    # 🔴 真实姓名必须被丢掉（这个接口**带** accountName）
    check_true("accountName 被丢掉", "accountName" not in p, "")
    check_true("SENSITIVE_NAME 没漏出去",
               "SENSITIVE_NAME" not in json.dumps(p, ensure_ascii=False), "")
    check("坏输入不崩", S.parse_package(None)["state"], "")


def test_clean():
    print("\n--- 3c. A 档：脏值归一化 ---")
    check("None → 空串", S._clean(None), "")
    check("空串 → 空串", S._clean(""), "")
    check('字符串 "(null)" → 空串（学校真这么发）', S._clean("(null)"), "")
    check("'null' → 空串", S._clean("null"), "")
    check("前后空格去掉", S._clean("  abc  "), "abc")
    # ⚠️ "Unknown" **不**归零 —— 那是学校真的写了「未知」，是信息不是缺失
    check('"Unknown" 保留（是信息不是缺失）', S._clean("Unknown"), "Unknown")
    check("0 保留（不是空）", S._clean(0), "0")


def test_bill_pagination():
    print("\n--- 3c. A 档：翻页 + 兜底（🔴 max_pages 防死循环） ---")
    full = {"rows": [REAL_SESSION] * 200, "total": 1000,
            "data": {"onlineHour": 1, "onlineMin": 0}}

    # ① 一页就够（返回 < pageSize）⇒ 只请求一次
    calls = []
    svc = S.SelfService()

    def _post1(path, payload=None):
        calls.append((path, dict(payload)))
        return {"rows": [REAL_SESSION] * 3, "total": 3, "data": {}}

    svc._post = _post1
    out = svc.bill("2025000000")
    check("一页够 → 只请求一次", len(calls), 1)
    check("拿到 3 条", len(out["sessions"]), 3)
    check("fetched=3", out["summary"]["fetched"], 3)

    # ② 服务端**一直**返回满页 ⇒ 必须在 max_pages 处停（不许死循环）
    calls2 = []

    def _post2(path, payload=None):
        calls2.append(dict(payload))
        return full

    svc._post = _post2
    out2 = svc.bill("2025000000", page_size=200, max_pages=3)
    check("一直满页 → 停在 max_pages=3", len(calls2), 3)
    check("3 页 × 200 条", len(out2["sessions"]), 600)
    check("total 仍是服务端说的 1000", out2["summary"]["total"], 1000)
    check("fetched 是真拉到的 600", out2["summary"]["fetched"], 600)

    # ③ 空日期**不发**（前端也是把空字段删掉再发的）
    payloads = []

    def _post3(path, payload=None):
        payloads.append(dict(payload))
        return {"rows": [], "total": 0, "data": {}}

    svc._post = _post3
    svc.bill("2025000000")
    p0 = payloads[0]
    check("空日期不发 startTime", "startTime" in p0, False)
    check("空日期不发 endTime", "endTime" in p0, False)
    check("accountId 发了", p0["accountId"], "2025000000")
    check("分页参数", p0["page"],
          {"pageSize": S.BILL_PAGE_SIZE, "currentPage": 1})

    # ④ 有日期就发
    payloads.clear()
    svc.bill("2025000000", start="2026-01-01", end="2026-10-15")
    p1 = payloads[0]
    check("有日期就发 startTime", p1["startTime"], "2026-01-01")
    check("有日期就发 endTime", p1["endTime"], "2026-10-15")

    # ⑤ bill() 只调 getbilldetail.do
    paths = []

    def _post5(path, payload=None):
        paths.append(path)
        return {"rows": [], "total": 0, "data": {}}

    svc._post = _post5
    svc.bill("2025000000")
    check("bill() 只调一个接口", sorted(set(paths)), [S.EP_BILL])


def test_a_batch2():
    print("\n--- 3d. A 档第二批：详细资料 / 扣费 / 缴费 / 可购套餐 ---")

    # ① 详细资料：白名单逐个字段拼出来的
    r = S.parse_baseinfo(FAKE_BASEINFO["data"])
    check("文本字段数 = 白名单长度", len(r["text"]), len(S._BASEINFO_TEXT))
    check("数字字段数 = 白名单长度", len(r["num"]), len(S._BASEINFO_NUM))
    check("开户日", r["text"]["openingDate"], "2024-09-01")
    check("累计流量", r["num"]["accumulateFlow"], 690876.003675)
    check("登录次数", r["num"]["loginCount"], 42)
    check("代理检测结果", r["text"]["proxyCtrlResult"], "正常")
    check("绑定槽位数（空槽被跳过）", r["bind_count"], 2)

    # ② 🔴 最敏感的一个接口：一个哨兵都不许漏
    blob = json.dumps(r, ensure_ascii=False)
    for s in SENTINELS:
        check_true("详细资料没有 %s" % s, s not in blob, "")
    for key in ("operatorAccount", "operatorPwd", "idCardNum", "idCardType",
                "accountMobile", "accountName", "baseMessageVo"):
        check_true("详细资料没有键 %s" % key, key not in blob, "")

    # ③ 绑定槽位：clear_type 必须是**字符串**（接口要的就是字符串）
    b = r["bind"]
    check("绑定1 clear_type", b[0]["clear_type"], "1")
    check("绑定1 mac", b[0]["mac"], "84:9E:56:40:69:1D")
    check("绑定2 mac（中间那个空槽被跳过）", b[1]["mac"], "AA:BB:CC:DD:EE:FF")
    nb = S.parse_bindings({"bindingMessageVo": {"bindMsgVo": [
        {"clearType": 2, "accountMac": "11:22:33:44:55:66"}]}})
    check("数字 clearType 也转成字符串", nb[0]["clear_type"], "2")

    # ④ 脏值：字段名全对不上 ⇒ 给空，**不许崩**（这一批没实测过，全靠这条兜底）
    junk = S.parse_baseinfo({"authMessageVo": {"whoKnows": 1},
                             "bindingMessageVo": {"bindMsgVo": "不是列表"}})
    check("脏值：文本全空", set(junk["text"].values()), {""})
    check("脏值：数字全 None", set(junk["num"].values()), {None})
    check("脏值：绑定为空", junk["bind"], [])
    check("None 输入不崩", S.parse_baseinfo(None)["bind_count"], 0)
    check("列表输入不崩", S.parse_baseinfo([1, 2])["bind_count"], 0)

    # ⑤ 扣费记录（🔴 四类费用汇总在**这个**接口的 data 里，不在缴费记录那边）
    f = S.parse_fee(FAKE_FEE["rows"], FAKE_FEE["data"], FAKE_FEE["total"])
    check("扣费 1 条", len(f["rows"]), 1)
    check("扣费类型", f["rows"][0]["kind"], "月租")
    check("扣费金额", f["rows"][0]["money"], 39.0)
    check("扣费 total", f["total"], 1)
    check("扣费四类汇总", sorted(f["sum"].keys()),
          ["bus", "fix", "flowtime", "month"])

    # ⑥ 缴费记录 —— 🔴 第二个带身份证号的接口
    c = S.parse_charge(FAKE_CHARGE["rows"], None, FAKE_CHARGE["total"])
    check("缴费 1 条", len(c["rows"]), 1)
    check("缴费金额", c["rows"][0]["money"], 50.0)
    cblob = json.dumps(c, ensure_ascii=False)
    for s in SENTINELS:
        check_true("缴费记录没有 %s" % s, s not in cblob, "")
    for key in ("idCardNum", "accountMobile"):
        check_true("缴费记录没有键 %s" % key, key not in cblob, "")

    # ⑦ 可购套餐：宽松取（真实字段名没实测过，常见几个键都试）
    p = S.parse_buspkg(FAKE_BUSPKG["rows"])
    check("可购套餐 1 条", len(p["rows"]), 1)
    check("套餐名", p["rows"][0]["name"], "30元30M")
    check("套餐 id", p["rows"][0]["id"], "9")
    p2 = S.parse_buspkg([{"label": "40元50M", "value": "10"}])
    check("宽松取 label/value",
          (p2["rows"][0]["name"], p2["rows"][0]["id"]), ("40元50M", "10"))
    check("空列表不崩", S.parse_buspkg(None)["total"], 0)

    # ⑧ 每个方法只打自己那一个接口
    for name, fn, want in (("base_info", lambda v: v.base_info("x"),
                            S.EP_BASEINFO),
                           ("fee_records", lambda v: v.fee_records("x"),
                            S.EP_FEE),
                           ("charge_records", lambda v: v.charge_records("x"),
                            S.EP_CHARGE),
                           ("bus_packages", lambda v: v.bus_packages("x"),
                            S.EP_BUSPKG)):
        svc, paths = make({S.EP_BASEINFO: FAKE_BASEINFO, S.EP_FEE: FAKE_FEE,
                           S.EP_CHARGE: FAKE_CHARGE, S.EP_BUSPKG: FAKE_BUSPKG}), []
        inner = svc._post

        def spy(path, payload=None, _i=inner, _p=paths):
            _p.append(path)
            return _i(path, payload)

        svc._post = spy
        fn(svc)
        check("%s() 只调一个接口" % name, sorted(set(paths)), [want])

    # ⑨ 记录接口的翻页兜底（和 bill 同一套：max_pages 防死循环）
    svc2 = make({S.EP_FEE: FAKE_FEE})
    seen = []
    inner2 = svc2._post

    def spy2(path, payload=None):
        seen.append(payload.get("page", {}).get("currentPage"))
        return inner2(path, payload)

    svc2._post = spy2
    svc2.fee_records("2025000000", page_size=200)
    check("一页够 → 只请求一次", seen, [1])

    full_page = {"rows": [FAKE_FEE["rows"][0]] * 200, "total": 1000, "data": {}}
    got = make({S.EP_FEE: full_page}).fee_records("2025000000", page_size=200,
                                                  max_pages=3)
    check("一直满页 → 停在 max_pages=3", len(got["rows"]), 600)


def test_account_center():
    print("\n--- 3c. A 档：account_center 一次拿全（七个接口） ---")
    svc = make({S.EP_BILL: FAKE_BILL,
                S.EP_STOP: {"data": {"pageType": "0"}},
                S.EP_PACKAGE: {"data": {"accountState": "正常",
                                        "chargeNumber": 0, "remainMoney": 0,
                                        "accountName": "SENSITIVE_NAME"}},
                S.EP_BASEINFO: FAKE_BASEINFO,
                S.EP_FEE: FAKE_FEE,
                S.EP_CHARGE: FAKE_CHARGE,
                S.EP_BUSPKG: FAKE_BUSPKG})
    out = svc.account_center("2025000000")
    check("七个键", sorted(out.keys()),
          ["base", "bill", "buspkg", "charge", "fee", "package", "stop"])
    check("bill 有会话", len(out["bill"]["sessions"]), 1)
    check("stop 状态", out["stop"]["state"], "正常")
    check("package 状态", out["package"]["state"], "正常")
    check("base 绑定槽位", out["base"]["bind_count"], 2)
    check("fee 条数", len(out["fee"]["rows"]), 1)
    check("charge 条数", len(out["charge"]["rows"]), 1)
    check("buspkg 条数", len(out["buspkg"]["rows"]), 1)
    blob = json.dumps(out, ensure_ascii=False)
    for sent in SENTINELS:
        check_true("account_center 输出没有 %s" % sent, sent not in blob, "")

    # 🔴 tolerant=False（默认）：任一失败就整个抛 —— 不许吞
    svc2 = make({S.EP_BILL: FAKE_BILL, S.EP_STOP: {"data": {"pageType": "0"}},
                 S.EP_BASEINFO: FAKE_BASEINFO, S.EP_FEE: FAKE_FEE,
                 S.EP_CHARGE: FAKE_CHARGE, S.EP_BUSPKG: FAKE_BUSPKG},
                raise_on={S.EP_PACKAGE: S.SelfError("other", "套餐接口抽风")})
    try:
        svc2.account_center("2025000000")
        check_true("默认（不容错）应当抛异常", False, "却没抛")
    except S.SelfError as e:
        check("默认模式抛的是原错误", e.msg, "套餐接口抽风")

    # tolerant=True：成功的留下、失败的给 None、原因收在 errors 里
    got = svc2.account_center("2025000000", tolerant=True)
    check("容错模式：bill 还在", len(got["bill"]["sessions"]), 1)
    check("容错模式：stop 还在", got["stop"]["state"], "正常")
    check("容错模式：base 还在", got["base"]["bind_count"], 2)
    check("容错模式：失败的给 None", got["package"], None)
    check("容错模式：原因收在 errors", got["errors"], ["套餐接口抽风"])


def test_full():
    print("\n--- 3c. A 档：full() = load + A 档，且 A 档失败不拖垮主数据 ---")
    base = {S.EP_INDEX: FAKE_INDEX, S.EP_CHART: FAKE_CHART,
            S.EP_ONLINE: FAKE_ONLINE, S.EP_BILL: FAKE_BILL,
            S.EP_STOP: {"data": {"pageType": "0"}},
            S.EP_PACKAGE: {"data": {"accountState": "正常", "chargeNumber": 0,
                                    "remainMoney": 0,
                                    "accountName": "SENSITIVE_NAME"}},
            S.EP_BASEINFO: FAKE_BASEINFO, S.EP_FEE: FAKE_FEE,
            S.EP_CHARGE: FAKE_CHARGE, S.EP_BUSPKG: FAKE_BUSPKG}

    # ① 全好：主数据 + A 档都在，center_error 是空串
    out = make(dict(base)).full("2025000000", today="2026-10-12")
    for k in ("status", "plan", "days", "devices", "bill", "stop", "package",
              "base", "fee", "charge", "buspkg"):
        check_true("full() 里有 %s" % k, k in out, "")
    check("主数据：状态", out["status"], "正常")
    check("A 档：详单条数", len(out["bill"]["sessions"]), 1)
    check("A 档：停机状态", out["stop"]["state"], "正常")
    check("A 档：套餐状态", out["package"]["state"], "正常")
    check("全好时 center_error 是空串", out["center_error"], "")

    # ② 🔴 详单接口挂了：**主数据必须照常**，且 center_error 要说出来
    out2 = make(dict(base),
                raise_on={S.EP_BILL: S.SelfError("other", "详单接口抽风")}
                ).full("2025000000", today="2026-10-12")
    check("详单挂了：主数据照常（状态）", out2["status"], "正常")
    check("详单挂了：主数据照常（天数）", len(out2["days"]), 1)
    check("详单挂了：bill 是 None", out2["bill"], None)
    check("详单挂了：stop 照常拿到", out2["stop"]["state"], "正常")
    check_true("🔴 详单挂了必须说出来（不能假装没有记录）",
               "详单接口抽风" in out2["center_error"], out2["center_error"])

    # ③ 三个全挂 ⇒ 主数据仍在，错误三条都在
    out3 = make(dict(base), raise_on={
        S.EP_BILL: S.SelfError("other", "详单挂"),
        S.EP_STOP: S.SelfError("other", "停机挂"),
        S.EP_PACKAGE: S.SelfError("other", "套餐挂"),
    }).full("2025000000", today="2026-10-12")
    check("三个全挂：主数据仍在", out3["status"], "正常")
    for msg in ("详单挂", "停机挂", "套餐挂"):
        check_true("center_error 里有 %s" % msg, msg in out3["center_error"], "")
    check("三个全挂：bill/stop/package 都是 None",
          (out3["bill"], out3["stop"], out3["package"]), (None, None, None))

    # ④ full() 输出的敏感字段也不能漏（多接口拼起来最容易漏）
    blob = json.dumps(out, ensure_ascii=False)
    for sent in SENTINELS:
        check_true("full() 输出没有 %s" % sent, sent not in blob, "")


# --------------------------------------------------------------- 4. 失败要分类

def test_error_kinds():
    print("\n--- 4. 失败分类（决定「能不能重试」） ---")
    # 验证码错 ⇒ 可以换一张重来
    svc = make(raise_on={S.EP_LOGIN: S.SelfError("other", "验证码错误")})
    try:
        svc.login("2025000000", "pw", "0000")
        check_true("验证码错应当抛异常", False, "却没抛")
    except S.SelfError as e:
        check("验证码错 → kind", e.kind, "captcha")

    # 其它登录失败 ⇒ 🔴 必须停下来，不许重试
    svc = make(raise_on={S.EP_LOGIN: S.SelfError("other", "账号或密码不正确")})
    try:
        svc.login("2025000000", "pw", "0000")
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
#
# 2026-10-15：新增 A 档三个只读接口（全部 `get*`，参数与返回已实测，
#             见 `郑航自助服务-接口清单.md` §3A）。
ALLOWED_EP = ["getbilldetail.do", "getindexchart.do", "getindexinfo.do",
              "getonline.do", "getpackageinformation.do",
              "getshutdownformation.do", "login.do", "tologin.do",
              # 2026-10-16 A 档第二批（参数与字段名从 app.js 逆出，未用真账号实测）
              "getbaseinfo.do", "getfeerecord.do", "getchargerecord.do",
              "getbusinesspackage.do"]


def _is_readonly_ep(name):
    """接口名是不是「只读」形状：`get*` / `login` / `tologin`。

    🔴 这是一条**形状**判据，和上面逐个点名的 `banned` 列表**互补**：
       banned 是「已知的坏东西」，这条是「凡是长得不像读的，一律不接受」。
       两者都要有 —— 只靠 banned，将来有人加个没列进去的写接口就漏了。
    """
    return name.startswith("get") or name in ("login.do", "tologin.do")


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
    check("代码里实际用到的接口集合", found, sorted(ALLOWED_EP))
    check_true("集合里没有多余接口",
               len(found) == len(ALLOWED_EP), "（多一个就说明加了新接口）")

    # ①b 形状判据：凡是不像「读」的接口名，一律不接受（和 banned 互补）
    for ep in found:
        check_true("接口 %s 是只读形状" % ep, _is_readonly_ep(ep),
                   "（不是 get*/login 开头的接口，本模块不许有）")

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
    test_parse_bill()
    test_fmt_duration()
    test_parse_stop()
    test_parse_package()
    test_clean()
    test_bill_pagination()
    test_a_batch2()
    test_account_center()
    test_full()
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
