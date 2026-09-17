# -*- coding: utf-8 -*-
"""
校园网自动登录（单文件版）
  - 不带参数        : 打开设置界面
  - --auto          : 静默模式（开机自启用），未联网才登录，不弹任何窗口
  - --logout        : 静默下线
  - --switch <学号> : 静默切换到指定账号（密码取自 accounts.json）
  - --selftest      : 环境自检，结果打印并写入数据目录 selftest.txt
  - --guitest       : 只构建界面不显示，结果写入数据目录 guitest.txt
  - --purge         : 清除本机保存的账号密码等隐私数据

数据目录：C:\\ProgramData\\CampusLogin
          （该目录写不进去时自动退回 %APPDATA%\\CampusLogin）
          账号密码只存在这里，**不打包进 exe**，所以把 exe 拷到别的电脑
          不会带出任何隐私信息。
"""
import os
import re
import sys
import json
import time
import socket
import threading
import subprocess
import traceback
import http.cookiejar
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

APP_NAME = "CampusLogin"
APP_TITLE = "校园网自动登录"
VERSION = "2.0"

# 学校 portal 默认参数（拷到同校其他电脑上可直接用）
DEFAULTS = {
    "portalHost": "http://202.196.169.166",
    "wlanAcIp": "10.0.1.10",
    "wlanAcName": "ZZHK-ZAX-BRAS",
    "timeoutSec": 8,
}

TRIGGER_URL = "http://1.1.1.1/"
# 连通性检测目标：逐个试，任一通过即算「已联网」。
#
# 为什么不是一个地址就够：只认一个域名时，该域名一旦被校园网拦截或临时不可达，
# 就会把「能上网」误判成「未连接」。实测首次 DNS 解析要 7.4 秒、之后只要 0.2 秒，
# 所以冷启动那一次尤其容易超时。
#   (url, 期望正文；None 表示只看状态码, 简短名字)
CHECK_TARGETS = (
    ("http://www.msftconnecttest.com/connecttest.txt", "Microsoft Connect Test", "msftconnecttest"),
    ("http://www.msftncsi.com/ncsi.txt", "Microsoft NCSI", "msftncsi"),
    ("http://connectivitycheck.platform.hicloud.com/generate_204", None, "hicloud"),
    ("http://connect.rom.miui.com/generate_204", None, "miui"),
)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ==================== 路径 ====================
CSIDL_APPDATA = 0x001A         # %APPDATA%（Roaming，用户级）
CSIDL_COMMON_APPDATA = 0x0023  # C:\ProgramData（机器级）
CSIDL_STARTUP = 0x0007         # 「启动」文件夹


def _shell_folder(csidl):
    """用 Windows API 取系统文件夹。比读环境变量可靠——
    某些场合（服务、精简环境、被剥掉环境变量的进程）%APPDATA% 是不存在的。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return ""


def roaming_dir():
    d = os.environ.get("APPDATA")
    if d and os.path.isdir(d):
        return d
    d = _shell_folder(CSIDL_APPDATA)
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def startup_dir():
    d = _shell_folder(CSIDL_STARTUP)
    if d and os.path.isdir(d):
        return d
    return os.path.join(roaming_dir(), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


def is_frozen():
    return getattr(sys, "frozen", False)


def app_path():
    """exe 自身路径（打包后）或脚本路径（源码运行时）"""
    if is_frozen():
        return os.path.abspath(sys.executable)
    return os.path.abspath(__file__)


def res_path(name):
    """打包后资源解压目录里的文件"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def icon_path():
    for n in ("设置.ico", "app.ico"):
        p = res_path(n)
        if os.path.isfile(p):
            return p
    return ""


def _writable(d):
    """目录能不能**真的写进去**（不是只看它存不存在）。

    有些机器上 C:\\ProgramData 存在但被策略锁死，只看 isdir 会误判。
    """
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_probe")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


_DATA_DIR = []


def data_dir():
    """数据目录。

    首选 `C:\\ProgramData\\CampusLogin`：机器级、路径固定、不藏在用户配置里，
    拷 exe 给别的电脑时它自然是空的（账号密码不会跟着 exe 跑）。
    ProgramData 写不进去（被策略锁了）才退回 `%APPDATA%\\CampusLogin`。

    结果缓存一次：这个函数在导入期就要被调用多次，每次都做写测试太浪费。
    """
    if _DATA_DIR:
        return _DATA_DIR[0]
    candidates = []
    common = _shell_folder(CSIDL_COMMON_APPDATA) or os.environ.get("ProgramData") or ""
    if common:
        candidates.append(os.path.join(common, APP_NAME))
    candidates.append(os.path.join(roaming_dir(), APP_NAME))
    for d in candidates:
        if _writable(d):
            _DATA_DIR.append(d)
            return d
    # 都写不进去也返回首选路径，让上层报错可见
    _DATA_DIR.append(candidates[0])
    return candidates[0]


CONFIG_FILE = os.path.join(data_dir(), "config.json")
ACCOUNTS_FILE = os.path.join(data_dir(), "accounts.json")
LOG_FILE = os.path.join(data_dir(), "campus-login.log")
RESULT_FILE = os.path.join(data_dir(), "last-result.json")

LEGACY_DIR = r"C:\tools"          # 最老的 PowerShell 版部署位置，首次运行时会尝试继承

# 本机保存过的隐私文件清单（--purge 用；显式列举，不用通配符）
PRIVATE_FILES = ("config.json", "accounts.json", "last-result.json",
                 "campus-login.log", "selftest.txt", "guitest.txt")


def legacy_data_dirs():
    """老的数据位置，按优先级：
      1) %APPDATA%\\CampusLogin  —— 上一版 exe 用的地方
      2) C:\\tools                —— 最老的 PowerShell 版
    """
    return [os.path.join(roaming_dir(), APP_NAME), LEGACY_DIR]


