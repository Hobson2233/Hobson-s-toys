# -*- coding: utf-8 -*-
"""探一下自助服务的会话机制：cookie 到底有没有「过期时间」。

为什么要探（2026-10-17）：
    用户问「每次同步都要登录，能不能登录后一段时间免登录」。
    答案完全取决于**服务端的会话能活多久**：
      · cookie 带 `Max-Age` / `Expires` ⇒ 有明确的存活期，值得做持久化
      · 是 session cookie（无过期时间）⇒ 存活期由**服务端**决定，
        客户端看不到，只能登录后隔一段时间再试一次才知道

    这一步**不需要账号密码**：`tologin.do` 不带 body 就是「给我一张验证码」，
    未登录也能请求。所以可以安全地先看响应头。

⚠️ 只发一个请求、只看响应头，不登录、不读任何业务数据。
⚠️ 不打印验证码内容（那是一张图，没意义）。

用法：python _lab/session_probe.py
"""
import http.cookiejar
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import self_service as S        # noqa: E402


def main():
    url = "%s/self/%s" % (S.DEFAULT_BASE.rstrip("/"), S.EP_TOLOGIN)
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, method="POST")
    req.add_header("User-Agent", S.UA)
    req.add_header("Referer", "%s/self/index.html" % S.DEFAULT_BASE)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("X-Requested-With", "XMLHttpRequest")

    print("请求 %s" % url)
    try:
        with op.open(req, None, timeout=S.TIMEOUT) as r:
            print("HTTP %s" % r.status)
            print("\n--- 响应头 ---")
            for k, v in r.headers.items():
                print("  %-28s %s" % (k, v))
            body = r.read()
    except Exception as e:
        print("请求失败：%s: %s" % (type(e).__name__, e))
        return 1

    print("\n--- CookieJar 里收到的 cookie ---")
    if not len(jar):
        print("  （没有 cookie）")
    for c in jar:
        # `expires` 为 None = session cookie（浏览器/进程关闭就丢）
        exp = c.expires
        if exp is None:
            ttl = "**session cookie（没有过期时间，存活期由服务端决定）**"
        else:
            import datetime
            ttl = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")
            ttl = "%s（还剩约 %d 秒）" % (ttl, exp - int(__import__("time").time()))
        print("  name=%-22s domain=%-18s path=%-8s" % (c.name, c.domain, c.path))
        print("      value=%s..." % (c.value[:12] if c.value else ""))
        print("      expires: %s" % ttl)
        print("      HttpOnly=%s  Secure=%s" % (c.has_nonstandard_attr("HttpOnly"),
                                                c.secure))

    print("\n--- 响应体（只看结构，不打印内容）---")
    try:
        import json
        j = json.loads(body.decode("utf-8", "replace"))
        print("  errcode = %r" % (j.get("errcode"),))
        d = j.get("data") or {}
        if isinstance(d, dict):
            print("  字段 = %s" % sorted(d.keys()))
            print("  已登录？(data.user 存在) = %s" % bool(d.get("user")))
            print("  给了验证码图片？ = %s" % bool(d.get("verifyCode")))
    except Exception as e:
        print("  解析失败：%s" % e)

    print("\n=== 结论怎么读 ===")
    print("  · 如果 cookie 是 session cookie ⇒ 客户端存多久都没用，")
    print("    真正的上限是**服务端的 session 超时**，只能登录后实测。")
    print("  · 如果有 Max-Age / Expires ⇒ 那就是可以免登录的时长。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
