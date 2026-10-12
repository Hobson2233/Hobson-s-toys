#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""郑航自助服务（/self/）取数层 —— 「上网数据」功能的数据来源。

背景：`http://202.196.169.166/self/` 是郑航的**用户自助管理**系统，而
`202.196.169.166` **就是软件现在用的 portalHost**（认证走 `/webauth.do`）——
同一台服务器上的另一个子系统。所以这不是新增外部依赖。
接口与单位已全部实测，见 `郑航自助服务-接口清单.md`。

**为什么单独一个模块**（而不是塞进 `campus_login.py`）：
  · 它是一个**完整的、能脱离界面单独测的东西**（取码 → 登录 → 取数 → 解析），
    和 tkinter 无关。`campus_login.py` 已经 7000 行，网络层再进去会很难维护。
  · `updater.py` 当初也是这么分出去的。

🔴 四条不可破的规矩：

  1. **只读。** 本模块只调 `tologin.do` / `login.do` / `getindexinfo.do` /
     `getindexchart.do` / `getonline.do`。
     **绝不加** `kickonline.do`（踢设备下线）/ `clearusermac.do`（解绑 MAC）/
     缴费 / 改密码 —— 要加必须是单独评审过的改动。
     ⚠️ 配套措施：`online()` **故意不保留** `billingId` / `serverIp`
     （那两个正是 kick 需要的参数）⇒ **结构上就凑不出踢人的请求**。

  2. 🔴 **构造式输出，不是过滤式。** 接口返回里**真的带**身份证号、手机号、
     真实姓名、以及一个明文密码（实测确认，见 `baseMessageVo` / `authMessageVo`）。
     所以本模块的每个返回值都是**逐个字段手写拼出来的**，
     **不是「把响应拷过来再删几个键」** —— 后者一旦学校加字段就会漏。
     ⚠️ 配套措施：`getbaseinfo.do` / `getuserinformation.do` **根本不调**
     （那才是身份证号/密码的出处）。

  3. 🔴 **不落盘、不写日志。** cookie 用内存 `CookieJar`（不是 `LWPCookieJar`）；
     任何响应内容都不进 log、不写缓存文件。

  4. 🔴 **验证码只能人工输。** 登录要图片验证码（4 位），所以这个功能
     **没法无人值守**，只能交互式。别试图绕过或自动识别。
"""

import base64
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

# 只读接口。**这里没有、也不许有** kick / clear / 缴费 / 改密码。
EP_TOLOGIN = "tologin.do"
EP_LOGIN = "login.do"
EP_INDEX = "getindexinfo.do"
EP_CHART = "getindexchart.do"
EP_ONLINE = "getonline.do"

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


def fmt_hours(h):
    """时长（**小时**）→ 给人看的字符串。单位实测确认（图表 yAxis `(单位/小时)`）。

    ⚠️ 学校自己的日时长会出现 **> 24 的值**（实测 `allUseTime` 里有 `"42"`），
    那是他们统计口径的问题。**我们照原样显示、不做"修正"** ——
    用户会拿我们的界面和学校网页对照，我们自己改了反而像我们算错。
    """
    return "%.2f 小时" % _num(h)


def _looks_like_captcha(msg):
    """errmsg 是不是在说「验证码不对」。"""
    return any(h in (msg or "") for h in _CAPTCHA_HINTS)


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

    # ------------------------------------------------------------ 取数

    def overview(self, account):
        """状态 + 用量。**返回的每个字段都是这里手写拼出来的**（见模块文档规矩 2）。

        只调两个接口：`getindexinfo.do`（状态/套餐/余额/到期）
        + `getindexchart.do`（用量汇总）。
        """
        info = self._post(EP_INDEX, {"accountId": account}).get("data") or {}
        chart = self._post(EP_CHART, {"accountId": account})
        use = chart.get("baseUseVo") or {}
        if not isinstance(info, dict):
            info = {}
        if not isinstance(use, dict):
            use = {}

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

    def load(self, account):
        """一次拿全（状态 + 用量 + 在线设备）。"""
        d = self.overview(account)
        d["devices"] = self.online(account)
        return d