# ==================== 日志 ====================
def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_json(path, obj):
    """一律写 UTF-8 无 BOM"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


# ==================== 配置 ====================
def load_config():
    cfg = read_json(CONFIG_FILE)
    if not isinstance(cfg, dict):
        cfg = {}
    out = dict(DEFAULTS)
    out["userId"] = ""
    out["passwd"] = ""
    out.update({k: v for k, v in cfg.items() if v is not None})
    return out


def save_config(cfg):
    write_json(CONFIG_FILE, cfg)


def load_accounts():
    s = read_json(ACCOUNTS_FILE)
    if not isinstance(s, dict):
        s = {}
    if not isinstance(s.get("accounts"), dict):
        s["accounts"] = {}
    if not s.get("lastOnline"):
        s["lastOnline"] = ""
    for k, v in list(s["accounts"].items()):
        if not isinstance(v, dict):
            s["accounts"][k] = {"passwd": "", "lastSuccess": "", "lastAttempt": "", "lastResult": ""}
    return s


def save_accounts(store):
    write_json(ACCOUNTS_FILE, store)


def upsert_account(uid, pwd, overwrite):
    """记录一个账号。

    overwrite=False 时**不覆盖**已存的密码 —— 切换账号时必须这样，
    否则回滚目标的密码就被新密码冲掉了，保险会失效。
    """
    store = load_accounts()
    if uid not in store["accounts"]:
        store["accounts"][uid] = {"passwd": pwd, "lastSuccess": "", "lastAttempt": "", "lastResult": ""}
    elif overwrite or not store["accounts"][uid].get("passwd"):
        store["accounts"][uid]["passwd"] = pwd
    save_accounts(store)


def update_account_record(uid, pwd, ok):
    store = load_accounts()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if uid not in store["accounts"]:
        store["accounts"][uid] = {"passwd": "", "lastSuccess": "", "lastAttempt": "", "lastResult": ""}
    a = store["accounts"][uid]
    a["lastAttempt"] = now
    a["lastResult"] = "成功" if ok else "失败"
    if ok:
        a["passwd"] = pwd
        a["lastSuccess"] = now
        store["lastOnline"] = uid
    save_accounts(store)


def set_last_online(uid):
    store = load_accounts()
    store["lastOnline"] = uid
    save_accounts(store)


def set_active_account(uid, pwd):
    cfg = load_config()
    cfg["userId"] = uid
    cfg["passwd"] = pwd
    save_config(cfg)


def resolve_rollback(prev_uid):
    """切换失败要退回哪个账号：优先 lastOnline（真正在线的），再退回 PrevUserId"""
    store = load_accounts()
    for uid in (store.get("lastOnline"), prev_uid):
        if not uid:
            continue
        a = store["accounts"].get(uid)
        if a and a.get("passwd"):
            return uid, a["passwd"]
    return None


def write_result(action, code, ok, message):
    """给界面/脚本读的一句话结论"""
    try:
        write_json(RESULT_FILE, {
            "action": action,
            "ok": bool(ok),
            "code": code,
            "message": message,
            "userId": load_config().get("userId", ""),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        log("写 last-result.json 失败: %s" % e)


def migrate_legacy_data():
    """首次运行时把老位置的账号继承过来。

    覆盖两种情况：上一版 exe 的 `%APPDATA%\\CampusLogin`，以及最老的
    PowerShell 版 `C:\\tools`。目标是**幂等**的：只要新位置已经有 config.json
    就直接返回，不会覆盖用户当前的设置。
    """
    if os.path.isfile(CONFIG_FILE):
        return False
    dst = os.path.abspath(data_dir())
    for src in legacy_data_dirs():
        if not src or os.path.abspath(src) == dst:
            continue
        old_cfg = read_json(os.path.join(src, "config.json"))
        if not isinstance(old_cfg, dict):
            continue
        cfg = dict(DEFAULTS)
        cfg["userId"] = old_cfg.get("userId", "") or ""
        cfg["passwd"] = old_cfg.get("passwd", "") or ""
        for k in DEFAULTS:
            if old_cfg.get(k):
                cfg[k] = old_cfg[k]
        save_config(cfg)
        old_acc = read_json(os.path.join(src, "accounts.json"))
        if isinstance(old_acc, dict) and isinstance(old_acc.get("accounts"), dict):
            save_accounts(old_acc)
        log("已从 %s 继承旧配置" % src)
        return True
    return False


def purge_local_data():
    """清除本机保存的账号密码等隐私数据。返回 (数据目录, 实际删掉的文件名)。

    只删 PRIVATE_FILES 里明确列出的文件 —— 不用通配符、不递归删目录，
    免得误伤。数据目录本身留着（里面已经空了，下次运行会照常重建）。
    """
    d = data_dir()
    removed, failed = [], []
    for n in PRIVATE_FILES:
        p = os.path.join(d, n)
        if not os.path.isfile(p):
            continue
        try:
            os.remove(p)
            removed.append(n)
        except Exception as e:
            failed.append("%s(%s)" % (n, e))
    return d, removed, failed


# ==================== 网络 ====================
# 只用标准库 urllib，不用 requests。
#
# 为什么值得换：requests 会连带拉进 urllib3 / charset_normalizer / certifi，
# 更要命的是它会 import ssl —— PyInstaller 一旦看到 ssl，就会把
# libcrypto-3.dll + libssl-3.dll（解压后约 5.8MB）一起打进来。
# 而本程序所有地址都是 http://（portal、1.1.1.1、msftconnecttest 全是明文），
# 根本用不到 TLS。换掉之后包体直接小一大截，每次启动的解压量也跟着少。
class Response(object):
    """把 urllib 的响应包成 requests 那样好用的形状。

    关键差异：requests 遇到 4xx/5xx **不抛异常**，urllib 会抛 HTTPError。
    为了保持原来的判定逻辑（看 status_code 而不是靠异常），这里把
    HTTPError 也当成正常响应收下。
    """

    def __init__(self, fp):
        self.url = fp.geturl()
        code = getattr(fp, "status", None)
        if code is None:
            code = getattr(fp, "code", None)
        if code is None:
            try:
                code = fp.getcode()
            except Exception:
                code = 0
        self.status_code = code
        raw = fp.read()
        enc = None
        try:
            enc = fp.headers.get_content_charset()
        except Exception:
            enc = None
        for e in (enc, "utf-8", "gbk"):
            if not e:
                continue
            try:
                self.text = raw.decode(e)
                break
            except Exception:
                continue
        else:
            self.text = raw.decode("utf-8", "replace")


class Session(object):
    """最小会话：cookie 罐 + 固定请求头。够这个 portal 流程用。"""

    def __init__(self, headers=None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = dict(headers or {})

    def request(self, url, data=None, timeout=10):
        body = None
        if data is not None:
            body = (bytes(data) if isinstance(data, (bytes, bytearray))
                    else urllib.parse.urlencode(data).encode("utf-8"))
        req = urllib.request.Request(url, data=body, headers=dict(self.headers))
        try:
            fp = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            fp = e                      # 见类注释：4xx/5xx 当正常响应处理
        return Response(fp)

    def get(self, url, timeout=10):
        return self.request(url, timeout=timeout)

    def post(self, url, data=None, timeout=10):
        return self.request(url, data=data, timeout=timeout)


def http_get(url, timeout=10, headers=None):
    return Session(headers).get(url, timeout=timeout)


def http_post(url, data=None, timeout=10, headers=None):
    return Session(headers).post(url, data=data, timeout=timeout)


def _probe(url, expect, timeout):
    """单点探测。返回 True=通 / False=被门户拦截 / None=连不上或判定不了。

    「有响应但内容不对」才说明是被门户劫持了（拿到的是认证页而不是期望内容），
    这才是「未认证」的确凿证据；连不上/超时只能说明「测不出来」。
    把这两种混为一谈，就会出现「明明能上网却报未连接」。
    """
    try:
        r = http_get(url, timeout=timeout, headers={"User-Agent": UA})
    except Exception:
        return None
    code = r.status_code
    if expect:
        if code != 200:
            return None
        return expect in r.text
    # 只看状态码的目标（generate_204 这类）。不返回 False：这类目标没有正文
    # 可以校验，拿到别的状态码未必是被劫持，不能当作「未认证」的证据。
    if code == 204 or (code == 200 and not r.text.strip()):
        return True
    return None


def test_internet(timeout=None, rounds=1):
    """检测「当前是否已能上网」，返回三态：

        True  —— 已联网
        False —— 被门户拦截（拿到响应但内容不对，即确实未认证）
        None  —— 判定不了（请求异常 / 超时 / 状态码异常）

    为什么要三态：调用方对「确定没认证」和「没测出来」的处理必须不同。把后者
    当成「没认证」，轻则状态显示错误，重则误触发登录、或在切换账号时跳过下线
    步骤（见 do_switch），所以宁可不给结论也不要给错结论。
    """
    cfg = load_config()
    t = timeout or int(cfg.get("timeoutSec") or 8)
    # 每个目标的超时上限：总时间不随目标数量线性膨胀，最坏约 4 × per。
    # 上限给到 5 秒是为了扛住首次 DNS 解析慢（实测冷启动 7.4 秒、之后 0.2 秒）。
    per = min(5.0, max(2.5, t * 0.5))
    saw_blocked = False
    for i in range(max(1, rounds)):
        if i:
            time.sleep(0.4)
        for url, expect, _name in CHECK_TARGETS:
            r = _probe(url, expect, per)
            if r is True:
                return True
            if r is False:
                saw_blocked = True
    # 「明确被拦截」的证据优先于「测不出来」：门户劫持整片流量时，有正文校验的
    # 目标会返回 200 + 认证页（判 False），而 generate_204 这类目标拿到的状态码
    # 不是 204（判 None）。要是让 None 盖过 False，就会把「确实没认证」误报成
    # 「测不出来」，进而让 do_auto 不去登录、用户上不了网。
    return False if saw_blocked else None


def need_logout_before_switch(net):
    """切换账号前是否需要先下线当前账号。

    只有「明确判定为未认证」才跳过下线；「已联网」和「测不出来」都要先下线。
    跳过下线的代价是切换静默失败（当前账号还在线，服务端拒绝新登录，而
    portal_login 只看「登录后能否上网」，能上网就报成功），比多下一次线大得多。
    """
    return net is not False


def local_ipv4():
    """借 UDP connect 拿到本机用于访问 portal 的出口 IP（不发包）"""
    host = urlparse(load_config().get("portalHost") or DEFAULTS["portalHost"]).hostname
    if not host:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def local_mac():
    ip = local_ipv4()
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            v4 = [a.address for a in addrs if a.family == socket.AF_INET]
            if ip and ip not in v4:
                continue
            for a in addrs:
                if a.family == psutil.AF_LINK and a.address and a.address != "00:00:00:00:00:00":
                    return a.address.replace("-", ":").lower()
    except Exception:
        pass
    try:
        out = subprocess.run(["getmac", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=8, creationflags=CREATE_NO_WINDOW).stdout
        m = re.search(r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})", out)
        if m:
            return m.group(1).replace("-", ":").lower()
    except Exception:
        pass
    return ""


def wait_for_campus(max_seconds=60):
    """轮询等待拿到校园网段（10.*）的地址"""
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        ip = local_ipv4()
        if ip and ip.startswith("10."):
            return True
        time.sleep(0.5)
    return False


def url_parameter():
    cfg = load_config()
    return urlencode({
        "wlanacip": cfg.get("wlanAcIp", ""),
        "wlanacname": cfg.get("wlanAcName", ""),
        "wlanuserip": local_ipv4() or "",
        "mac": local_mac(),
        "vlan": "0",
        "url": TRIGGER_URL,
    })


# ==================== portal ====================
LOGIN_EXTRA = {
    "scheme": "http", "serverIp": "tomcat_server:80", "hostIp": "http://127.0.0.1:8080/",
    "loginType": "auth_type", "isBindMac1": "0", "pageid": "5", "templatetype": "1",
    "listbindmac": "0", "recordmac": "0", "isRemind": "1",
    "loginTimes": "", "groupId": "", "distoken": "", "echostr": "",
    "isautoauth": "", "mobile": "",
    "notice_pic_loop1": "/portal/uploads/pc/demo3/images/logo.jpg",
    "notice_pic_loop2": "/portal/uploads/pc/demo3/images/rrs_bg.jpg",
    "remInfo": "on", "desc_lb": "on",
}


def portal_login(uid, pwd):
    cfg = load_config()
    host = cfg.get("portalHost") or DEFAULTS["portalHost"]
    sess = Session({"User-Agent": UA})

    final_url = None
    try:
        r = sess.get(TRIGGER_URL, timeout=10)   # urllib 默认就会跟随重定向
        final_url = r.url
    except Exception as e:
        log("GET 异常: %s" % e)
    log("GET 最终地址: %s" % (final_url or ""))

    p = None
    if final_url and "webauth.do?" in final_url:
        try:
            q = parse_qs(urlparse(final_url).query, keep_blank_values=True)
            p = {k: (v[0] if v else "") for k, v in q.items()}
        except Exception:
            p = None
    if not p:
        log("未从重定向拿到参数，改用本机信息构造")
        p = {
            "wlanacip": cfg.get("wlanAcIp", ""),
            "wlanacname": cfg.get("wlanAcName", ""),
            "wlanuserip": local_ipv4() or "",
            "mac": local_mac(),
            "vlan": "0",
            "url": TRIGGER_URL,
        }

    p = dict(p)
    for k, v in LOGIN_EXTRA.items():
        p.setdefault(k, v)
    p["userId"] = uid
    p["passwd"] = pwd
    p.setdefault("url", TRIGGER_URL)

    query = urlencode({k: p.get(k, "") for k in
                       ("wlanacip", "wlanacname", "wlanuserip", "mac", "vlan", "url")})
    post_url = "%s/webauth.do?%s" % (host, query)
    log("POST %s" % post_url)
    try:
        r = sess.post(post_url, data=p, timeout=15)
        log("POST 状态: %s  长度: %s" % (r.status_code, len(r.text)))
    except Exception as e:
        log("POST 异常: %s" % e)

    # 门户建立会话要几秒。原来只 sleep 2 秒就单次判定，太急：
    # 实测 12:50:07 POST 返回 200、12:50:09 就判「认证后仍不通」并报登录失败，
    # 而网络其实几秒后就通了（门户重定向里甚至已经带着 act=LOGINSUCC）。
    # 结果是「明明登上了却报失败」—— switch 场景下还会触发一次没必要的回滚。
    # 改成隔几秒多探几次再下结论：正常 1 次就过，真的失败才会走满全程。
    for i, delay in enumerate((2, 3, 3, 4, 4), 1):
        time.sleep(delay)
        net = test_internet(timeout=5)
        if net is True:
            log("认证后第 %d 次探测：已连通" % i)
            return True
        log("认证后第 %d 次探测：%s" % (i, "仍未连通" if net is False else "无法判定"))
    return False


def check_password(uid, pwd):
    """只读校验：True 正确 / False 密码错 / None 无法判断"""
    cfg = load_config()
    host = cfg.get("portalHost") or DEFAULTS["portalHost"]
    try:
        r = http_post("%s/httpservice/checkUserPwdGeneral.do" % host,
                      data={"userId": uid, "passwd": pwd, "pageid": "5"},
                      timeout=10, headers={"User-Agent": UA})
        m = re.search(r'"check"\s*:\s*"(\d+)"', r.text)
        if m:
            log("密码校验 %s -> check=%s" % (uid, m.group(1)))
            if m.group(1) == "0":
                return True
            if m.group(1) == "3":
                return False
    except Exception as e:
        log("密码校验请求失败: %s" % e)
    return None


def _auth_page(uid, pwd):
    """POST webauth.do，取回「认证后页面」的 HTML。失败返回 ""。

    这个页面是很多信息的来源：distoken（下线要用）、connUserId（当前在线
    账号）。已在线时服务端会回「此IP已在线请勿重复认证」，但页面照样下发。
    """
    cfg = load_config()
    host = cfg.get("portalHost") or DEFAULTS["portalHost"]
    urlp = url_parameter()
    sess = Session({"User-Agent": UA})
    try:
        sess.get("%s/webauth.do?%s" % (host, urlp), timeout=10)
    except Exception:
        pass
    form = {
        "scheme": "http", "serverIp": "tomcat_server:80", "hostIp": "http://127.0.0.1:8080/",
        "loginType": "auth_type", "isBindMac1": "0", "pageid": "5", "templatetype": "1",
        "listbindmac": "0", "recordmac": "0", "isRemind": "1",
        "loginTimes": "", "groupId": "", "distoken": "", "echostr": "",
        "isautoauth": "", "mobile": "",
        "notice_pic_loop1": "/portal/uploads/pc/demo3/images/logo.jpg",
        "notice_pic_loop2": "/portal/uploads/pc/demo3/images/rrs_bg.jpg",
        "userId": uid, "passwd": pwd, "remInfo": "on", "desc_lb": "on",
    }
    try:
        r = sess.post("%s/webauth.do?%s" % (host, urlp), data=form, timeout=20)
    except Exception as e:
        log("取认证页异常: %s" % e)
        return ""
    return r.text or ""


def request_distoken(uid, pwd):
    text = _auth_page(uid, pwd)
    m = (re.search(r'id="distoken"[^>]*value="([^"]*)"', text) or
         re.search(r'name="distoken"[^>]*value="([^"]*)"', text))
    return m.group(1) if (m and m.group(1)) else None


def current_online_user():
    """当前在线账号（认证后页面里的 connUserId）。拿不到返回 ""。

    用来避免「切到自己已经在用的账号」时白下一次线再登一次 —— 那中间会断网
    好几秒，而结果和原来完全一样。
    """
    store = load_accounts()
    tries = []
    lo = store.get("lastOnline")
    if lo and lo in store["accounts"] and store["accounts"][lo].get("passwd"):
        tries.append((lo, store["accounts"][lo]["passwd"]))
    cfg = load_config()
    if cfg.get("userId") and cfg.get("passwd"):
        tries.append((cfg["userId"], cfg["passwd"]))
    for uid, pwd in tries:
        m = re.search(r'id="connUserId"[^>]*value="([^"]*)"', _auth_page(uid, pwd))
        if m and m.group(1).strip():
            log("当前在线账号: %s" % m.group(1).strip())
            return m.group(1).strip()
    return ""


def get_distoken():
    store = load_accounts()
    tries = []
    lo = store.get("lastOnline")
    if lo and lo in store["accounts"] and store["accounts"][lo].get("passwd"):
        tries.append((lo, store["accounts"][lo]["passwd"]))
    cfg = load_config()
    if cfg.get("userId") and cfg.get("passwd"):
        tries.append((cfg["userId"], cfg["passwd"]))
    for uid, pwd in tries:
        tok = request_distoken(uid, pwd)
        if tok:
            log("distoken 取自账号 %s" % uid)
            return tok
    return None


def portal_logout():
    """复刻认证后页面「离线」按钮：POST /webdisconn.do?<urlParameter>，
    必须带 other1=disconn 和 distoken"""
    cfg = load_config()
    host = cfg.get("portalHost") or DEFAULTS["portalHost"]
    token = get_distoken()
    if not token:
        log("未能取得 distoken，无法下线")
        return False
    log("已取得 distoken")
    form = {
        "scheme": "http", "serverIp": "tomcat_server:80", "hostIp": "http://127.0.0.1:8080/",
        "loginType": "auth_type", "auth_type": "0", "isBindMac1": "0", "pageid": "5",
        "templatetype": "1", "listbindmac": "0", "recordmac": "0", "isRemind": "1",
        "loginTimes": "", "groupId": "", "distoken": token, "echostr": "",
        "isautoauth": "", "mobile": "",
        "notice_pic_loop1": "/portal/uploads/pc/demo3/images/logo.jpg",
        "notice_pic_loop2": "/portal/uploads/pc/demo3/images/rrs_bg.jpg",
        "url": TRIGGER_URL, "userId": cfg.get("userId", ""), "other1": "disconn",
    }
    post_url = "%s/webdisconn.do?%s" % (host, url_parameter())
    log("下线请求(POST): %s" % post_url)
    try:
        r = http_post(post_url, data=form, timeout=15, headers={"User-Agent": UA})
        txt = re.sub(r"(?s)<script.*?</script>", " ", r.text)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()[:120]
        log("  状态 %s  正文: %s" % (r.status_code, txt))
    except Exception as e:
        log("  异常: %s" % e)
    time.sleep(3)
    # 只有「明确还能上网」才算下线未生效；测不出来时按已下线处理（与原来的
    # `not test_internet()` 一致，但那时单点失败会被当成下线成功，现在多地址
    # 探测基本不会漏掉「其实还在线」的情况）。
    if test_internet(timeout=5) is not True:
        log("已确认下线")
        return True
    log("  仍可联网，下线未生效")
    return False


# ==================== 业务动作 ====================
def do_auto():
    cfg = load_config()
    uid = cfg.get("userId") or ""
    pwd = cfg.get("passwd") or ""
    log("=== 开始检查 (Action=auto, 账号=%s) ===" % uid)
    if not uid or not pwd:
        log("账号或密码为空，退出")
        write_result("auto", "ERR_CONFIG", False, "账号或密码为空")
        return 4
    if not wait_for_campus(60):
        log("60 秒内未检测到校园网，退出")
        write_result("auto", "NO_GATEWAY", False, "未检测到校园网，请确认已连接校园网 Wi-Fi")
        return 4
    log("校园网就绪")
    net = test_internet()
    if net is True:
        log("网络已通，无需认证")
        write_result("auto", "OK", True, "网络已连接")
        return 0
    log("联网检测：%s，开始登录" % ("未认证" if net is False else "无法判定"))
    ok = portal_login(uid, pwd)
    update_account_record(uid, pwd, ok)
    if ok:
        log("=== 认证成功 ===")
        write_result("auto", "OK", True, "网络已连接")
        return 0
    log("=== 认证后仍不通 ===")
    pw_ok = check_password(uid, pwd)
    msg = "登录失败，请检查密码是否正确" if pw_ok is False else "登录失败，网络异常，请稍后重试"
    write_result("auto", "LOGIN_FAILED", False, msg)
    return 2


def do_logout():
    cfg = load_config()
    log("=== 开始检查 (Action=logout, 账号=%s) ===" % cfg.get("userId", ""))
    if not wait_for_campus(60):
        write_result("logout", "NO_GATEWAY", False, "未检测到校园网，请确认已连接校园网 Wi-Fi")
        return 4
    # 只有明确判定为「被门户拦截」才认为本来就未联网；测不出来时照样尝试下线
    # （用户点这个按钮的意图就是要下线，不该因为一次检测失败就不动作）。
    if test_internet() is False:
        log("本来就未联网，无需下线")
        set_last_online("")
        write_result("logout", "OK", True, "当前未登录校园网")
        return 0
    if portal_logout():
        log("=== 已下线 ===")
        set_last_online("")
        write_result("logout", "OK", True, "已退出当前账号")
        return 0
    log("=== 下线失败 ===")
    write_result("logout", "LOGOUT_FAILED", False, "退出失败，请稍后重试")
    return 3


def do_switch(uid, pwd, prev_uid):
    log("=== 开始检查 (Action=switch, 账号=%s, PrevUserId=%s) ===" % (uid, prev_uid))
    if not wait_for_campus(60):
        write_result("switch", "NO_GATEWAY", False, "未检测到校园网，请确认已连接校园网 Wi-Fi")
        return 4

    online = test_internet()
    log("当前联网状态: %s" % {True: "已联网", False: "未认证", None: "无法判定"}[online])

    # 已经在用目标账号时直接返回：下线再登同一个账号，中间会断网好几秒，结果却
    # 和现在完全一样。用户点了「切换到我的账号」而本来就在用这个账号（状态栏误报
    # 「未连接」之后很容易这么操作），没必要让他经历一次无谓的重连。
    #
    # 这里刻意不动 accounts.json 里存的密码：那需要用服务端校验过的密码去更新，
    # 而此刻表单里的密码还没被验证过，写进去可能把好密码覆盖成错的。
    if online is True:
        cur = current_online_user()
        if cur and cur == uid:
            log("当前在线账号已经是 %s，无需切换" % uid)
            set_last_online(uid)
            write_result("switch", "OK", True, "当前已在使用该账号，无需切换")
            return 0

    rollback = resolve_rollback(prev_uid)
    log("回滚目标: %s" % (rollback[0] if rollback else "无（没有可用的历史账号记录）"))

    # 只有「明确判定为未认证」才跳过下线，测不出来时按「可能在线」处理。
    #
    # 原来这里是 `if online:`：检测一旦误判成未联网就跳过下线，直接登录目标账号。
    # 但当前账号实际还在线时，服务端会回「此IP已在线请勿重复认证」，目标账号根本
    # 没登上去；而 portal_login 只看「登录后能否上网」，能上网就报成功 —— 于是
    # 切换静默失败（在线账号没变，界面却显示切换成功）。
    # 多下一次线最多浪费几秒，跳过下线却会让切换假装成功，所以这里取前者。
    if not need_logout_before_switch(online):
        log("当前未认证，直接登录目标账号")
    else:
        log("切换账号：先下线当前账号")
        if not portal_logout():
            log("=== 下线失败：当前账号仍在线，已中止切换 ===")
            write_result("switch", "LOGOUT_FAILED", False, "切换失败：当前账号仍在线，请稍后重试")
            return 3
        time.sleep(2)

    log("开始登录 %s" % uid)
    if portal_login(uid, pwd):
        update_account_record(uid, pwd, True)
        log("=== 切换成功 ===")
        write_result("switch", "OK", True, "切换成功")
        return 0

    update_account_record(uid, pwd, False)
    log("=== 切换失败：%s 登录后仍无法上网 ===" % uid)
    pw_ok = check_password(uid, pwd)

    if not rollback:
        log("没有可回滚的账号记录，无法恢复原账号")
        write_result("switch", "ROLLBACK_FAILED", False, "切换失败，且没有可恢复的原账号，请手动登录")
        return 2

    log("尝试回滚到原账号 %s" % rollback[0])
    if portal_login(rollback[0], rollback[1]):
        set_active_account(rollback[0], rollback[1])
        set_last_online(rollback[0])
        log("=== 已恢复原账号 %s ===" % rollback[0])
        msg = "切换失败，请检查密码是否正确" if pw_ok is False else "切换失败，网络异常，请稍后重试"
        write_result("switch", "SWITCH_FAILED", False, msg)
        return 2

    log("=== 回滚失败：原账号 %s 也登录不上 ===" % rollback[0])
    write_result("switch", "ROLLBACK_FAILED", False, "切换失败，且原账号未能恢复，请手动登录")
    return 2


# ==================== 开机自启 ====================
AUTOSTART_LNK_NAME = "校园网自动登录.lnk"
# 老版本 / 手工建的可能用这些名字
LEGACY_LNK_NAMES = (
    "campus-login.bat - 快捷方式.lnk",
    "campus-login.lnk",
    "campus-login.exe - 快捷方式.lnk",
    "校园网自动登录 - 快捷方式.lnk",
)


def _lnk_target(path):
    """读 .lnk 的目标。
    注意：pylnk3 的解析器对「中文 / 长文件名」的段重建有 bug（会少最后一段），
    所以这里的结果只能当辅助线索，不能当唯一依据。"""
    try:
        import pylnk3
        with open(path, "rb") as f:
            return pylnk3.parse(f).path or ""
    except Exception:
        return ""


def autostart_links():
    """启动文件夹里所有属于本程序的自启快捷方式。

    判定顺序（前面的更可靠）：
      1) 文件名就是我们建的 / 老版本用的名字
      2) 解析出的目标就是本 exe、或指向老的 campus-login.bat/.exe
      3) 解析出的目标正好是本 exe 所在目录（应对用户自己改了快捷方式名字）
    """
    d = startup_dir()
    out = []
    if not os.path.isdir(d):
        return out
    me = app_path().lower()
    mydir = os.path.dirname(me)
    legacy = tuple(n.lower() for n in LEGACY_LNK_NAMES)
    for name in os.listdir(d):
        low = name.lower()
        if not low.endswith(".lnk"):
            continue
        p = os.path.join(d, name)
        if name == AUTOSTART_LNK_NAME or low in legacy:
            out.append(p)
            continue
        tg = (_lnk_target(p) or "").lower()
        if tg and (tg == me or tg == mydir
                   or tg.endswith("campus-login.bat") or tg.endswith("campus-login.exe")):
            out.append(p)
    return out


def is_autostart_on():
    return len(autostart_links()) > 0


def _lnk_points_to_me(path):
    """这个 .lnk 是不是真的指向**当前这个 exe**。True / False，读不出来返回 None。

    **不能用 pylnk3 判断**：它的解析器对中文路径会丢最后一段（实测把
    `...\\Desktop\\校园网自动登录.exe` 解析成 `...\\Desktop`），拿它做「指向谁」的
    判断必然误判。快捷方式里的路径是以 **UTF-16LE 明文**存的，直接搜原始字节最可靠。

    只认**完整路径**，不认「目录相同」—— 旧位置 `Desktop\\校园网自动登录\\...` 的
    字节流里本来就含 `Desktop`，认目录会把失效的链接判成有效。
    """
    me = app_path()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None                      # 读不出来 = 不知道，不妄断
    if me.encode("utf-16-le") in raw:
        return True
    # 大小写 / 斜杠写法可能不同，退一步做规范化比较
    for off in (0, 1):
        text = raw[off:].decode("utf-16-le", "ignore").replace("/", "\\").lower()
        if os.path.normcase(me).replace("/", "\\").lower() in text:
            return True
    return False


def autostart_stale():
    """开机自启是不是「勾打着、其实已经失效」。

    为什么需要这个：快捷方式里存的是**绝对路径**，把 exe 挪个位置它就指不到了；
    而 `autostart_links()` 的判定里有「文件名就是我们建的那个」这条，所以哪怕目标
    早就不存在，`is_autostart_on()` 照样返回 True —— 用户看到勾是打着的，以为开机
    没问题，实际开机什么都不会发生。2026-09-17 就是这么坏的（exe 从桌面子文件夹
    移到桌面根目录，`.lnk` 还指着旧路径）。

    源码运行时恒返回 False：那时 `app_path()` 是脚本路径，本来就不该有指向它的自启
    链接，而且 `set_autostart()` 也会拒绝。
    """
    if not is_frozen():
        return False
    links = autostart_links()
    if not links:
        return False
    return all(_lnk_points_to_me(p) is False for p in links)


def make_lnk(lnk_path, target, arguments="", icon=None, work_dir=None):
    """写一个 Windows 快捷方式。

    坑：**不要**给 LinkInfo 塞非 ASCII 路径。pylnk3 的 LinkInfo 用固定代码页
    （DEFAULT_CHARSET = cp1251）写 local_base_path，目标路径里只要有中文就会抛
    UnicodeEncodeError。目标路径交给 Shell Link 的 IDList 承载即可——IDList 原生
    就是 UTF-16，中文没问题，Windows 也照样能解析（已实测）。
    """
    from pylnk3 import for_file
    lnk = for_file(target, arguments=(arguments or None), icon_file=icon,
                   icon_index=0, work_dir=work_dir, description=APP_TITLE)
    lnk.save(lnk_path)


def set_autostart(on):
    """开/关开机自启。返回 (ok, 说明)"""
    d = startup_dir()
    if not os.path.isdir(d):
        return False, "找不到启动文件夹"
    try:
        links = autostart_links()
        if on:
            exe = app_path()
            if not is_frozen():
                return False, "源码运行时无法设置自启"
            # 清掉旧的（可能指向 .bat），统一换成指向 exe 的
            for p in links:
                try:
                    os.remove(p)
                except Exception:
                    pass
            lnk_path = os.path.join(d, AUTOSTART_LNK_NAME)
            make_lnk(lnk_path, exe, arguments="--auto", icon=exe, work_dir=os.path.dirname(exe))
            return (True, "") if os.path.isfile(lnk_path) else (False, "快捷方式未生成")
        for p in links:
            os.remove(p)
        return (True, "") if not is_autostart_on() else (False, "快捷方式未能删除")
    except Exception as e:
        return False, str(e)


# ==================== 使用说明 ====================
# 说明文字直接写进程序，不再随 exe 附带 .txt —— 拷一个 exe 过去就能看到。
# 面向使用者：不写实现细节，短句、少术语，第一次用的人照着做就行。
# （排障用的 --selftest 之类留在 --help 和日志里，不占说明篇幅。）
HELP_TEXT = r"""校园网自动登录 —— 使用说明

