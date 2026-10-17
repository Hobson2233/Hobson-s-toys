# -*- coding: utf-8 -*-
"""实测「自助服务会话到底能活多久」—— 分三步走，跨进程用临时文件传 cookie。

    python session_ttl.py captcha              # ① 取一张验证码，存到 _lab/out/captcha.png
    python session_ttl.py login <验证码>        # ② 登录，把 cookie 存到 %TEMP%\\self_sess.json
    python session_ttl.py poll [分钟] [间隔秒]  # ③ 定时探活，打印存活曲线

🔴 为什么非要实测（2026-10-17）：
    `JSESSIONID-BOSS-0` 是 **session cookie**（没有 Max-Age / Expires），
    客户端存多久都不算数 —— 真正的上限完全由**服务端**决定。
    而服务端的超时是「滑动」还是「固定」，直接决定两件事：

      · 滑动过期 ⇒ 程序开着的时候定时轻探一下就能**一直不掉线**（保活可行）
      · 固定寿命 ⇒ 保活没用，只能在过期后重新登录

    所以第 ③ 步每 2 分钟探一次、连续探 45 分钟：
    要是活过 30 分钟（Tomcat 默认 session-timeout）还活着 ⇒ 就是滑动过期。

⚠️ 这个脚本**只读**：只打 tologin / getindexinfo，不改任何数据。
⚠️ cookie 只写 `%TEMP%`，**绝不落进仓库**（红线 1：凭据不进 git）。
"""
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import self_service as S                                  # noqa: E402
import campus_login as C                                  # noqa: E402

OUT_DIR = os.path.join(HERE, "out")
CAPTCHA = os.path.join(OUT_DIR, "captcha.png")
SESS = os.path.join(os.environ.get("TEMP") or "/tmp", "self_sess.json")


def _creds():
    """从程序自己存的那份配置里取 (账号, 密码)。

    🔴 **不要在这里写死密码**：2026-10-17 实测踩过 ——
       智慧门户的密码和「自助服务 / 校园网」的密码**不是同一个**
       （一个 10 位、一个 9 位），拿门户密码去登录自助服务会返回
       「登录失败,密码错误」。而那句话看起来像「验证码读错了」，
       很容易把人往错的方向带。
       程序里 `_center_creds()` 用的就是这份配置，所以这里也用它。
    ⚠️ 密码**绝不打印**。
    """
    cfg = C.read_secret_json(C.CONFIG_FILE) or {}
    uid = (cfg.get("userId") or "").strip()
    pwd = cfg.get("passwd") or ""
    if not uid or not pwd:
        raise SystemExit("本机没存账号密码（config.json 里是空的），先打开程序填一次")
    return uid, pwd


def _mk(base=None):
    return S.SelfService(base=base or S.DEFAULT_BASE)


def _cookie_obj(name, value, host):
    import http.cookiejar as cj
    return cj.Cookie(
        version=0, name=name, value=value,
        port=None, port_specified=False,
        domain=host, domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True,
        secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest=None)


def _save(svc):
    host = svc.base.split("//", 1)[-1].split("/")[0].split(":")[0]
    data = {"base": svc.base, "host": host, "loginAt": time.time(),
            "cookies": [[c.name, c.value] for c in svc._jar]}
    with io.open(SESS, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print("cookie 已存：%s（%d 条）" % (SESS, len(data["cookies"])))
    for n, _v in data["cookies"]:
        print("   · %s" % n)


def _load():
    with io.open(SESS, encoding="utf-8") as f:
        data = json.load(f)
    svc = _mk(data["base"])
    for n, v in data["cookies"]:
        svc._jar.set_cookie(_cookie_obj(n, v, data["host"]))
    return svc, data


def cmd_captcha():
    svc = _mk()
    png, already = svc.fetch_captcha()
    if already:
        print("这个进程的会话居然还有效 —— 不用验证码")
        return 0
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    with open(CAPTCHA, "wb") as f:
        f.write(png)
    print("验证码已存：%s（%d 字节）" % (CAPTCHA, len(png)))
    return 0


def cmd_login(_unused=None, wait=1800):
    """取码 → 等我把答案写进 out/answer.txt → 登录 → 存 cookie。

    🔴 **必须同一个进程**：验证码是**按会话存在服务端**的。
       分两次跑（先取码、再登录）会各自拿到一个 JSESSIONID，
       结果是「验证码为空,登录失败」—— 实测踩过。
    """
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    answer = os.path.join(OUT_DIR, "answer.txt")
    if os.path.exists(answer):
        os.remove(answer)

    svc = _mk()
    png, already = svc.fetch_captcha()
    if already:
        print("会话还有效，不用验证码")
        _save(svc)
        return 0
    with open(CAPTCHA, "wb") as f:
        f.write(png)
    print("验证码已存：%s —— 等答案（out/answer.txt，最多 %d 秒）"
          % (CAPTCHA, wait))
    sys.stdout.flush()

    t0 = time.time()
    code = ""
    while time.time() - t0 < wait:
        if os.path.exists(answer):
            with io.open(answer, encoding="utf-8") as f:
                code = (f.read() or "").strip()
            if code:
                break
        time.sleep(0.5)
    if not code:
        print("等答案超时，放弃")
        return 1
    print("收到答案：%s" % code)

    account, password = _creds()
    print("用本机存的账号登录：%s（密码长度 %d，不显示内容）"
          % (account, len(password)))
    ok = svc.login(account, password, code)
    print("登录结果：%s" % ("成功" if ok else "失败"))
    if not ok:
        return 1
    _save(svc)
    return 0


def cmd_poll(minutes=45, interval=120):
    svc, data = _load()
    t0 = data.get("loginAt") or time.time()
    deadline = time.time() + minutes * 60
    print("开始探活：每 %d 秒一次，最多 %d 分钟。登录时刻 %s"
          % (interval, minutes, time.strftime("%H:%M:%S", time.localtime(t0))))
    n = 0
    while time.time() < deadline:
        n += 1
        alive = t0
        try:
            alive = svc.has_session(_creds()[0])
        except Exception as e:
            print("[%s] 第 %d 次：探测本身出错（%s: %s）—— 不算会话失效"
                  % (time.strftime("%H:%M:%S"), n, type(e).__name__, e))
            alive = None
        elapsed = (time.time() - t0) / 60.0
        if alive is True:
            print("[%s] 第 %d 次：**还活着**（登录后 %.1f 分钟）"
                  % (time.strftime("%H:%M:%S"), n, elapsed))
        elif alive is False:
            print("[%s] 第 %d 次：**已失效**（登录后 %.1f 分钟）"
                  % (time.strftime("%H:%M:%S"), n, elapsed))
            print("\n结论：会话在「一直有访问」的情况下撑了 %.1f 分钟。"
                  % elapsed)
            return 0
        time.sleep(interval)
    print("\n跑满 %d 分钟仍然活着 ⇒ 服务端是**滑动过期**（有访问就续命），"
          "保活这条路可行。" % minutes)
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "captcha":
        return cmd_captcha()
    if cmd == "login":
        return cmd_login(argv[1] if len(argv) > 1 else "")
    if cmd == "poll":
        m = float(argv[1]) if len(argv) > 1 else 45
        iv = float(argv[2]) if len(argv) > 2 else 120
        return cmd_poll(m, iv)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
