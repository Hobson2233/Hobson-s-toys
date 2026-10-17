#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""郑航自助服务（/self/）取数层 —— 「账号中心」功能的数据来源。

背景：`http://202.196.169.166/self/` 是郑航的**用户自助管理**系统，而
`202.196.169.166` **就是软件现在用的 portalHost**（认证走 `/webauth.do`）——
同一台服务器上的另一个子系统。所以这不是新增外部依赖。
接口与单位已全部实测，见 `郑航自助服务-接口清单.md`。

**为什么单独一个模块**（而不是塞进 `campus_login.py`）：
  · 它是一个**完整的、能脱离界面单独测的东西**（取码 → 登录 → 取数 → 解析），
    和 tkinter 无关。`campus_login.py` 已经 7000 行，网络层再进去会很难维护。
  · `updater.py` 当初也是这么分出去的。

🔴 四条不可破的规矩：

  1. **只读。** 本模块只调这几个（**全部 `get*` / `tologin` / `login`**）：
     `tologin.do` / `login.do` / `getindexinfo.do` / `getindexchart.do` /
     `getonline.do` / `getbilldetail.do`（上网详单） /
     `getshutdownformation.do`（停机状态） / `getpackageinformation.do`（套餐概览） /
     `getbaseinfo.do`（详细资料） / `getfeerecord.do`（扣费记录） /
     `getchargerecord.do`（缴费记录） / `getbusinesspackage.do`（可购套餐）。
     **绝不加** `kickonline.do`（踢设备下线）/ `clearusermac.do`（解绑 MAC）/
     `selfstopping.do` / `selfrecovery.do`（停机复机）/ `changepassword.do`（改密码）/
     `buybusinesspackage.do`（买套餐）/ 各种 `*pay*`（付钱）/ `getcode.do`（发短信）
     —— **写操作一律去 `self_actions.py`**（那个模块单独评审、单独测试）。
     ⚠️ 配套措施：`online()` **故意不保留** `billingId` / `serverIp`
     （那两个正是 kick 需要的参数）⇒ **结构上就凑不出踢人的请求**。
     ⚠️ 配套措施 2：本文件**没有** `action()` 之类的通用转发口子 ——
        `self_actions.py` 需要发写请求时，自己 new 一个 opener
        （见那个模块的说明），这样「本文件只读」是**结构保证**，不是纪律要求。

  2. 🔴 **构造式输出，不是过滤式。** 接口返回里**真的带**身份证号、手机号、
     真实姓名、以及**两个**接口会返回**明文密码**（实测确认：
     `getbaseinfo.do` 的 `authMessageVo.operatorPwd`、
     `getoperatorsinformation.do` 的 `operatorPwd`）。
     所以本模块的每个返回值都是**逐个字段手写拼出来的**，
     **不是「把响应拷过来再删几个键」** —— 后者一旦学校加字段就会漏。
     ⚠️ 配套措施：`getbaseinfo.do` / `getuserinformation.do` /
     `getoperatorsinformation.do` **根本不调**（那才是身份证号/密码的出处）；
     `getpackageinformation.do` 虽然调了，但 `parse_package()` **明确丢掉**
     `accountName`（真实姓名）。
     ⇒ 白名单必须**逐个接口**列，**不能只防一个**。

  3. 🔴 **不落盘、不写日志。** cookie 用内存 `CookieJar`（不是 `LWPCookieJar`）；
     任何响应内容都不进 log、不写缓存文件。

  4. 🔴 **验证码只能人工输。** 登录要图片验证码（4 位），所以这个功能
     **没法无人值守**，只能交互式。别试图绕过或自动识别。

  5. 🔴 **「本地历史」不在这个模块里。** 每日流量明细要存到本机、下次不登录
     也能看 —— 那部分在 `self_history.py`。本模块**继续保持不碰磁盘**
     （`self_service_test.py` 里有断言钉着「没用到 os / pathlib」）。
     ⚠️ 别为了图省事在这里加 `open()` / `write_json()` —— 那条断言保护的是
     「这个模块不会偷偷把响应落盘」，而响应里**带身份证号和明文密码**。
