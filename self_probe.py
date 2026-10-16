#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""郑航自助服务（/self/）探测脚本 —— 用来「先看清楚返回什么，再写功能」。

背景（2026-10-12）：
    用户发现 http://202.196.169.166/self/index.html 是郑航的**用户自助管理**系统，
    而 202.196.169.166 **就是软件现在用的 portalHost**（认证走 /webauth.do）——
    同一个服务器上的另一个子系统。所以这不是新增外部依赖。

    前端是 Vue SPA，逻辑全在 static/js/app.*.js 里。逆向结论：
      · 请求封装 axios({baseURL:"", url, data}) ⇒ 同源、全 POST 到 /self/*.do
      · 登录：POST /self/login.do  body={accountId, password: c(明文), verifyCode}
        其中 c(e) = w(u(v(e), e.length*8)) —— **标准 MD5、小写十六进制、无盐**
      · 成功判据 errcode === "0"；未登录 errcode === "-1"
      · 登录要**图片验证码**（tologin.do 返回 data.verifyCode = base64 PNG）

🔴 这个脚本是**只读探针**：
    · 只登录一次，只调「查」类接口，不调任何 kick / clear / 改密码 / 缴费
    · 走 datasafe.sandbox() —— 一个字节都不会写回真实数据目录
    · 不打印密码；账号只打印长度
    · 🔴 敏感字段（身份证号/手机号/明文密码/真实姓名）**存盘和打印前都屏蔽**
    · 输出落在临时目录，不进 git

用法（**推荐走 `ask`**，它把「取码 + 放大」一步做完）：
    # 第一步：取一张验证码，同时写一张放大 8 倍的图（方便肉眼读）
    python self_probe.py ask
    # 看一眼那张放大图，把 4 位字符填进来
    # 第二步：登录 + 抓接口
    python self_probe.py login 8f3k
    # 若 JSON 体不被接受，换表单编码再试（要重新取验证码）
    python self_probe.py login 8f3k --mode form

    # 次数闸：连错可能被学校系统锁号，所以默认只允许 3 次
    python self_probe.py status
    python self_probe.py reset

🔴 **验证码会过期**（通常几分钟）。所以顺序必须是「人先准备好 → 现取 → 马上读 →
    马上 login」，不要提前取一张放着。过期了的表现是 errmsg 里说验证码不对，
    那种情况下**不是密码错**，重取一张即可（不计入防锁号闸的判定逻辑）。
"""

import base64
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://202.196.169.166"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
STATE = os.path.join(tempfile.gettempdir(), "zjzu_self_probe")
COOKIE_FILE = os.path.join(STATE, "cookies.txt")
ATTEMPT_FILE = os.path.join(STATE, "attempts.txt")
CAPTCHA_PNG = os.path.join(STATE, "captcha.png")     # 原始尺寸
CAPTCHA_BIG = os.path.join(STATE, "captcha_8x.png")  # 放大 8 倍，给人看的

# 🔴 防锁号：连错密码可能被学校系统锁账号。这个闸门只管「真发出去的登录请求」，
#    取验证码不计数。想清零就 `python self_probe.py reset`。
MAX_ATTEMPTS = 3

# 只探「查」类接口。⚠️ 绝不加 kickonline / clearusermac / 缴费 / 改密码。
#
# 每条是 (接口, 说明, 附加参数)。附加参数会与 {"accountId": 账号} 合并 ——
# 分页类接口（getfeerecord / getchargerecord / getbilldetail）要带 page，
# 默认值照抄前端：pageSize=5、currentPage=1（见 app.js 的 data()）。
# ⚠️ 前端用 `ne()` 把**空字段整个删掉**再发（日期区间留空 = 不带这俩键），
#    所以这里**不写** startTime / endTime，等价于「不限日期」。
#
# A 档（2026-10-15 新增，全部 get* 只读，参数从 app.js 逆出）。
# ⚠️ 说明按**实测**写，别照名字猜 —— 实测结果见 `郑航自助服务-接口清单.md` §3A：
#   getshutdownformation.do  停机/复机状态    {accountId}
#   getpackageinformation.do 套餐概览         {accountId}
#   getbusinesspackage.do    可购套餐列表      {accountId}   → 实测 rows=[] total=0
#   getbilldetail.do         ⭐ 上网详单（会话级流量/时长/费用/断开原因）
#   getchargerecord.do       充值记录         → 实测 rows=[] total=0
#   getfeerecord.do          扣费记录         → 实测 rows=[] total=0
# 🔴 `getbilldetail.do` 是**账单明细**这个名字的误导：它返回的是**会话级上网详单**
#    （startDate/endDate/chargingDuration/uploadFlows/downloadFlows）。
PROBE = [
    ("getindexinfo.do", "首页概览", {}),
    ("getindexchart.do", "首页图表", {}),
    ("getuserinformation.do", "用户资料", {}),
    ("getonline.do", "在线记录（当前在线设备）", {}),
    ("getbaseinfo.do", "详细资料（🔴 最敏感，含身份证/明文密码）", {}),
    # ---- A 档：账号中心 ----
    ("getshutdownformation.do", "停机状态", {}),
    ("getpackageinformation.do", "套餐概览（余额/可用流量/状态）", {}),
    ("getbusinesspackage.do", "可购套餐列表", {}),
    ("getbilldetail.do", "⭐ 上网详单（会话级）",
     {"page": {"pageSize": 5, "currentPage": 1}}),
    ("getchargerecord.do", "充值记录",
     {"page": {"pageSize": 5, "currentPage": 1}}),
    ("getfeerecord.do", "扣费记录",
     {"page": {"pageSize": 5, "currentPage": 1}}),
]

# 🔴 这些键**即使只在临时目录**也不该留原文（实测确认接口真的会返回它们）。
#    dump.json 是调试产物，没必要把身份证号/手机号/明文密码抄一份进去。
#    值一律换成 "<已屏蔽>" —— 键还在，**形状**仍然看得见，只是内容不留。
REDACT_KEYS = {
    "idCardNum", "cardNo", "accountMobile", "accNbr",
    "operatorAccount", "operatorPwd", "accountName",
    "password", "oldPassword", "newPassword", "newPasswordAgain",
    "email", "accountPwd", "openid",
}


def _redact(obj):
    """递归把敏感键的值换成占位符。**键保留、值抹掉。**"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in REDACT_KEYS and v not in (None, ""):
                out[k] = "<已屏蔽:%d 字符>" % len(str(v))
            elif isinstance(v, (dict, list)):
                out[k] = _redact(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


# ---------------------------------------------------------------- 小工具

def js_md5(s):
    """复刻前端 c(e) = w(u(v(e), e.length*8))。

    MD5 本身是标准的，但**输入不是 UTF-8**：v() 是 `charCodeAt(r/l) & 255`，
    也就是「每个 UTF-16 码元取低 8 位」。纯 ASCII 时和 UTF-8 等价，
    但中文密码下两者不同 —— 所以这里按 UTF-16LE 逐码元取低字节。
    """
    data = bytes(b & 0xFF for b in s.encode("utf-16-le")[::2])
    return hashlib.md5(data).hexdigest()


def _short(obj, limit=600):
    """把超长字段（比如验证码图片）截断，免得刷屏。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and len(v) > 120:
                out[k] = "<%d 字符，已截断>" % len(v)
            elif isinstance(v, (dict, list)):
                out[k] = _short(v, limit)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_short(x, limit) for x in obj[:8]]
    return obj


def _attempts():
    try:
        with open(ATTEMPT_FILE, "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def _bump_attempts():
    os.makedirs(STATE, exist_ok=True)
    n = _attempts() + 1
    with open(ATTEMPT_FILE, "w", encoding="utf-8") as f:
        f.write(str(n))
    return n


def _opener():
    os.makedirs(STATE, exist_ok=True)
    jar = http.cookiejar.LWPCookieJar(COOKIE_FILE)
    if os.path.isfile(COOKIE_FILE):
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def post(op, path, payload=None, mode="json"):
    """POST 到 /self/<path>。payload=None 表示不带 body（tologin.do 就是这样）。"""
    req = urllib.request.Request(BASE + "/self/" + path, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Referer", BASE + "/self/index.html")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("X-Requested-With", "XMLHttpRequest")
    if payload is None:
        data = b""
    elif mode == "json":
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json;charset=UTF-8")
    else:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req.add_header("Content-Type",
                       "application/x-www-form-urlencoded;charset=UTF-8")
    try:
        with op.open(req, data if data else None, timeout=15) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, "请求失败：%r" % (e,)


# ---------------------------------------------------------------- 取验证码

def cmd_captcha():
    op, jar = _opener()
    code, raw = post(op, "tologin.do")
    print("[tologin.do] HTTP %s" % code)
    try:
        j = json.loads(raw)
    except Exception:
        print(raw[:500])
        return 1
    print(json.dumps(_short(j), ensure_ascii=False, indent=2))

    d = j.get("data") or {}
    if d.get("user"):
        print("\n⚠️ 这个会话**已经登录**（data.user 有值），不用再输验证码。")
        jar.save(ignore_discard=True, ignore_expires=True)
        return 0

    b64 = d.get("verifyCode") or ""
    if not b64:
        print("\n❌ 没拿到 verifyCode，看看上面的返回。")
        return 1
    png = CAPTCHA_PNG
    with open(png, "wb") as f:
        f.write(base64.b64decode(b64))
    jar.save(ignore_discard=True, ignore_expires=True)
    print("\n✅ 验证码图片已存：%s" % png)
    print("   （用图片查看器打开，把 4 位字符作为参数传给 login 子命令）")
    return 0


# ---------------------------------------------------------------- 取码 + 放大（一步做完）

def cmd_ask(scale=8):
    """取一张验证码，并另写一张放大图 —— 给人肉读码用。

    为什么要合成一条命令：**验证码几分钟就过期**，
    所以「取码」和「给人看」之间的间隔越短越好。
    分两步跑（先 captcha 再 png_zoom）会多花十几秒，且容易忘。
    """
    rc = cmd_captcha()
    if rc != 0:
        return rc
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import png_zoom
        w, h, nch, px = png_zoom.read_png(CAPTCHA_PNG)
        rgb = png_zoom.to_rgb(w, h, nch, px)
        ow, oh, out = png_zoom.crop_zoom(w, h, rgb, 0, 0, w, h, scale)
        png_zoom.write_png(CAPTCHA_BIG, ow, oh, out)
        print("✅ 放大 %dx 的图已存：%s  (%dx%d)" % (scale, CAPTCHA_BIG, ow, oh))
    except Exception as e:
        print("⚠️ 放大失败（不影响登录，只是不好看）：%r" % (e,))
    print()
    print("👉 把上面那张图里的 4 位字符告诉我，然后跑：")
    print("   python self_probe.py login <4位>")
    return 0


# ---------------------------------------------------------------- 登录 + 抓接口

def cmd_login(code, mode="json"):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import campus_login as C
    import datasafe

    # 🔴 红线 7：会写数据的脚本必须先沙箱。这里其实只读，但照样盖住。
    #    copy_real=True ⇒ 读到的仍是这台机器真实的账号。
    sandbox = datasafe.sandbox(copy_real=True)
    print("[沙箱] 数据目录已换到 %s" % sandbox)

    cfg = C.load_config() or {}
    account = (cfg.get("userId") or "").strip()
    passwd = cfg.get("passwd") or ""
    print("[账号] userId 长度 %d（不打印内容）｜有密码: %s"
          % (len(account), bool(passwd)))
    if not account or not passwd:
        print("❌ config.json 里没有账号或密码，先在软件里保存一次账号。")
        return 2

    op, jar = _opener()
    body = {"accountId": account, "password": js_md5(passwd), "verifyCode": code}

    n = _attempts()
    if n >= MAX_ATTEMPTS:
        print("🛑 已经发过 %d 次登录请求（上限 %d）。" % (n, MAX_ATTEMPTS))
        print("   密码连错可能被学校系统锁号，所以这里**故意**停下来。")
        print("   确认要继续就 `python self_probe.py reset` 清零再跑。")
        return 3

    print("[登录] POST /self/login.do  mode=%s  第 %d/%d 次"
          % (mode, n + 1, MAX_ATTEMPTS))
    st, raw = post(op, "login.do", body, mode)
    _bump_attempts()
    print("[登录] HTTP %s" % st)
    try:
        j = json.loads(raw)
    except Exception:
        print("❌ 返回不是 JSON：\n%s" % raw[:600])
        return 1
    print(json.dumps(_short(j), ensure_ascii=False, indent=2))
    if str(j.get("errcode")) != "0":
        msg = j.get("errmsg") or ""
        print("\n❌ 登录失败：errcode=%s  errmsg=%r" % (j.get("errcode"), msg))
        # 🔴 把「验证码错」和「密码错」分开说 —— 这两件事的下一步完全不同：
        #    验证码错 ⇒ 重取一张再输；密码错 ⇒ 说明我的 MD5 复刻有问题。
        if "验证码" in msg or "校验码" in msg or "验证" in msg:
            print("   ⇒ 是**验证码**的问题，不是密码。重取一张再输一次。")
        else:
            print("   ⇒ 不是验证码的错。**先别重试**，把这条 errmsg 拿给我看 ——")
            print("     如果指向密码，说明前端那个 MD5 复刻得不对，要回去查。")
        print("   （验证码一次性，要重取）")
        return 1
    print("\n✅ 登录成功")
    jar.save(ignore_discard=True, ignore_expires=True)

    out = os.path.join(STATE, "dump.json")
    dump = {}
    for entry in PROBE:
        path, desc = entry[0], entry[1]
        extra = entry[2] if len(entry) > 2 else {}
        payload = {"accountId": account}
        payload.update(extra)
        st, raw = post(op, path, payload)
        try:
            jj = json.loads(raw)
        except Exception:
            jj = {"_raw": raw[:800]}
        # 🔴 屏蔽敏感字段**再**存/再打印 —— 临时目录也不留身份证号/明文密码。
        jj = _redact(jj)
        dump[path] = jj
        print("\n" + "=" * 66)
        print("### %s   —— %s" % (path, desc))
        print("=" * 66)
        print(json.dumps(_short(jj, 2000), ensure_ascii=False, indent=2)[:4000])

    with open(out, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 66)
    print("完整原始返回已存：%s" % out)
    print("（临时目录，含账号信息，**别提交进 git**）")
    return 0


def cmd_probe_only():
    """**不登录**，直接用存下来的 cookie 打接口 —— 用来验证「会话能不能复用」。

    为什么这件事很重要：验证码只能人工输一次。如果会话能撑很久，
    功能就可以做成「输一次验证码 → 之后几十分钟随便查」；
    如果每次都要验证码，体验就很差（每查一次都要人读图）。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import campus_login as C
    import datasafe

    sandbox = datasafe.sandbox(copy_real=True)
    print("[沙箱] 数据目录已换到 %s" % sandbox)
    cfg = C.load_config() or {}
    account = (cfg.get("userId") or "").strip()
    print("[账号] userId 长度 %d" % len(account))

    op, jar = _opener()
    n = len(list(jar))
    print("[会话] 从 %s 读到 %d 条 cookie" % (COOKIE_FILE, n))
    if n == 0:
        print("❌ 没有 cookie，先跑一次 login。")
        return 1

    alive = 0
    for entry in PROBE:
        path, desc = entry[0], entry[1]
        extra = entry[2] if len(entry) > 2 else {}
        payload = {"accountId": account}
        payload.update(extra)
        st, raw = post(op, path, payload)
        try:
            j = json.loads(raw)
        except Exception:
            j = {}
        ec = str(j.get("errcode"))
        ok = (ec == "0")
        alive += 1 if ok else 0
        print("  %-26s errcode=%-4s %s" % (path, ec, "✅" if ok else j.get("errmsg")))
    print("\n%d/%d 个接口仍然可用" % (alive, len(PROBE)))
    if alive == len(PROBE):
        print("✅ **会话还有效** —— 不用再输验证码")
    else:
        print("❌ 会话已失效，要重新走 captcha + login")
    return 0 if alive else 1


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd == "captcha":
        return cmd_captcha()
    if cmd == "ask":
        return cmd_ask()
    if cmd == "login":
        if len(argv) < 3:
            print("用法：python self_probe.py login <验证码> [--mode form]")
            return 0
        mode = "form" if "--mode" in argv and "form" in argv else "json"
        return cmd_login(argv[2], mode)
    if cmd == "reset":
        try:
            os.remove(ATTEMPT_FILE)
        except OSError:
            pass
        print("✅ 登录尝试计数已清零（当前 %d）" % _attempts())
        return 0
    if cmd == "probe":
        return cmd_probe_only()
    if cmd == "status":
        print("登录尝试计数：%d / %d" % (_attempts(), MAX_ATTEMPTS))
        print("验证码文件：%s" % CAPTCHA_PNG)
        print("放大图：    %s" % CAPTCHA_BIG)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