【第一次使用】

  1. 填上你的校园网账号和密码
  2. 点「仅保存」
  3. 勾选最下面的「开机时自动登录校园网」

  以后开机就会自动联网，不用再管。


【三个按钮】

  切换到此账号
      换账号时用。现在登的是别人的号，点它换成你的。
      没切成功会自动退回原来的账号，不会让你卡在断网状态。
      本来就在用这个账号的话，会直接告诉你，不会白断一次网。

  仅保存
      只记住账号密码，不马上切换。

  退出当前账号
      主动下线。下线后本机暂时上不了网。


【密码框怎么用】

  密码默认是看不到的，显示成一串小星号，旁边的人瞄一眼也看不出内容。

  正在输入时，刚敲进去的那一个字符会露出来一下，方便你确认有没有打错，
  前面的字符仍然是星号。

  想看完整密码，点密码框右边的「显示」，再点一下「隐藏」就盖回去。
  鼠标点到别处、或者关掉窗口，密码会重新变回星号。

  从别处复制密码粘进来，用 Ctrl+V。想改中间的字，先退格删掉再重打
  （输入位置固定在末尾）。


【顶部的网络状态】

  绿点  已连接校园网     可以正常上网。
  红点  未连接校园网     被校园网的登录页挡住了，需要登录。
  灰点  网络状态未知     这次没检测出来，不代表没网，
                        点「切换到此账号」试一次。