"""

import base64
import datetime
import hashlib
import http.cookiejar
import json
import urllib.error
import urllib.request

# 郑航门户。和 `campus_login.DEFAULTS["portalHost"]` 同源 ——
# 调用方应当把这个值传进来，别在这里写死第二份。
DEFAULT_BASE = "http://202.196.169.166"

# 🔴 学校服务器会 **403 掉默认的 Python UA**（实测），必须带浏览器的 UA。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 超时 6 秒：和 `campus_login.MANIFEST_TIMEOUT` 同一档。
# ⚠️ 别照抄到下载场景去 —— 对下载是收紧。这里是一次小 JSON 请求，6 秒够。
TIMEOUT = 6.0


def _host_of(base):
    """从 `http://1.2.3.4:80/self` 里取出 `1.2.3.4`（造 cookie 时要）。"""
    s = (base or DEFAULT_BASE).split("//", 1)[-1]
    return s.split("/", 1)[0].split(":", 1)[0]


def _make_cookie(name, value, host):
    """造一条能给 `CookieJar` 用的 cookie。

    ⚠️ `discard=True` + `expires=None`：我们在**还原**一条原本就是
       session cookie 的东西（实测那个 `JSESSIONID-BOSS-0` 没有 Expires）。
       把它写成「有期限」会让 `CookieJar` 对过期与否做出我们并不掌握的判断。
       真正的判断交给**服务端** —— 还原之后必须探一次 `has_session()`。
    """
    return http.cookiejar.Cookie(
        version=0, name=name, value=value,
        port=None, port_specified=False,
        domain=host, domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True,
        secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest=None)

# 只读接口。**这里没有、也不许有** kick / clear / 缴费 / 改密码。
EP_TOLOGIN = "tologin.do"
EP_LOGIN = "login.do"
EP_INDEX = "getindexinfo.do"
EP_CHART = "getindexchart.do"
EP_ONLINE = "getonline.do"

# ---- A 档（2026-10-15 实测，见 `郑航自助服务-接口清单.md` §3A）----
# 全部 `get*` 只读。参数从 `app.js` 逆出、返回全部真实测过。
EP_BILL = "getbilldetail.do"          # ⭐ 上网详单（会话级）
EP_STOP = "getshutdownformation.do"   # 停机 / 复机状态
EP_PACKAGE = "getpackageinformation.do"  # 套餐概览

# ---- A 档第二批（2026-10-16 逆 app.js 得到参数，**字段名全部从模板逆出**）----
# ⚠️ 这一批**没有用真账号实测过**（要登录 = 要人肉读验证码），所以：
#    · 解析全部走「白名单 + 取不到就给空」——字段名对不上只会显示不全，**不会崩**
#    · 界面上要能看出「这次没读到」，别把空当「没有」（红线：把错误说成事实）
EP_BASEINFO = "getbaseinfo.do"        # 详细资料（⚠️ 见 parse_baseinfo 的白名单说明）
EP_FEE = "getfeerecord.do"            # 扣费记录
EP_CHARGE = "getchargerecord.do"      # 缴费记录（⚠️ 带身份证号 / 联系电话）
EP_BUSPKG = "getbusinesspackage.do"   # 可购套餐（**只列，不买**；买是 C 档，只放链接）

# 🔴 服务端**不限制** `pageSize`：实测 `pageSize=200` 直接返回 200 条
#    （前端只给 5/10 两档，但接口本身接受更大的值）。
BILL_PAGE_SIZE = 200
# 兜底：最多翻这么多页。防的是「服务端 total 变了 / 一直返回满页」导致死循环 ——
# 200 × 10 = 2000 条，半年 227 条远远用不到。
BILL_MAX_PAGES = 10

# 登录失败时，errmsg 里出现这些词 = 验证码的问题（可以换一张重试）；
# 否则**一律当作「不要再重试」**（可能是密码错 / 账号不存在 / 被锁）。
# 🔴 这个区分很重要：密码连错可能触发学校风控，不能盲目重试。
_CAPTCHA_HINTS = ("验证码", "校验码", "验证码错误", "图片验证码")


class SelfError(Exception):
    """取数失败。`kind` 决定调用方该怎么办：

    · `"net"`     网络不通 / 超时 → 提示「网络不通」，可稍后重试
    · `"session"` 会话过期（errcode -1）→ **重新走验证码流程**
    · `"captcha"` 验证码不对 → **换一张重来**（这一种是安全的重试）
    · `"login"`   其它登录失败 → 🔴 **不要再重试**（可能是密码错/被锁）
    · `"other"`   其它 → 提示原文
    """

    def __init__(self, kind, msg):
        Exception.__init__(self, msg)
        self.kind = kind
        self.msg = msg


def js_md5(s):
    """复刻前端的密码处理 `c(e) = w(u(v(e), e.length*8))`。

    MD5 本身是**标准**的（`u()` / `w()` 就是教科书实现，小写十六进制、**无盐**），
    但**输入不是 UTF-8**：`v()` 做的是 `charCodeAt(r/l) & 255`，
    也就是「**每个 UTF-16 码元取低 8 位**」。
    纯 ASCII 时这和 UTF-8 等价，**中文密码不等价** —— 所以不能图省事写
    `md5(s.encode("utf-8"))`。

    已实测：用这个函数登录**成功**（`errcode == "0"`）。
    """
    data = bytes(b & 0xFF for b in s.encode("utf-16-le")[::2])
    return hashlib.md5(data).hexdigest()


def _num(v, default=0.0):
    """把接口返回的数字安全地转成 float。转不了就给默认值（不抛）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _num_or_none(v):
    """转 float；转不了给 `None`。

    🔴 **和 `_num` 不是一回事，别互相替代**：`_num` 的 `0.0` 意思是
    「这个数就是 0」（比如今天确实没流量）；这里的 `None` 意思是
    「**接口没给这个数**」（比如今天的上行/下行，服务端真的不提供）。
    把后者写成 0，界面上就会显示「上行 0 MB」——那是**编出来的**。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_flow(mb):
    """流量（**MB**）→ 给人看的字符串。

    换算规则照学校网页（`app.js` 的 `flows` 过滤器）：< 1024 显示 MB，否则 GB。

    ⚠️ **我们和学校那个过滤器有两处不同，都是故意的**：
      ① 学校写的是 `e < 1024 ? MB : e > 1024 ? GB : void 0`
         —— **`e === 1024` 时返回 `undefined`**，是他们的 bug。这里用 `>=`。
      ② 学校没有 TB 档（`e/10240` 那个分支也不对）。这里按 1024 进制补上。
    单位「MB」本身是实测确认的：图表 yAxis 标注 `(单位/MB)`，
    且「图表 10 月各天之和 + 今日」== `sameMonthFlow`，**分毫不差**。
    """
    v = _num(mb)
    if v < 1024:
        return "%.2f MB" % v
    if v < 1024 * 1024:
        return "%.2f GB" % (v / 1024.0)
    return "%.2f TB" % (v / 1024.0 / 1024.0)


def fmt_flow_short(mb):
    """流量（MB）→ **紧凑**字符串，给图表柱顶的数值标签用。

    ⚠️ 和 `fmt_flow` 的区别只是「短」：`fmt_flow(3936.18)` → `"3.84 GB"`，
    `fmt_flow_short(3936.18)` → `"3.84G"`。柱顶标签在窄柱子上放不下
    「12.34 GB」这种带空格的长串（一个月 31 根柱子时每根只有十几像素宽），
    所以另开一个短格式 —— **不是**要取代 `fmt_flow`，正文里还是用长的。
    """
    v = _num(mb)
    if v < 1024:
        return "%.0fM" % v
    if v < 1024 * 1024:
        return "%.2fG" % (v / 1024.0)
    return "%.2fT" % (v / 1024.0 / 1024.0)


def fmt_hours(h):
    """时长（**小时**）→ 给人看的字符串。单位实测确认（图表 yAxis `(单位/小时)`）。

    ⚠️ 学校自己的日时长会出现 **> 24 的值**（实测 `allUseTime` 里有 `"42"`），
    那是他们统计口径的问题。**我们照原样显示、不做"修正"** ——
    用户会拿我们的界面和学校网页对照，我们自己改了反而像我们算错。
    """
    return "%.2f 小时" % _num(h)


def fmt_duration(sec):
    """时长（**秒**）→ 给人看的字符串。**和 `fmt_hours` 不是一回事，别混用。**

    🔴 为什么另开一个：`getindexchart.do` 的时长单位是**小时**，
       而 `getbilldetail.do`（上网详单）的 `chargingDuration` 单位是**秒**
       （实测 43208 == `endDate − startDate`，**分毫不差**）。
       两个接口单位不同，拿错就会差 3600 倍 —— 而那种错**不会报错**，
       只会让界面上的数字离谱。

    输出规则：< 60 秒 → 「N 秒」；< 1 小时 → 「N 分 N 秒」；否则「N 小时 N 分」。
    """
    s = int(_num(sec))
    if s < 0:
        s = 0
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return "%d 分 %d 秒" % (s // 60, s % 60)
    h, m = divmod(s // 60, 60)
    return "%d 小时 %d 分" % (h, m)


def _looks_like_captcha(msg):
    """errmsg 是不是在说「验证码不对」。"""
    return any(h in (msg or "") for h in _CAPTCHA_HINTS)


# ---------------------------------------------------------------- 每日明细
#
# 这一段是纯函数（**不联网**），因为它要处理的全是「服务端给的数据长什么样」
# 这类**容易出错又不好复现**的问题：数组不等长、日期不连续、今天不在里面……

def _empty_summary():
    """空汇总。**两条返回路径共用一份** —— 形状必须完全一致。

    ⚠️ 2026-10-12 被自己的测试抓出来的：第一版「坏输入」那条路返回的是
    `{"total": {}, "month": {}, ...}`，而正常路径返回的是
    `{"total": {"flow": 0.0, "hours": None}, ...}` —— **同一个函数两种形状**。
    调用方就得同时处理「有键但值是 0」和「键根本没有」，迟早漏一个，
    而漏的那次表现是 `KeyError` 崩在界面上、或者更糟：`{}` 被当成「没数据」
    而其实是「有数据但都是 0」。
    """
    return {
        "today": None,
        "total": {"flow": 0.0, "hours": None},
        "month": {"flow": 0.0, "hours": None},
        "limit": {"flow": "", "hours": ""},
    }


def parse_days(chart, today=None):
    """把 `getindexchart.do` 的返回解析成**按日期的明细**。**纯函数，不联网。**

    返回 `(days, summary)`：

      · `days`    —— 按日期升序的列表，每条是
        `{"date": "YYYY-MM-DD", "flow": MB, "up": MB|None,
          "down": MB|None, "hours": 小时|None}`
      · `summary` —— `{"today": 一条|None, "total": {...}, "month": {...},
                       "limit": {...}}`

    `today` 是「今天算哪一天」（`YYYY-MM-DD`）。**不传就取本机当天** ——
    测试里**必须显式传**，否则跑出来的结果依赖运行日期，今天绿明天红。

    🔴 字段与单位的实测结论（2026-10-12，见 `郑航自助服务-接口清单.md`）：

      · `baseChartVo` 里 `dates` / `allFlows` / `uploadFlows` / `downloadFlows` /
        `allUseTime` 是**五个等长的平行数组**（实测各 24 条）。
        单位：MB / MB / MB / 小时。
      · 🔴 实测 `uploadFlows[i] + downloadFlows[i] == allFlows[i]`，**分毫不差**
        （24 天逐条验过）⇒ 上下行和合计是同一口径，可以放心一起显示。

    ⚠️ 三个**不能想当然**的地方（都是实测出来的）：

      1. **`dates` 不连续。** 服务端只给「有记录的日子」：实测 2026-09-30
         直接跳到 2026-10-07（国庆那几天没有记录）。
         ⇒ **绝不能假设「连续 N 天」**，一切都按日期做键。
         ⚠️ 也别把缺失的日子补成 0 —— 那是**猜**。补 0 意味着「那天确实用了 0」，
         而真实原因可能是「服务端没这条记录」，两者在界面上必须能区分。
      2. **今天不在 `dates` 里。** 实测 2026-10-12 晚上抓的包，最后一天是 10-11。
         今天的数只能从 `baseUseVo.sameDayFlow` / `sameDayTime` 拿。
      3. 🔴 **今天的上行/下行服务端不给**（`baseUseVo` 里只有 `sameDayFlow`
         这个合计）⇒ 存 `None`。这是 `_num_or_none` 存在的唯一理由。
         ⚠️ 等明天再同步时，今天变成「过去的一天」，就会从 `dates` 里拿到真的
         上下行 —— 所以**明天那次同步会把这天的 up/down 补上**（覆盖规则天然处理）。

    ⚠️ 坏数据一律**不抛异常**，能取多少取多少：数组不等长时按最短的截断，
    单个数转不动就那一个字段给 `None`。理由 —— 这是**给人看的历史数据**，
    为了一条脏记录把整个界面搞崩（还看不出原因）是最差的处理方式。
    """
    if not isinstance(chart, dict):
        return [], _empty_summary()
    bc = chart.get("baseChartVo")
    use = chart.get("baseUseVo")
    bc = bc if isinstance(bc, dict) else {}
    use = use if isinstance(use, dict) else {}

    def _arr(key):
        v = bc.get(key)
        return v if isinstance(v, list) else []

    dates, flows = _arr("dates"), _arr("allFlows")
    ups, downs = _arr("uploadFlows"), _arr("downloadFlows")
    hours = _arr("allUseTime")

    # 平行数组**按最短的截断** —— 不等长说明服务端那边出了状况，
    # 硬按下标取会 IndexError，或者更糟：把 A 天的流量记到 B 天上。
    n = min(len(dates), len(flows), len(ups), len(downs), len(hours))

    days = []
    for i in range(n):
        d = str(dates[i] or "").strip()
        if not d:
            continue          # 日期都读不出来，这条没法用（也没法当键）
        days.append({
            "date": d,
            "flow": _num(flows[i], 0.0),
            "up": _num_or_none(ups[i]),
            "down": _num_or_none(downs[i]),
            "hours": _num_or_none(hours[i]),
        })

    # 按日期升序 —— 服务端目前是升序的，但**别依赖它**（顺序变了我们的
    # 图表就会乱，而且不报错）。同一天出现两次时保留最后一条（后写的赢）。
    merged = {}
    for d in days:
        merged[d["date"]] = d
    days = [merged[k] for k in sorted(merged)]

    # 今天：只有合计和时长，上下行是 None（见上面第 3 条）
    #
    # ⚠️ 变量名**故意不叫 `today`**（2026-10-13 踩过）：
    #    原来写的是 `today = None`，再把那条记录赋给同一个名字 ——
    #    于是**参数 `today` 先被自己清成了 None**，`today or 取当天` 永远走
    #    「取当天」那一支，传进来的日期完全不起作用。
    #    更阴的是：测试里传 `today="2026-10-12"`、那天**恰好**就是 10-12，
    #    所以断言一直是绿的 —— 过了午夜才红。典型的「假绿」。
    #    ⇒ 换个名字，从根上不可能再覆盖参数。
    today_rec = None
    if "sameDayFlow" in use:
        today_rec = {
            "date": today or datetime.date.today().isoformat(),
            "flow": _num(use.get("sameDayFlow"), 0.0),
            "up": None,
            "down": None,
            "hours": _num_or_none(use.get("sameDayTime")),
        }
    return days, {
        "today": today_rec,
        "total": {
            "flow": _num(use.get("cumulativeFlow"), 0.0),
            "hours": _num_or_none(use.get("cumulativeTime")),
        },
        "month": {
            "flow": _num(use.get("sameMonthFlow"), 0.0),
            "hours": _num_or_none(use.get("sameMonthTime")),
        },
        "limit": {
            "flow": use.get("limtFlows") or "",
            "hours": use.get("limtTimes") or "",
        },
    }


# ---------------------------------------------------------------- A 档解析
#
# 下面几个也是**纯函数**（不联网）。分开写的理由和 `parse_days` 一样：
# 它们处理的全是「服务端返回长什么样」这类**容易错又不好复现**的问题。

def _clean(v):
    """把服务端那些「等于没有值」的写法统一成空串。

    实测见过：JSON `null` / `""` / **字符串** `"(null)"`（学校真的这么发）。
    ⚠️ 但 `"Unknown"` **不**归零 —— 那是学校真的写了「未知」，是信息不是缺失。
    """
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("", "(null)", "null", "NULL"):
        return ""
    return s


def parse_bill(rows, data=None, total=None):
    """把 `getbilldetail.do`（上网详单）解析成**会话列表 + 汇总**。**纯函数。**

    返回 `(sessions, summary)`：

      · `sessions` —— 每次上网一条：
        `{"start", "end", "seconds", "up", "down", "fee", "cause",
          "device", "os", "mac", "ip"}`
      · `summary` —— `{"total", "fetched", "up", "down", "fee", "seconds"}`

    🔴 **构造式输出**（模块规矩 2）：每个字段手写拼出来，**不是**把响应拷过来再删键。
    丢弃的字段：`id`（页内序号，不是主键）、`accountId`、`serverIp`、`nasId`、
    `vlanId`、`broswerType`、`areaName`、`internetTacticsName`（后两个对本账号是常量，
    概览里已经有了，放在 227 行的列表里纯属噪音）。

    ⚠️ 保留 `mac` / `ip` 是**有意的**：界面上要能区分「哪台设备」，
       而 `online()`（当前在线设备）也是这么做的，两处保持一致。
       `ip` 每次上网都会变，本来只是辅助。

    🔴 单位（实测确认，**两个接口不一样，别混**）：
       · `chargingDuration` = **秒**（实测 43208 == `endDate − startDate`）
       · `uploadFlows` / `downloadFlows` = **MB**
       · `fee` = 元
    """
    sessions = []
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            sessions.append({
                "start": _clean(r.get("startDate")),
                "end": _clean(r.get("endDate")),
                "seconds": int(_num(r.get("chargingDuration"))),
                "up": _num(r.get("uploadFlows")),
                "down": _num(r.get("downloadFlows")),
                "fee": _num(r.get("fee")),
                "cause": _clean(r.get("terminateCause")),
                "device": _clean(r.get("terminalType")),
                "os": _clean(r.get("osInfo")),
                "mac": _clean(r.get("accountMac")),
                "ip": _clean(r.get("accountIp")),
            })

    d = data if isinstance(data, dict) else {}
    # ⚠️ `total` 是**服务端说的总数**，`fetched` 是**我们真拉到的**。
    #    翻页被 `max_pages` 截断时两者会不一样 —— 这种时候必须让调用方看得出来，
    #    而不是默默少给几条。所以两个都留着，别合并。
    try:
        tot = int(total)
    except (TypeError, ValueError):
        tot = None
    summary = {
        "total": tot,
        "fetched": len(sessions),
        # 汇总来自服务端 `data`（是**全量**口径，不是当前这一页）
        "up": _num(d.get("allUpFlows")),
        "down": _num(d.get("allDownFlows")),
        "fee": _num(d.get("allFees")),
        # onlineHour / onlineMin 是**两个字段拼一个时长**，换算成秒好统一处理
        "seconds": int(_num(d.get("onlineHour")) * 3600
                       + _num(d.get("onlineMin")) * 60),
    }
    return sessions, summary


# `pageType` 的含义从 `app.js` 逆出（前端 `if("1"==pageType)` / `if("2"==pageType)`）。
# ⚠️ 实测本账号是 `"0"`。**没见过的值不要猜** —— 见 `parse_stop`。
_STOP_STATES = {"0": "正常", "1": "停机中", "2": "复机中"}


def parse_stop(data):
    """把 `getshutdownformation.do` 解析成停机状态。**纯函数。**

    ⚠️ `pageType` 遇到**没见过的值**时：`state` 给「未知」，但把原始值留在
    `page_type` 里。**不要**往「正常」上兜底 —— 那会把「停机了」显示成「正常」，
    是这类功能里最糟的一种错。
    """
    d = data if isinstance(data, dict) else {}
    pt = _clean(d.get("pageType"))
    return {
        "page_type": pt,
        "state": _STOP_STATES.get(pt, "未知"),
        "reason": _clean(d.get("shutdownReason")),
        "oper": _clean(d.get("stopOper")),
        "date": _clean(d.get("stopDate")),
    }


def parse_package(data):
    """把 `getpackageinformation.do` 解析成套餐概览。**纯函数。**

    🔴 **故意丢掉 `accountName`** —— 这个接口带**真实姓名**，而我们不需要。
       （模块规矩 2：白名单要**逐个接口**列，不能只防 `getbaseinfo.do` 一个。）
    """
    d = data if isinstance(data, dict) else {}
    return {
        "state": _clean(d.get("accountState")),
        # chargeNumber = 可用流量（实测本账号 0，因为套餐不限量）
        "available_flow": _num(d.get("chargeNumber")),
        "balance": _num(d.get("remainMoney")),
    }


# ============ A 档第二批：详细资料 / 扣费 / 缴费 / 可购套餐（2026-10-16）============
#
# 🔴 **这一批的字段名全部是从 `app.js` 的模板里逆出来的**，**没有用真账号实测**
#    （登录要人肉读验证码）。所以解析一律走「白名单 + 取不到就给空」：
#    字段名万一对不上，结果是**显示不全**，不会崩、也不会串位。
#    ⚠️ 但界面上必须能看出「这次没读到」，**不能把空显示成「没有」**。
#
# 🔴 `getbaseinfo.do` 是**全项目最敏感的一个接口**（接口清单 §5）：
#    实测返回里带 **身份证号 / 手机号 / 真实姓名 / 运营商明文密码**。
#    所以这里是**逐个字段手写拼**，不是「拷过来再删几个键」——
#    学校一加字段，黑名单就漏，白名单不会。

# `authMessageVo` 实测有 32 个字段（从 app.js 逆出）。这里只留要用的。
# 🔴 **明确排除**（都在同一个对象里，**别顺手加回来**）：
#      · `operatorAccount` —— 运营商账号
#      · `operatorPwd`     —— 🔴 **运营商明文密码**（实测确认存在）
# 🔴 另一个块 `baseMessageVo`（24 字段）**整块不要** ——
#    前端模板只用了它两个字段，而那俩恰好是 `idCardNum` / `idCardType`，
#    整块都是个人身份信息。**一个字段都不取。**
_BASEINFO_TEXT = (
    # --- 日期 ---
    "openingDate",            # 开户日
    "effectiveDate",          # 生效日
    "expirationDate",         # 到期日
    "firstUsetime",           # 首次上网
    "latestEditDate",         # 最后修改
    "lastmonthlyChargetime",  # 上次月结
    # --- 认证 ---
    "authResult",             # 认证结果
    "operationAuthResult",
    "operationAuthTime",
    # --- 套餐变更 ---
    "groupChangeDate",
    "groupChangeFlag",
    # --- 🔴 代理检测：学校就是靠它判断「共享行为」的（门户须知第 2 条）---
    "proxyCtrlResult",
    "proxyCtrlStopTime",
    # --- 停机 ---
    "shutdownReason",
    "stopDate",
    # --- 计费开关 ---
    "flowtimeflag",
)

# 数字类。⚠️ 用 `_num_or_none` 不是 `_num` —— 「接口没给这个数」和
#    「这个数是 0」是两回事（见 `_num_or_none` 的说明）。
_BASEINFO_NUM = (
    "accumulateFlow", "accumulateTime", "accumulateMoney",       # 累计
    "currMonthFlow", "currMonthTime", "currMonthMoney",          # 本月
    "lastMonthFlow", "lastMonthTime", "lastMonthMoney",          # 上月
    "currAccumulateFlow",                                        # 本期累计
    "remainflow", "remaintime",                                  # 剩余
    "loginCount", "webLoginFailTimes", "proxyCtrlTimes",         # 计数
)


def parse_baseinfo(data):
    """把 `getbaseinfo.do` 的 `data` 解析成「详细资料」。**纯函数。**

    返回 `{"text": {...}, "num": {...}, "bind": [...], "bind_count": N}`。
    `text` / `num` 的键就是上面的白名单；取不到的值分别是 `""` / `None`。

    ⚠️ `bind` 是 MAC 绑定槽位，**`clear_type` 是 B 档解绑要用的参数**
       （`clearusermac.do` 的 `clearType` 直接取它，见 `self_actions.py`）。
    """
    d = data if isinstance(data, dict) else {}
    auth = d.get("authMessageVo")
    auth = auth if isinstance(auth, dict) else {}

    text = {k: _clean(auth.get(k)) for k in _BASEINFO_TEXT}
    num = {k: _num_or_none(auth.get(k)) for k in _BASEINFO_NUM}

    bind = parse_bindings(d)
    return {"text": text, "num": num, "bind": bind, "bind_count": len(bind)}


def parse_bindings(data):
    """MAC 绑定槽位。**纯函数。** 只取解绑需要的四个字段。

    实测结构：`bindingMessageVo.bindMsgVo` 是 6 个槽位的数组，
    每个槽有 `clearType`（`"1"`~`"6"` 字符串）/ `accountMac` / `terminalType` / `osInfo`。

    ⚠️ **前端只把 `accountMac` 非空的槽推进列表**（`if (null!=e.accountMac …) r.push(e)`）
       —— 所以「6 个槽位」里通常只有 1~2 个真的绑了设备。这里照做。
    ⚠️ `clearType` 可能是数字 `1` 或字符串 `"1"`，统一转成字符串再发给接口。
    """
    d = data if isinstance(data, dict) else {}
    bm = d.get("bindingMessageVo")
    bm = bm if isinstance(bm, dict) else {}
    rows = bm.get("bindMsgVo")
    rows = rows if isinstance(rows, list) else []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        mac = _clean(r.get("accountMac"))
        if not mac:                     # 空槽位不推（和前端一致）
            continue
        out.append({
            "clear_type": _clean(r.get("clearType")),
            "mac": mac,
            "kind": _clean(r.get("terminalType")),
            "os": _clean(r.get("osInfo")),
        })
    return out


def parse_fee(rows, data=None, total=None):
    """把 `getfeerecord.do` 解析成**扣费记录**。**纯函数。**

    表格列（从 `app.js` 模板逆出）：
      用户账号 / 扣费类型 / 上网套餐 / 网点名称 / 扣除费用(元) / 扣费月份 / 扣费时间 / 收费人

    ⚠️ 这一组**不含隐私字段**（`feeAccount` 是「收费人」，是学校的工作人员，
       不是用户自己的信息）。

    ⚠️ `data` 是**四类费用汇总**（固定费 / 月租 / 业务费 / 流量时长费）。
       实测本账号全是 0（融合套餐不单独计费），但**别的账号可能非 0**。
    """
    out = []
    for r in (rows if isinstance(rows, list) else []):
        if not isinstance(r, dict):
            continue
        out.append({
            "kind": _clean(r.get("feeType")),          # 扣费类型
            "plan": _clean(r.get("internetTacticsName")),  # 上网套餐
            "area": _clean(r.get("areaName")),         # 网点名称
            "money": _num_or_none(r.get("feeMoney")),  # 扣除费用（元）
            "month": _clean(r.get("feeMonth")),        # 扣费月份
            "time": _clean(r.get("feeTime")),          # 扣费时间
        })
    d = data if isinstance(data, dict) else {}
    return {
        "rows": out,
        "total": int(_num(total)),
        "sum": {
            "fix": _num_or_none(d.get("sumFixMoney")),        # 固定费
            "month": _num_or_none(d.get("sumMothMoney")),     # 月租（学校拼错了，是 Moth）
            "bus": _num_or_none(d.get("sumBusMoney")),        # 业务费
            "flowtime": _num_or_none(d.get("sumFlowTimeMoney")),  # 流量时长费
        },
    }


def parse_charge(rows, data=None, total=None):
    """把 `getchargerecord.do` 解析成**缴费（充值）记录**。**纯函数。**

    表格列（从 `app.js` 模板逆出）：
      ID / 用户账号 / 缴费类型 / 缴费金额(元) / 缴费标志 / 收费时间 / 收费人 /
      预约码 / 办理运营商 / 办理套餐 / 办理网点 / 套餐编码 / **身份证号** / **联系电话** /
      订单状态 / 生成时间 / 处理时间

    🔴 **明确丢掉 `idCardNum`（身份证号）和 `accountMobile`（联系电话）** ——
       这两列学校自己的网页上是**明文显示**的，但我们不需要，就不取。
       （模块规矩 2：白名单逐个接口列。这是**第二个**带身份证号的接口。）

    ⚠️ 这个接口的 `data` 实测是 `null`（四类费用汇总在 `getfeerecord.do` 那边，
       别放错地方 —— 第一版就放错了，被测试抓出来）。
    """
    out = []
    for r in (rows if isinstance(rows, list) else []):
        if not isinstance(r, dict):
            continue
        out.append({
            "kind": _clean(r.get("chargeType")),           # 缴费类型
            "money": _num_or_none(r.get("chargeMoney")),   # 缴费金额（元）
            "flag": _clean(r.get("chargeFlag")),           # 缴费标志
            "time": _clean(r.get("chargeTime")),           # 收费时间
            "sub_code": _clean(r.get("subCode")),          # 预约码
            "carrier": _clean(r.get("accNbr")),            # 办理运营商
            "plan": _clean(r.get("internetTacticsName")),  # 办理套餐
            "area": _clean(r.get("areaName")),             # 办理网点
            "state": _clean(r.get("state")),               # 订单状态
            "created": _clean(r.get("createTime")),        # 生成时间
            "handled": _clean(r.get("handleTime")),        # 处理时间
        })
    return {"rows": out, "total": int(_num(total))}


def parse_buspkg(rows):
    """把 `getbusinesspackage.do` 解析成**可购套餐列表**。**纯函数。**

    ⚠️ **只用于「看有哪些套餐」，不用于下单。** 下单是 `buybusinesspackage.do`，
       属于 C 档（涉及钱）—— 我们**只放跳转链接**，不在软件里代办。

    结构：前端直接 `e.options = t.rows` 喂给下拉框，所以字段名以
    `label` / `value` / `name` 之类为主，实测本账号 `rows` 是**空的**
    （融合套餐由电信侧办理，不能在自助服务里买）。
    ⇒ 这里**宽松取**：常见的几个键都试一遍，取不到就给空。
    """
    out = []
    for r in (rows if isinstance(rows, list) else []):
        if not isinstance(r, dict):
            continue
        name = ""
        for k in ("businessPackageName", "packageName", "name", "label", "internetTacticsName"):
            name = _clean(r.get(k))
            if name:
                break
        pid = ""
        for k in ("businessPackageId", "packageId", "id", "value"):
            pid = _clean(r.get(k))
            if pid:
                break
        out.append({
            "name": name,
            "id": pid,
            "money": _num_or_none(r.get("money") or r.get("price") or r.get("feeMoney")),
            "flow": _num_or_none(r.get("flow") or r.get("chargeNumber")),
        })
    return {"rows": out, "total": len(out)}


class SelfService(object):
    """一个会话（内存里的 cookie + 几个只读请求）。**不碰磁盘。**"""
    def __init__(self, base=DEFAULT_BASE, timeout=TIMEOUT):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        # 🔴 内存 CookieJar（不是 LWPCookieJar）—— 会话 cookie 等价于一次登录态，
        #    落盘等于多存一份凭据。程序关掉就没了，这是**故意**的。
        self._jar = http.cookiejar.CookieJar()
        self._op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self.logged_in = False

    # ------------------------------------------------------------ 底层请求

    def _post(self, path, payload=None):
        """POST 到 `/self/<path>`，返回解析好的 JSON dict。

        `payload=None` 表示不带 body（`tologin.do` 就是这样）。
        """
        url = "%s/self/%s" % (self.base, path)
        req = urllib.request.Request(url, method="POST")
        req.add_header("User-Agent", UA)
        req.add_header("Referer", "%s/self/index.html" % self.base)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("X-Requested-With", "XMLHttpRequest")

        if payload is None:
            data = None
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req.add_header("Content-Type", "application/json;charset=UTF-8")

        try:
            with self._op.open(req, data, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise SelfError("net", "服务器返回 HTTP %s" % e.code)
        except Exception as e:
            # ⚠️ 这里**故意只报异常类型**，不把 URL / 参数写进去 ——
            #    密码虽然已经是 MD5，但没理由让它出现在任何消息里。
            raise SelfError("net", "连不上自助服务（%s）" % type(e).__name__)

        try:
            j = json.loads(raw)
        except ValueError:
            raise SelfError("other", "服务器返回的不是 JSON")
        if not isinstance(j, dict):
            raise SelfError("other", "服务器返回的格式不认识")

        ec = str(j.get("errcode"))
        if ec == "0":
            return j
        if ec == "-1":
            # 未登录 / 会话过期 —— 这是**正常**的一种状态，不是故障
            raise SelfError("session", j.get("errmsg") or "请重新登录")
        raise SelfError("other", j.get("errmsg") or ("errcode=%s" % ec))

    # ------------------------------------------------------------ 登录

    def fetch_captcha(self):
        """取一张验证码。返回 `(png_bytes, already_logged_in)`。

        · 会话还有效 ⇒ `(None, True)`，不用输验证码
        · 否则 ⇒ `(PNG 字节, False)`

        ⚠️ **每次都返回一张新的**，且会过期 ⇒ 取码和用码要连着做，
        别提前取一张放着等用户。
        """
        j = self._post(EP_TOLOGIN, None)
        d = j.get("data") or {}
        if d.get("user"):
            self.logged_in = True
            return None, True
        b64 = d.get("verifyCode") or ""
        if not b64:
            raise SelfError("other", "服务器没有返回验证码图片")
        try:
            return base64.b64decode(b64), False
        except Exception:
            raise SelfError("other", "验证码图片解不开")

    def login(self, account, password, code):
        """登录。成功返回 True；失败抛 `SelfError`（kind 见类文档）。

        🔴 **不打印、不记录**任何账号密码相关的东西 —— 失败信息只用服务器的原文。
        """
        try:
            self._post(EP_LOGIN, {"accountId": account,
                                  "password": js_md5(password),
                                  "verifyCode": code})
        except SelfError as e:
            if e.kind == "other" and _looks_like_captcha(e.msg):
                raise SelfError("captcha", e.msg)
            if e.kind == "other":
                # 不是验证码的错 ⇒ 归成 "login"，调用方**必须停下来不要重试**
                raise SelfError("login", e.msg)
            raise
        self.logged_in = True
        return True

    def has_session(self, account):
        """只试一下会话还在不在。返回 True / False。

        网络不通**照样抛**（那是"不知道"，不是"没有"）——
        把"不知道"当成"没有"会让界面莫名其妙地弹验证码。
        """
        try:
            self._post(EP_INDEX, {"accountId": account})
            return True
        except SelfError as e:
            if e.kind == "session":
                return False
            raise

    # ------------------------------------------------------------ 会话搬运

    def export_cookies(self):
        """把当前会话的 cookie 交出来（**明文值**，调用方负责加密存储）。

        ⚠️ 别写死 `JSESSIONID-BOSS-0` 这个名字 —— 实测它就带了个 `-BOSS-0`
           后缀，学校换个实现整个名字都会变。这里按「现在 jar 里有什么」给。
        ⚠️ 本方法**不碰磁盘**：存不存、存哪、怎么加密，是调用方的事。
        """
        return [(c.name, c.value) for c in self._jar]

    def import_cookies(self, pairs):
        """把一份 cookie 塞回这个会话。返回塞进去几条（0 = 没塞进去）。

        🔴 只在**确认还没有会话**时调（`has_session()` 探过是 False）。
           不然可能拿一份不新鲜的 cookie 盖掉正在用的那个。
        🔴 塞进去**不等于**能用：调用方必须再探一次 `has_session()` ——
           cookie 会过期，而 `CookieJar` 只管「带不带」，不管「服务端认不认」。
        """
        host = _host_of(self.base)
        n = 0
        for item in (pairs or []):
            # 🔴 **必须先判形状再取下标**。只写 `item[0] / item[1]` 的话，
            #    一个坏掉的字符串（比如 `"zzz"`）会被当成 `("z", "z")`
            #    塞进 jar —— 不报错，只表现为「载入了 1 条没用的 cookie」。
            #    实测踩到过（2026-10-17）。
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            name, value = item
            if not name or not value:
                continue
            try:
                self._jar.set_cookie(_make_cookie(str(name), str(value), host))
                n += 1
            except Exception:
                continue
        return n

    # ------------------------------------------------------------ 取数

    def _fetch(self, account):
        """取两个接口的原始返回：`getindexinfo.do` + `getindexchart.do`。

        ⚠️ 抽出来是为了让 `overview()` 和每日明细**共用同一次请求** ——
           两次分别请求的话，两个返回可能落在不同秒上，界面上就会出现
           「汇总和明细对不上」，而且**不报错**（这种最难查）。
        """
        info = self._post(EP_INDEX, {"accountId": account}).get("data")
        chart = self._post(EP_CHART, {"accountId": account})
        return (info if isinstance(info, dict) else {},
                chart if isinstance(chart, dict) else {})

    def overview(self, account):
        """状态 + 用量汇总（不含每日明细）。

        只调两个接口：`getindexinfo.do`（状态/套餐/余额/到期）
        + `getindexchart.do`（用量汇总）。
        """
        info, chart = self._fetch(account)
        return _overview(info, chart)

    def chart(self, account, today=None):
        """每日流量明细 + 汇总。**只调 `getindexchart.do` 一个接口。**

        返回 `{"days": [...], "summary": {...}}`，字段含义见 `parse_days()`。
        """
        chart = self._post(EP_CHART, {"accountId": account})
        days, summary = parse_days(chart if isinstance(chart, dict) else {},
                                   today)
        return {"days": days, "summary": summary}

    def online(self, account):
        """当前在线的设备列表。**只保留展示需要的字段。**

        🔴 **故意丢掉 `billingId` / `serverIp`** —— 那两个正是
        `kickonline.do`（踢设备下线）要求的参数。不留它们，
        这个模块就**结构上凑不出**踢人的请求。要加踢人功能必须**明确改这里**，
        而不是"顺手把原始 rows 传出去"。
        """
        j = self._post(EP_ONLINE, {"accountId": account})
        rows = j.get("rows") or []
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "ip": r.get("accountIp") or "",
                "mac": r.get("accountMac") or "",
                "since": r.get("onlineTime") or "",
                "os": r.get("osInfo") or "",
                "nas": r.get("nasId") or "",
                "kind": r.get("terminalType") or "",
            })
        return out

    # ------------------------------------------------------------ A 档取数

    def bill(self, account, start=None, end=None, page_size=BILL_PAGE_SIZE,
             max_pages=BILL_MAX_PAGES):
        """⭐ **上网详单（会话级）**。**只调 `getbilldetail.do`。**

        `start` / `end` 是 `"YYYY-MM-DD"`（可 `None` = 不限日期）。
        返回 `{"sessions": [...], "summary": {...}}`，字段含义见 `parse_bill()`。

        🔴 **翻页有兜底**：最多 `max_pages` 页。
           实测服务端**不限制** `pageSize`，所以正常情况 1~2 次请求就拉完了
           （半年 227 条 → 200 + 27）。
           ⚠️ **别去掉 `max_pages`** —— 万一服务端一直返回满页，
              没有它这里就是个死循环（而且它不会报错，只会一直转）。
        """
        sessions, summary = [], {}
        for page in range(1, max(1, int(max_pages)) + 1):
            payload = {"accountId": account,
                       "page": {"pageSize": page_size, "currentPage": page}}
            # ⚠️ 空日期**不要发**（前端也是把空字段删掉再发的）。
            #    发 `""` 和不发**不等价** —— 实测只发一个 `{"accountId", "page"}` 就通。
            if start:
                payload["startTime"] = start
            if end:
                payload["endTime"] = end
            j = self._post(EP_BILL, payload)
            rows = j.get("rows")
            rows = rows if isinstance(rows, list) else []
            part, summary = parse_bill(rows, j.get("data"), j.get("total"))
            sessions.extend(part)
            if len(rows) < page_size:
                break
        # `fetched` 以**真拉到的**为准（`parse_bill` 里那一份只看了最后一页）
        summary["fetched"] = len(sessions)
        return {"sessions": sessions, "summary": summary}

    def stop_state(self, account):
        """**停机 / 复机状态**。**只调 `getshutdownformation.do`。**"""
        return parse_stop(self._post(EP_STOP, {"accountId": account}).get("data"))

    def package(self, account):
        """**套餐概览**（可用流量 / 余额 / 状态）。**只调 `getpackageinformation.do`。**"""
        return parse_package(self._post(EP_PACKAGE, {"accountId": account}).get("data"))

    # ---------------------------------------------------- A 档第二批取数

    def base_info(self, account):
        """**详细资料**。**只调 `getbaseinfo.do`。**

        ⚠️ 返回是**白名单拼出来的**（见 `parse_baseinfo`），
           `baseMessageVo` 整块不取、`operatorPwd` 不取。
        """
        return parse_baseinfo(self._post(EP_BASEINFO, {"accountId": account}).get("data"))

    def _records(self, ep, account, parse, start=None, end=None,
                 page_size=BILL_PAGE_SIZE, max_pages=BILL_MAX_PAGES):
        """扣费 / 缴费记录**共用**的翻页取数。

        ⚠️ 两个接口的请求体结构**完全一样**（`{accountId, startTime, endTime, page}`），
           所以合并成一个 —— 免得两边各写一遍、日后改一个漏一个。
        🔴 和 `bill()` 一样**必须有 `max_pages` 兜底**：服务端要是一直返回满页，
           没有它就是死循环（而且不报错，只会一直转）。
        """
        rows, tail = [], {}
        for page in range(1, max(1, int(max_pages)) + 1):
            payload = {"accountId": account,
                       "page": {"pageSize": page_size, "currentPage": page}}
            # 空日期不要发（前端也是把空字段删掉再发）
            if start:
                payload["startTime"] = start
            if end:
                payload["endTime"] = end
            j = self._post(ep, payload)
            part = j.get("rows")
            part = part if isinstance(part, list) else []
            rows.extend(part)
            tail = j
            if len(part) < page_size:
                break
        return parse(rows, tail.get("data"), tail.get("total"))

    def fee_records(self, account, start=None, end=None, **kw):
        """**扣费记录**。**只调 `getfeerecord.do`。**"""
        return self._records(EP_FEE, account, parse_fee, start, end, **kw)

    def charge_records(self, account, start=None, end=None, **kw):
        """**缴费（充值）记录**。**只调 `getchargerecord.do`。**"""
        return self._records(EP_CHARGE, account, parse_charge, start, end, **kw)

    def bus_packages(self, account):
        """**可购套餐**（只列，不买）。**只调 `getbusinesspackage.do`。**"""
        return parse_buspkg(self._post(EP_BUSPKG, {"accountId": account}).get("rows"))

    def account_center(self, account, start=None, end=None, tolerant=False):
        """账号中心一次拿全：**上网详单 + 停机状态 + 套餐概览 + 详细资料
        + 扣费记录 + 缴费记录 + 可购套餐**。

        `tolerant=False`（默认）：任一失败就整个抛 `SelfError`（不吞错）。
        `tolerant=True`：各部分**各自 try** —— 成功的留下，失败的给 `None`，
            原因收在 `errors` 里（字符串列表）。
            ⇒ 界面要「一个挂了其它照常显示」时走这条。`full()` 就是这么用的。

        ⚠️ 2026-10-16 从 3 项扩到 7 项。新增的四项（`base` / `fee` / `charge` /
           `buspkg`）**字段名是从 `app.js` 逆出来的、没实测过** —— 所以
           `tolerant=True` 这条路对它们尤其重要：字段对不上时接口本身是通的
           （不会进 `errors`），只是解析出来是空的。
           ⇒ **界面不能因为「rows 空」就说「你没有记录」**（那是把错误说成事实）。
        """
        out, errs = {}, []
        for key, call in (("bill", lambda: self.bill(account, start, end)),
                          ("stop", lambda: self.stop_state(account)),
                          ("package", lambda: self.package(account)),
                          ("base", lambda: self.base_info(account)),
                          ("fee", lambda: self.fee_records(account, start, end)),
                          ("charge", lambda: self.charge_records(account, start, end)),
                          ("buspkg", lambda: self.bus_packages(account))):
            try:
                out[key] = call()
            except SelfError as e:
                if not tolerant:
                    raise
                out[key] = None
                errs.append(e.msg or key)
        if tolerant:
            out["errors"] = errs
        return out

    def full(self, account, today=None, start=None, end=None):
        """**界面用的一次拿全**：`load()`（状态 / 套餐 / 流量 / 设备）
        **加上** A 档（上网详单 / 停机状态 / 套餐概览）。

        🔴 **A 档失败不拖垮主数据**（这是故意的，和 `account_center()` 默认的
           「任一失败就抛」相反）。理由：主数据是用户每次同步都要的，
           A 档是附加的、而且是学校那边最容易变的几个接口。让「详单接口抽风」
           把整次同步变成失败，等于用一个附加功能废掉主功能。

        ⚠️ 但**绝不能安静吞掉**：失败原因放在 `center_error` 里（空串 = 全好）。
           界面必须**显式**说「详单没读到」—— 否则用户会以为
           「这个账号没有上网记录」，那是把「没读到」说成「没有」，
           比直接报错更坏。
        """
        out = self.load(account, today=today)
        center = self.account_center(account, start, end, tolerant=True)
        errs = center.pop("errors")
        out.update(center)
        out["center_error"] = "；".join(errs)
        return out

    def load(self, account, today=None):
        """一次拿全（状态 + 用量汇总 + 每日明细 + 在线设备）。

        ⚠️ `getindexchart.do` **只调一次**（`_fetch` 里），汇总和每日明细共用
           同一个返回 —— 见 `_fetch` 的说明。
        """
        info, chart = self._fetch(account)
        d = _overview(info, chart)
        days, summary = parse_days(chart, today)
        d["days"] = days
        d["summary"] = summary
        d["devices"] = self.online(account)
        return d


def _overview(info, chart):
    """把两个接口的返回拼成「状态 + 用量汇总」。**纯函数，不联网。**

    🔴 **返回的每个字段都是手写拼出来的**（模块文档规矩 2）——
    不是「把响应拷过来再删几个键」。学校一加字段，黑名单就会漏。
    """
    use = chart.get("baseUseVo") if isinstance(chart, dict) else None
    info = info if isinstance(info, dict) else {}
    use = use if isinstance(use, dict) else {}
    return {
        # --- 账号状态（来自 getindexinfo） ---
        "status": info.get("accountStatus") or use.get("accountStatus") or "",
        "plan": info.get("internetTacticsName") or "",
        "area": info.get("areaName") or "",
        "expire": info.get("expirationDate") or "",
        "billing_day": info.get("billingcycletime") or "",
        "charge_type": str(info.get("chargeType") or ""),
        # 余额单位「元」是实测确认的（模板里 `remainMoney + "元"`）
        "balance": _num(info.get("remainMoney")),
        # --- 用量（来自 getindexchart） ---
        "flow": {
            "today": _num(use.get("sameDayFlow")),
            "month": _num(use.get("sameMonthFlow")),
            "total": _num(use.get("cumulativeFlow")),
            "limit": use.get("limtFlows") or "",
        },
        "time": {
            "today": _num(use.get("sameDayTime")),
            "month": _num(use.get("sameMonthTime")),
            "total": _num(use.get("cumulativeTime")),
            "limit": use.get("limtTimes") or "",
        },
    }