【账号密码存在哪】

  存在这台电脑的  C:\ProgramData\CampusLogin  里，不在这个程序里。

  所以把「校园网自动登录.exe」这一个文件拷给别人，
  不会带出你的账号、密码和登录记录。

  想在本机彻底清掉账号密码，在命令行执行：
      校园网自动登录.exe --purge


【注意】

  · 只适用于本校校园网。
  · 部分杀毒软件可能误报，加白名单即可。
  · 主动退出账号后，校园网通常几十秒内会自动重新连上，
    这是学校那边的设置，不是故障。
  · 这个 exe 放在哪个文件夹都能正常用，文件夹名带中文、带空格也没关系；
    账号密码存在系统目录里，不跟着 exe 走。
  · 但如果把 exe 挪到别的位置，「开机自启」会失效 —— 自启快捷方式记的是原来的
    位置。这时界面会在「开机自启」下面提示你，把那个勾取消、再重新勾一次就好。
  · 出问题时看日志：C:\ProgramData\CampusLogin\campus-login.log
"""


# ==================== 密码框的显示与编辑规则 ====================
# 抽成纯函数，是为了能脱离 Tk 直接测 —— 受控会话里建窗口不可靠，而这段
# 「哪个键改密码、怎么改、该显示成什么」的逻辑恰恰最容易写错。

def pwd_display_text(real, reveal=False, focused=False):
    """密码框此刻该显示什么。

    规则：点了「显示」就一直明文；否则只在有焦点（正在输入）时露出最后敲进去
    的那一个字符，其余打码；没焦点时全部打码。
    """
    if reveal:
        return real
    if not real:
        return ""
    if focused:
        return "*" * (len(real) - 1) + real[-1]
    return "*" * len(real)


def pwd_apply_key(real, keysym, char, ctrl=False, clip=None, selected=False):
    """按下一个键之后真实密码应该变成什么。返回 (新密码, handled)。

    handled=True  —— 这个键被密码框接管了，调用方要拦住 Entry 的默认处理
    handled=False —— 交回 Entry 默认行为（方向键、Tab 等）

    clip：按下 Ctrl+V 时剪贴板的内容（由调用方去读，这里不碰 Tk）。
    selected：当前是否有选中内容（有的话 BackSpace 是清空而不是退一格）。
    """
    # 「按着 Ctrl 且这个键不产生可打印字符」才算快捷键。
    # 为什么要多这一个条件：Windows 上 AltGr（右 Alt，欧洲键盘打 @ € 等用的）
    # 在 Tk 里报告成 Ctrl+Alt，只看 Ctrl 位会把用户正常输入的字符当成快捷键吃掉，
    # 那些字符就永远打不进密码框。而真正的 Ctrl 快捷键（Ctrl+V 是 \x16、
    # Ctrl+A 是 \x01）产生的一定是不可打印字符，所以这个条件能准确区分两者。
    if ctrl and not (char and char.isprintable()):
        if keysym.lower() == "v":
            paste = (clip or "").replace("\r", "").replace("\n", "")
            return real + paste, True
        if keysym.lower() == "a":
            return real, False          # 放行，允许全选
        return real, True               # 复制/剪切等一律忽略，别把密码带走
    if keysym == "BackSpace":
        return ("" if selected else real[:-1]), True
    if keysym == "Delete":
        return real, True               # 插入点固定在末尾，Delete 没有意义
    if char and char.isprintable():
        return real + char, True
    return real, False


class PasswordField(object):
    """带隐私保护的密码框。

    三条规则：输入时只露出刚敲进去的那一个字符、右侧「显示」按钮看全部、
    没在输入时全部打码。

    tkinter 的 Entry 只有「全打码」(show="*") 和「全明文」两种，做不到「只留
    最后一个」，所以这里不让 Entry 自己管文本 —— 拦下键盘事件自己维护真实密码，
    再按规则算出该显示什么。代价是插入点固定在末尾，和手机上密码框行为一致。

    抽成独立的类（而不是塞在 run_gui 里当嵌套函数），是为了能单独建一个窗口
    测它的真实交互，而不只是测那两条纯规则。
    """

    def __init__(self, parent, tk, font_main, font_small, fg):
        self.real = ""
        self.reveal = False
        self.focused = False

        self.box = tk.Frame(parent, bg="#ffffff", highlightbackground="#c9ced6",
                            highlightthickness=1, bd=0)
        self.entry = tk.Entry(self.box, font=font_main, relief="flat", bd=0,
                              bg="#ffffff", fg=fg, highlightthickness=0,
                              insertbackground=fg)
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(7, 0))
        self.button = tk.Button(self.box, text="显示", font=font_small,
                                relief="flat", bd=0, bg="#ffffff", fg="#0b5ed7",
                                activebackground="#eef4fb", activeforeground="#0b5ed7",
                                cursor="hand2", padx=8, command=self.toggle_reveal)
        self.button.pack(side="right", padx=(4, 6), ipady=6)

        self.entry.bind("<KeyPress>", self._on_key)
        self.entry.bind("<FocusIn>", lambda e: self.set_focus(True))
        self.entry.bind("<FocusOut>", lambda e: self.set_focus(False))
        # 鼠标点进来之后把插入点拉回末尾，免得选中一段打码文本造成误解
        self.entry.bind("<Button-1>", lambda e: self.entry.after(1, self._to_end))

        # 堵住「不经过键盘」的改内容路径。
        # 为什么必须堵：Entry 的类绑定里有 <<Paste>> / <<Cut>> / <<Clear>> /
        # <<PasteSelection>>（中键粘贴），它们**不产生 KeyPress**，会直接把文本
        # 塞进控件。那样 self.real 就和显示内容脱节了 —— 用户看着密码框里字是对的，
        # 点按钮却登录失败，而且完全看不出原因。
        # 键盘路径本身是安全的：Tk 先跑 widget 绑定，<KeyPress> 里 return "break"
        # 会截断后面的类绑定，所以 Ctrl+V 走的是我们自己的处理。
        # 实测 <Button-3>（右键菜单）在这个 Tk 里没有绑定，不用管。
        self.entry.bind("<<Paste>>", self._on_paste)
        for seq in ("<<Cut>>", "<<Clear>>", "<<PasteSelection>>", "<Button-2>"):
            self.entry.bind(seq, self._block)

    # --- 内部 ---
    def _to_end(self):
        try:
            self.entry.icursor("end")
            self.entry.selection_clear()
        except Exception:
            pass

    def _read_clip(self):
        """读剪贴板文本，读不到返回 None（空剪贴板会抛 TclError）。"""
        try:
            return self.entry.clipboard_get()
        except Exception:
            return None

    def _on_paste(self, ev=None):
        """<<Paste>> 虚拟事件（中键粘贴等）—— 走我们自己的处理，不走 Entry 默认行为。"""
        self.paste()
        return "break"

    def _block(self, ev=None):
        """拦掉会绕过按键处理的改内容路径（剪切 / 清空 / 中键粘贴）。"""
        return "break"

    def _on_key(self, ev):
        ctrl = bool(ev.state & 0x0004)           # Windows 上 Ctrl 就是这个位
        clip = None
        if ctrl and ev.keysym.lower() == "v":
            clip = self._read_clip()
        try:
            selected = bool(self.entry.selection_present())
        except Exception:
            selected = False
        new, handled = pwd_apply_key(self.real, ev.keysym, ev.char,
                                     ctrl=ctrl, clip=clip, selected=selected)
        if handled:
            if new != self.real:
                self.set(new)
            return "break"
        return None                              # 方向键、Tab 等交给默认行为

    # --- 对外 ---
    def paste(self):
        """把剪贴板内容接到密码末尾（Ctrl+V 和 <<Paste>> 共用这一份实现）。"""
        new, _ = pwd_apply_key(self.real, "v", "", ctrl=True, clip=self._read_clip())
        if new != self.real:
            self.set(new)

    def text(self):
        """此刻该显示的内容"""
        return pwd_display_text(self.real, self.reveal, self.focused)

    def draw(self):
        txt = self.text()
        if self.entry.get() != txt:              # 没变就别动，免得光标乱跳
            self.entry.delete(0, "end")
            self.entry.insert(0, txt)
        if self.focused and not self.reveal:
            self._to_end()

    def get(self):
        """真实密码（不是显示出来的那串星号）"""
        return self.real

    def set(self, real):
        self.real = real
        self.draw()

    def set_focus(self, on):
        self.focused = on
        self.draw()

    def toggle_reveal(self):
        self.reveal = not self.reveal
        self.button.configure(text="隐藏" if self.reveal else "显示")
        self.draw()
        try:
            self.entry.focus_set()               # 焦点还给输入框，方便接着输入
        except Exception:
            pass


# 接线自检失败的固定标识。**build.py 也从这里导入**，不各写一份 ——
# 两处硬编码同一个字符串迟早会漂移，那时门槛就静默失效了（找不到这个串，
# 于是永远不拦）。改这句话之前先确认 build.py 引用的是这个常量而不是字面量。
WIRING_FAIL_PHRASE = "密码框接线自检失败"

# 接线自检用的假密码。**必须是假字符串** —— 如果这里写真实密码，
# 它就会被打进 exe，`leak_check.py` 会（正确地）判定隐私检查不通过。
PWD_PROBE = "demo-1234"


def pwd_wiring_check(field, loaded=None):
    """在真实界面里验证密码框的接线：读到的是真密码，看到的是打码。

    为什么要有这一条：`pwd_test.py` 测的是两条纯规则，`pwd_gui_test.py` 测的是
    `PasswordField` 自己的绑定 —— 但「`run_gui` 有没有把 `form()` 接到真实密码上」
    这一层谁也测不到。它一旦接错（比如接到显示的那串星号上），表现就是
    **「密码框里字看着对、点按钮却登录失败」**，和密码填错长得一模一样，
    极难排查。所以在真界面里拿那个实例跑一遍。

    loaded：`load_all()` 从配置里读出来的密码（没有就传 None），
    用来确认「配置 → 密码框 → 读回」这条往返没丢东西。

    返回一句成功描述；有任何一项不符就抛 AssertionError。
    """
    fails = []

    def want(name, got, expect):
        if got != expect:
            fails.append("%s: 得到 %r，期望 %r" % (name, got, expect))

    n = len(PWD_PROBE)
    field.set_focus(False)
    field.set(PWD_PROBE)
    want("失焦时应全打码", field.entry.get(), "*" * n)
    want("失焦时 get() 应给真实密码", field.get(), PWD_PROBE)

    field.set_focus(True)
    want("聚焦时应只露最后一位", field.entry.get(), "*" * (n - 1) + PWD_PROBE[-1])
    want("聚焦时 get() 仍是真实密码", field.get(), PWD_PROBE)

    field.toggle_reveal()
    want("点「显示」后应为明文", field.entry.get(), PWD_PROBE)
    want("点「显示」后 get() 仍是真实密码", field.get(), PWD_PROBE)
    field.toggle_reveal()
    want("再点一次应回到打码", field.entry.get(), "*" * (n - 1) + PWD_PROBE[-1])

    if loaded:
        field.set_focus(False)
        field.set(loaded)
        want("配置里的密码应原样读回", field.get(), loaded)
        want("配置密码失焦后应全打码", field.entry.get(), "*" * len(loaded))

    field.set_focus(False)
    if fails:
        raise AssertionError(WIRING_FAIL_PHRASE + ":\n  " + "\n  ".join(fails))
    return "密码框接线自检通过（显示打码 / get() 给真密码 / 配置往返一致）"


# ==================== 窗口几何 ====================
# 抽成纯函数是为了能测：这段算错在受控会话里很难复现（要改屏幕分辨率），
# 而它恰恰是「小屏幕上底部控件够不到」这类问题的根源。

OUTER_PAD = 18      # 最外层留白
INNER_PAD = 20      # 卡片内留白
EXTRA_H = 40        # 给标题栏/边框留的余量
SCREEN_MARGIN_H = 80    # 给任务栏 + 标题栏留的余量


def window_geometry(sw, sh, reqh, reqw=0):
    """按屏幕尺寸和内容需要的高度算窗口几何，返回 (w, h, x, y)。

    **必须按屏幕收口。** 原来的写法只把高度限制在 560..1000，完全没看屏幕多高 ——
    在 1366x768、或者 1080p 开了 150% 缩放（虚拟化后只剩 720 高）这类屏幕上，
    窗口会比屏幕还高。要是界面又没滚动条，底部的「开机自启」就永远够不到，
    那是**真的没法开自启**，不只是难看。
    """
    max_w = max(360, sw - 40)
    max_h = max(320, sh - SCREEN_MARGIN_H)

    w = max(820, min(1180, int(sw * 0.44)))
    h = max(560, min(1000, reqh + EXTRA_H))
    w = min(w, max_w)
    h = min(h, max_h)          # 屏幕装不下就按屏幕来，宁可小也不要跑出屏幕

    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)
    return w, h, x, y


def minsize_for(sw, sh):
    """最小尺寸同样要按屏幕收口，否则小屏幕上用户连缩都缩不动。"""
    return (min(700, max(360, sw - 40)),
            min(520, max(320, sh - SCREEN_MARGIN_H)))


# ==================== 界面 ====================
def run_gui(smoke=False):
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import messagebox

    BG = "#f0f2f5"
    CARD = "#ffffff"
    FG = "#222222"
    SUB = "#666666"
    BLUE = "#0078d4"

    root = tk.Tk()
    if smoke:
        root.withdraw()          # 自检时不显示窗口
    root.title("%s · 设置" % APP_TITLE)
    root.configure(bg=BG)

    # 按屏幕高度决定字号，避免高分屏上字太小
    try:
        sh = root.winfo_screenheight()
    except Exception:
        sh = 1080
    BASE = 12 if sh >= 1400 else 10
    TITLE = BASE + 5
    SMALL = BASE - 2

    fam = "Microsoft YaHei UI"
    try:
        if fam not in tkfont.families():
            fam = "Microsoft YaHei"
    except Exception:
        fam = "Microsoft YaHei"

    root.option_add("*Font", (fam, BASE))
    ico = icon_path()
    if ico:
        try:
            root.iconbitmap(default=ico)
        except Exception:
            pass

    def lbl(parent, text, size=None, bold=False, fg=FG, bg=CARD, **kw):
        return tk.Label(parent, text=text, bg=bg, fg=fg,
                        font=(fam, size or BASE, "bold" if bold else "normal"), **kw)

    outer = tk.Frame(root, bg=BG)
    outer.pack(fill="both", expand=True, padx=OUTER_PAD, pady=OUTER_PAD)
    card = tk.Frame(outer, bg=CARD, highlightthickness=0)
    card.pack(fill="both", expand=True)

    # 内容放进可滚动区域。屏幕矮的时候（1366x768、1080p@150%…）内容会比窗口高，
    # 没有滚动的话底部「开机自启」就永远够不到 —— 那是真的没法开自启，不是难看。
    canvas = tk.Canvas(card, bg=CARD, highlightthickness=0, bd=0)
    vsb = tk.Scrollbar(card, command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)

    holder = tk.Frame(canvas, bg=CARD)
    inner = tk.Frame(holder, bg=CARD)
    inner.pack(fill="both", expand=True, padx=22, pady=INNER_PAD)
    holder_id = canvas.create_window((0, 0), window=holder, anchor="nw")

    holder.bind("<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    # 让 holder 始终和 canvas 一样宽，否则内容不会跟着窗口变宽
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfigure(holder_id, width=e.width))

    def on_wheel(ev):
        # 内容比视口高才滚，否则别抢事件（短内容时滚轮该干嘛干嘛）
        if holder.winfo_reqheight() > canvas.winfo_height():
            canvas.yview_scroll(-1 if ev.delta > 0 else 1, "units")

    # 绑在 toplevel 上：鼠标在任意子控件上滚都会冒到这一层，不用逐个控件去绑
    # （账号列表是动态重建的，逐个绑必然漏）。而且只影响这个窗口 ——
    # 说明弹窗是另一个 Toplevel，不受影响。
    root.bind("<MouseWheel>", on_wheel)

    # 标题行：「使用说明」放最右边，紧挨着窗口右上角的最小化按钮下方
    head = tk.Frame(inner, bg=CARD)
    head.pack(fill="x", pady=(0, 14))
    lbl(head, "%s · 设置" % APP_TITLE, TITLE, True).pack(side="left")
    tk.Button(head, text="使用说明", font=(fam, SMALL), bg="#eef0f3", fg="#333",
              activebackground="#e0e4ea", activeforeground="#333",
              relief="flat", bd=0, cursor="hand2", padx=14, pady=3,
              command=lambda: show_help()).pack(side="right")

    # --- 状态条 ---
    status = tk.Frame(inner, bg="#eef4fb")
    status.pack(fill="x", pady=(0, 16))
    dot = tk.Canvas(status, width=12, height=12, bg="#eef4fb", highlightthickness=0)
    dot.pack(side="left", padx=(14, 10), pady=11)
    dot_id = dot.create_oval(0, 0, 11, 11, fill="#bbbbbb", outline="")
    status_text = lbl(status, "正在检测网络状态…", BASE, bg="#eef4fb", fg="#0c5460")
    status_text.pack(side="left", pady=11)

    # --- 账号列表 ---
    lbl(inner, "已保存的账号", SMALL, True, SUB).pack(anchor="w", pady=(0, 6))
    list_box = tk.Frame(inner, bg=CARD, highlightbackground="#e3e6ea", highlightthickness=1)
    list_box.pack(fill="x")
    list_inner = tk.Frame(list_box, bg=CARD)
    list_inner.pack(fill="x")

    # --- 表单 ---
    lbl(inner, "账号信息", SMALL, True, SUB).pack(anchor="w", pady=(16, 2))
    lbl(inner, "学号 / 账号", BASE, fg="#444").pack(anchor="w", pady=(8, 0))
    var_uid = tk.StringVar()
    ent_uid = tk.Entry(inner, textvariable=var_uid, font=(fam, BASE), relief="solid", bd=1)
    ent_uid.pack(fill="x", ipady=6, pady=(4, 0))
    lbl(inner, "密码", BASE, fg="#444").pack(anchor="w", pady=(10, 0))

    # --- 密码框（带隐私保护）---
    # 规则都在 PasswordField 里（那样才能单独建窗口测交互），这里只负责摆放。
    pwd_field = PasswordField(inner, tk, (fam, BASE), (fam, SMALL), FG)
    pwd_field.box.pack(fill="x", pady=(4, 0))

    # --- 按钮（从上到下排列，每个占一整行）---
    def mkbtn(parent, text, cmd, kind="ghost"):
        style = {
            "primary": (BLUE, "#ffffff", "#106ebe"),
            "ghost":   ("#e9edf2", "#333333", "#dde3ea"),
            "danger":  ("#fdeceb", "#c0392b", "#fbdcd9"),
        }[kind]
        b = tk.Button(parent, text=text, command=cmd, font=(fam, BASE),
                      bg=style[0], fg=style[1], activebackground=style[2],
                      activeforeground=style[1], relief="flat", bd=0, cursor="hand2",
                      disabledforeground="#9aa0a6")
        b.pack(fill="x", pady=(0, 8), ipady=9)
        return b

    btns = tk.Frame(inner, bg=CARD)
    btns.pack(fill="x", pady=(18, 0))
    btn_switch = mkbtn(btns, "切换到此账号", lambda: on_switch(), "primary")
    btn_save = mkbtn(btns, "仅保存", lambda: on_save())
    btn_out = mkbtn(btns, "退出当前账号", lambda: on_logout(), "danger")

    # --- 消息 ---
    msg = lbl(inner, "", BASE, bg="#e8f2fb", fg="#0c5460", anchor="w", justify="left",
              wraplength=760)
    msg.pack(fill="x", ipady=10, pady=(16, 0))
    msg.pack_forget()

    # --- 开机自启 ---
    tk.Frame(inner, bg="#eef0f3", height=1).pack(fill="x", pady=(20, 14))
    lbl(inner, "开机自启", SMALL, True, SUB).pack(anchor="w", pady=(0, 6))
    var_auto = tk.BooleanVar()
    chk = tk.Checkbutton(inner, text="开机时自动登录校园网", variable=var_auto,
                         command=lambda: on_toggle_autostart(), bg=CARD, fg="#333",
                         activebackground=CARD, font=(fam, BASE), anchor="w",
                         selectcolor="#ffffff", cursor="hand2")
    chk.pack(anchor="w")

    # 「开机自启」勾打着、但快捷方式其实已失效时的提示。
    # 快捷方式存的是绝对路径，挪动 exe 就会指不到；而光看勾选状态发现不了，
    # 所以必须把这种静默失败显式说出来。默认不显示。
    warn_auto = lbl(inner, "", SMALL, False, "#b45309", justify="left", wraplength=700)

    def refresh_auto_warning():
        try:
            stale = autostart_stale()
        except Exception:
            stale = False
        if stale:
            warn_auto.configure(
                text="⚠ 开机自启的快捷方式指向的是 exe 的**旧位置**，开机时实际不会生效。\n"
                     "   把下面的勾取消、再重新勾选一次即可修好。")
            warn_auto.pack(anchor="w", pady=(6, 0))
        else:
            warn_auto.pack_forget()

    # --- 状态 ---
    state = {"busy": False, "queue": None, "buttons": None}

    def show(text, kind="info"):
        color = {"ok": ("#e6f6ea", "#155724"),
                 "err": ("#fdeceb", "#721c24"),
                 "info": ("#e8f2fb", "#0c5460")}[kind]
        msg.configure(text=text, bg=color[0], fg=color[1])
        msg.pack(fill="x", ipady=10, pady=(16, 0))

    def set_busy(b):
        state["busy"] = b
        st = "disabled" if b else "normal"
        for w in (btn_switch, btn_save, btn_out):
            w.configure(state=st)

    def refresh_status():
        def work():
            on = test_internet()
            # 程序刚起来时第一个网络请求赶上 DNS 冷启动（实测首次 7.4 秒），
            # 有可能「测不出来」。隔几秒再试一次，免得状态栏一直停在未知上
            # ——用户打开程序第一眼看到的就是这行状态。
            if on is None:
                time.sleep(3)
                on = test_internet()
            state["queue"].put(("status", on))
        threading.Thread(target=work, daemon=True).start()

    def render_accounts():
        for w in list_inner.winfo_children():
            w.destroy()
        store = load_accounts()
        cfg = load_config()
        cur = cfg.get("userId") or ""
        ids = list(store["accounts"].keys())
        ids.sort(key=lambda k: (store["accounts"][k].get("lastSuccess") or "", k), reverse=True)

        if not ids:
            lbl(list_inner, "暂无保存的账号", SMALL, fg="#999").pack(anchor="w", padx=14, pady=12)
            return
        for i, uid in enumerate(ids):
            a = store["accounts"][uid]
            row = tk.Frame(list_inner, bg=CARD)
            row.pack(fill="x")
            if i:
                tk.Frame(list_inner, bg="#eef0f3", height=1).pack(fill="x")
            left = tk.Frame(row, bg=CARD)
            left.pack(side="left", fill="x", expand=True, padx=14, pady=9)
            line = tk.Frame(left, bg=CARD)
            line.pack(anchor="w")
            lbl(line, uid, BASE, True).pack(side="left")
            if uid == cur:
                lbl(line, " 当前使用 ", SMALL - 1, fg="#0b5ed7", bg="#e7f0ff").pack(side="left", padx=(6, 0))
            if uid == store.get("lastOnline"):
                lbl(line, " 上次登录 ", SMALL - 1, fg="#1a7f37", bg="#e2f6e6").pack(side="left", padx=(4, 0))
            if a.get("lastSuccess"):
                lbl(left, "上次成功：%s" % a["lastSuccess"], SMALL - 1, fg="#888").pack(anchor="w")
            right = tk.Frame(row, bg=CARD)
            right.pack(side="right", padx=12)
            tk.Button(right, text="选用", font=(fam, SMALL), relief="solid", bd=1,
                      bg="#ffffff", cursor="hand2",
                      command=lambda u=uid: pick(u)).pack(side="left", padx=4)
            tk.Button(right, text="删除", font=(fam, SMALL), relief="solid", bd=1,
                      fg="#c0392b", bg="#ffffff", cursor="hand2",
                      command=lambda u=uid: drop(u)).pack(side="left", padx=4)

    def pick(uid):
        store = load_accounts()
        var_uid.set(uid)
        pwd_field.set(store["accounts"].get(uid, {}).get("passwd", ""))

    def drop(uid):
        if not messagebox.askyesno(APP_TITLE, "从列表中删除账号 %s 吗？" % uid):
            return
        store = load_accounts()
        store["accounts"].pop(uid, None)
        if store.get("lastOnline") == uid:
            store["lastOnline"] = ""
        save_accounts(store)
        render_accounts()

    def load_all():
        cfg = load_config()
        var_uid.set(cfg.get("userId") or "")
        pwd_field.set(cfg.get("passwd") or "")
        uid = cfg.get("userId")
        if uid:
            store = load_accounts()
            if uid not in store["accounts"]:
                store["accounts"][uid] = {"passwd": cfg.get("passwd") or "",
                                          "lastSuccess": "", "lastAttempt": "", "lastResult": ""}
                save_accounts(store)
        render_accounts()
        try:
            var_auto.set(is_autostart_on())
            chk.configure(state="normal")
            refresh_auto_warning()
        except Exception:
            var_auto.set(False)
            chk.configure(state="disabled")

    # --- 后台任务 ---
    def start_job(running_text, fn):
        if state["busy"]:
            return
        show(running_text, "info")
        set_busy(True)

        def work():
            try:
                r = fn()
            except Exception:
                log("任务异常:\n%s" % traceback.format_exc())
                r = (-1, "操作异常，请查看日志")
            state["queue"].put(("job", r))

        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                kind, payload = state["queue"].get_nowait()
                if kind == "status":
                    on = payload
                    if on is True:
                        dot.itemconfigure(dot_id, fill="#28a745")
                        status_text.configure(text="已连接校园网")
                    elif on is False:
                        dot.itemconfigure(dot_id, fill="#dc3545")
                        status_text.configure(text="未连接校园网")
                    else:
                        dot.itemconfigure(dot_id, fill="#bbbbbb")
                        status_text.configure(text="网络状态未知")
                elif kind == "job":
                    code, text = payload
                    set_busy(False)
                    load_all()
                    show(text, "ok" if code == 0 else "err")
                    refresh_status()
        except Exception:
            pass
        root.after(120, poll)

    # --- 按钮动作 ---
    def form():
        uid = var_uid.get().strip()
        pwd = pwd_field.get()
        if not uid:
            show("请填写账号", "err")
            return None
        if not pwd:
            show("请填写密码", "err")
            return None
        return uid, pwd

    def upsert(uid, pwd, overwrite):
        upsert_account(uid, pwd, overwrite)

    def on_save():
        f = form()
        if not f:
            return
        cfg = load_config()
        cfg["userId"], cfg["passwd"] = f
        save_config(cfg)
        upsert(f[0], f[1], True)
        load_all()
        show("已保存", "ok")

    def action_runner(fn):
        """跑一个 do_* 动作，把 (退出码, 给用户看的一句话) 交回主线程"""
        def run():
            code = fn()
            r = read_json(RESULT_FILE) or {}
            return code, (r.get("message") or "操作完成")
        return run

    def on_switch():
        f = form()
        if not f:
            return
        uid, pwd = f
        prev = load_config().get("userId") or ""
        cfg = load_config()
        cfg["userId"], cfg["passwd"] = uid, pwd
        save_config(cfg)
        upsert(uid, pwd, False)            # 不覆盖已存密码，回滚要靠它
        start_job("切换账号中，可能需要等待十几秒…",
                  action_runner(lambda: do_switch(uid, pwd, prev)))

    def show_help():
        """使用说明弹窗。

        说明文字内置在 HELP_TEXT 里，所以程序不依赖外部 .txt —— 单独一个 exe
        拷到别的电脑上，照样点得开说明。
        """
        win = tk.Toplevel(root)
        win.title("使用说明 · %s" % APP_TITLE)
        win.configure(bg=CARD)
        win.transient(root)              # 跟随主窗口，不单独占一个任务栏项
        ico = icon_path()
        if ico:
            try:
                win.iconbitmap(default=ico)
            except Exception:
                pass

        body = tk.Frame(win, bg=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=(14, 0))
        txt = tk.Text(body, wrap="word", font=(fam, BASE), bg=CARD, fg=FG,
                      relief="flat", bd=0, padx=6, pady=2,
                      spacing1=1, spacing3=1, cursor="arrow")
        sb = tk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")  # 只读；仍可选中复制

        bar = tk.Frame(win, bg=CARD)
        bar.pack(fill="x", padx=16, pady=14)
        tk.Button(bar, text="知道了", font=(fam, BASE), bg=BLUE, fg="#ffffff",
                  activebackground="#106ebe", activeforeground="#ffffff",
                  relief="flat", bd=0, cursor="hand2", padx=26, pady=6,
                  command=win.destroy).pack(side="right")

        if smoke:
            win.withdraw()               # 自检时不真的弹出来
        else:
            win.update_idletasks()
            w = min(720, max(520, int(root.winfo_width() * 0.92)))
            h = min(700, max(440, int(root.winfo_height() * 0.92)))
            x = root.winfo_rootx() + (root.winfo_width() - w) // 2
            y = root.winfo_rooty() + (root.winfo_height() - h) // 3
            win.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))
            win.minsize(420, 320)
            win.focus_set()
        return win

    def on_logout():
        if not messagebox.askyesno(APP_TITLE, "确定退出当前校园网账号吗？\n\n退出后本机将无法上网。"):
            return
        start_job("正在退出当前账号…", action_runner(do_logout))

    def on_toggle_autostart():
        want = var_auto.get()
        ok, why = set_autostart(want)
        if not ok:
            var_auto.set(not want)
            show("设置失败：%s" % why, "err")
            return
        refresh_auto_warning()      # 重新勾选之后，警告要跟着消失
        show("已开启开机自启" if want else "已关闭开机自启", "ok")

    # --- 启动 ---
    import queue as _q
    state["queue"] = _q.Queue()
    import tkinter.font as _f
    root.option_add("*Font", (fam, BASE))

    load_all()
    root.after(120, poll)
    root.after(60, refresh_status)

    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    # 内容需要多高：holder 是滚动区里那层（含卡片内留白），再加上最外层留白。
    # **不要在这里加 EXTRA_H** —— window_geometry 自己会加，加两次就白高 40px。
    # 也别用 root.winfo_reqheight()：内容进了 canvas 之后它就不再反映内容高度了。
    reqh = holder.winfo_reqheight() + 2 * OUTER_PAD
    w, h, x, y = window_geometry(sw, sh, reqh)
    # 内容装不下才挂滚动条。一次性决定，不做动态显隐 —— 动态 pack/pack_forget
    # 会改变 canvas 宽度、又触发 <Configure>，容易自己把自己绕进去。
    if reqh > h:
        vsb.pack(side="right", fill="y", before=canvas)
    root.geometry("%dx%d+%d+%d" % (w, h, x, y))
    root.minsize(*minsize_for(sw, sh))
    if smoke:
        root.update_idletasks()
        # 密码框接线自检：确认界面里读到的是真密码、看到的是打码。
        # 只有真建了窗口、拿到 run_gui 里那个实例才测得到（见 pwd_wiring_check）。
        # 失败就抛出去 —— 让 --guitest 落成 GUI_FAIL，别静默放过。
        wiring = pwd_wiring_check(pwd_field, (load_config().get("passwd") or None))
        log(wiring)
        # 顺便把说明弹窗也建一次，确认它能起来（不显示，建完就销毁）
        try:
            hv = show_help()
            hv.update_idletasks()
            hv.destroy()
        except Exception:
            log("说明弹窗自检失败:\n%s" % traceback.format_exc())
        # 自检路径不走 mainloop，显式销毁，别把 Tk 留给解释器收尾
        try:
            root.destroy()
        except Exception:
            pass
        return 0
    root.mainloop()
    # mainloop 返回后**不要再走解释器正常收尾**：--windowed 打包后 Tk 在收尾阶段
    # 偶发死锁（界面已经关掉、文件也落盘了，进程却一直不退，实测卡过 10 分钟以上），
    # 结果是留一个看不见的僵尸进程在那儿占着。
    # 只在**打包后**硬退：从源码跑时（ui_shot.py / layout_probe.py 会调 run_gui）
    # 要让它正常返回，否则脚本自己的收尾输出会被这一下截掉。
    if is_frozen():
        exit_now(0)
    return 0


# ==================== 入口 ====================
def exit_now(code):
    """立刻结束进程，不走 Python 的正常收尾。

    坑：--windowed 打包后，Tk 在解释器收尾阶段偶发死锁——界面已经建好、
    结果文件也落盘了，进程却一直不退出（实测卡住 10 分钟以上，父进程
    subprocess.run 永远等不到它）。诊断/静默这几条路径本来就不跑 mainloop，
    没必要走正常收尾，直接 os._exit。

    安全性：所有文件写入都用 `with open(...)` 或 `os.replace` 即时落盘，
    没有待刷的缓冲；onefile 的 _MEI 临时目录由 bootloader 父进程负责清理，
    子进程硬退不影响。
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def main():
    # --windowed 打包后 stdout/stderr 是 None，某些库会因此报错
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    args = [a.lower() for a in sys.argv[1:]]

    # 必须在继承旧数据**之前**处理：否则刚继承进来的账号又被这里删掉，
    # 顺序反了会让人以为清除失败（或者以为继承成功）。
    if "--purge" in args:
        d, removed, failed = purge_local_data()
        print("已清除本机保存的账号数据：%s" % ("、".join(removed) if removed else "（本来就没有）"))
        if failed:
            print("以下文件删除失败：%s" % "、".join(failed))
        print("数据目录: %s" % d)
        exit_now(1 if failed else 0)

    try:
        migrate_legacy_data()
    except Exception:
        pass

    if "--selftest" in args:
        common = (_shell_folder(CSIDL_COMMON_APPDATA) or "").lower()
        dd = data_dir()
        if common and dd.lower().startswith(common):
            where = "C:\\ProgramData（机器级）"
        else:
            where = "%APPDATA%（ProgramData 不可写，已退回）"
        try:
            n_acct = len(load_accounts()["accounts"])
        except Exception:
            n_acct = -1
        info = [
            "版本: %s" % VERSION,
            "打包: %s" % is_frozen(),
            "程序路径: %s" % app_path(),
            "数据目录: %s" % dd,
            "数据目录来源: %s" % where,
            "配置存在: %s" % os.path.isfile(CONFIG_FILE),
            "已保存账号数: %s" % n_acct,
            "启动文件夹: %s" % startup_dir(),
            "自启链接: %s" % autostart_links(),
            "本机IP: %s" % local_ipv4(),
            "本机MAC: %s" % local_mac(),
            "联网: %s" % {True: "已联网", False: "未认证（被门户拦截）",
                          None: "无法判定（请求异常/超时）"}[test_internet()],
        ]
        try:
            import psutil
            info.append("psutil: 有")
        except Exception:
            info.append("psutil: 无")
        text = "\n".join(info)
        print(text)
        try:
            with open(os.path.join(data_dir(), "selftest.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        exit_now(0)

    if "--guitest" in args:
        # 无窗口地构建一遍界面，用来确认 tcl/tk 运行库打包完整。
        # 注意：--windowed 打包后没有 stdout，所以结果要落到文件里才验得到。
        try:
            run_gui(smoke=True)
            result = "GUI_OK"
            code = 0
        except Exception:
            result = "GUI_FAIL\n%s" % traceback.format_exc()
            code = 1
            log("GUI 自检异常:\n%s" % traceback.format_exc())
        print(result)
        try:
            with open(os.path.join(data_dir(), "guitest.txt"), "w", encoding="utf-8") as f:
                f.write(result)
        except Exception:
            pass
        exit_now(code)

    if "--switch" in args:
        # 命令行切换账号：密码从 accounts.json 里取（没存过就退回当前 config）。
        # 主要给排障/脚本用，平时走设置页按钮。
        try:
            i = args.index("--switch")
            uid = args[i + 1] if i + 1 < len(args) else ""
            if not uid:
                print("用法: 校园网自动登录.exe --switch <学号>")
                exit_now(2)
            acct = (load_accounts().get("accounts") or {}).get(uid) or {}
            pwd = acct.get("passwd") or ""
            cur = load_config()
            if not pwd and cur.get("userId") == uid:
                pwd = cur.get("passwd") or ""
            if not pwd:
                write_result("switch", "ERR_CONFIG", False,
                             "没有账号 %s 的密码，请先在设置页保存一次" % uid)
                exit_now(1)
            prev = cur.get("userId") or ""
            cfg = load_config()
            cfg["userId"], cfg["passwd"] = uid, pwd
            save_config(cfg)
            upsert_account(uid, pwd, False)      # 不覆盖已存密码，回滚要靠它
            exit_now(do_switch(uid, pwd, prev))
        except Exception:
            log("switch 异常:\n%s" % traceback.format_exc())
            exit_now(1)
    if "--auto" in args:
        try:
            exit_now(do_auto())
        except Exception:
            log("auto 异常:\n%s" % traceback.format_exc())
            exit_now(1)
    if "--logout" in args:
        try:
            exit_now(do_logout())
        except Exception:
            log("logout 异常:\n%s" % traceback.format_exc())
            exit_now(1)

    try:
        run_gui()
    except Exception:
        log("GUI 异常:\n%s" % traceback.format_exc())
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP_TITLE, "程序出错，详情见：\n%s" % LOG_FILE)
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
