# -*- coding: utf-8 -*-
"""
校园网自动登录（单文件版）
  - 不带参数        : 打开设置界面
  - --auto          : 静默模式（开机自启用），未联网才登录，不弹任何窗口
  - --guard         : 静默登录后继续守一段时间，掉线自动重连（到点自己退出）
  - --logout        : 静默下线
  - --switch <学号> : 静默切换到指定账号（密码取自 accounts.json）
  - --selftest      : 环境自检，结果打印并写入数据目录 selftest.txt
  - --guitest       : 只构建界面不显示，结果写入数据目录 guitest.txt
  - --purge         : 清除本机保存的账号密码等隐私数据
  - --version (-v)  : 只打印版本号就退出（打包后没有 stdout，改为弹窗）

开机自启用的是 `--auto --guard`（见 AUTOSTART_ARGS）。
自启入口是系统「任务计划程序」里一个叫 CampusLogin 的**登录触发**任务
（见 AUTOSTART_TASK_NAME 那段：为什么不用启动文件夹 —— 差约 60 秒）。
建不了任务时才退回启动文件夹里的 .lnk。

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
import queue
import threading
import subprocess
import tempfile
import traceback
import http.cookiejar
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

# 检查更新模块。纯标准库，不引入新依赖（打包体积的三条手段一条都不破坏）。
# ⚠️ 顶层 import 而不是延迟 import：updater 只 import 标准库，开销可以忽略，
#    但**打包时必须让 PyInstaller 看见它** —— 藏在函数里的 import 容易被漏掉。
import updater

APP_NAME = "CampusLogin"
APP_TITLE = "校园网自动登录"

# 对外版本号，语义化版本：主版本.次版本.修订号
#   主版本：不兼容的改动（改配置格式、换认证协议）
#   次版本：加了功能但向后兼容
#   修订号：只修 bug
#
# ⚠️ 这里是**唯一来源**。界面标题、使用说明、--version、--selftest、
#    以及 exe 文件属性里的版本，全部由它推导 —— 不要在别处另写一份，
#    否则迟早漂移（改了一处忘了另一处，用户看到的版本号就是错的）。
VERSION = "0.3.3"

# 学校 portal 默认参数（拷到同校其他电脑上可直接用）
DEFAULTS = {
    "portalHost": "http://202.196.169.166",
    "wlanAcIp": "10.0.1.10",
    "wlanAcName": "ZZHK-ZAX-BRAS",
    "timeoutSec": 8,
}

# 校园网给终端分配的网段前缀。**判断「在不在校园网」用它，不用「能不能上网」**。
#
# 为什么必须分开（2026-09-19 修的 bug）：
#   原来界面直接把 test_internet() 的结果翻译成「已连接校园网」，
#   而 test_internet() 测的是「能不能上公网」—— 于是**在宿舍/家用 Wi-Fi 上
#   也显示「已连接校园网」**，用户根本分不清自己是连上了校园网还是别的网。
#   实测本校给的是 10.x（日志里 wlanuserip=10.15.116.156）。
#
# 写成可配置的：别的学校/别的校区网段不同，改 config.json 里的
# `campusSubnets` 就行，不用动代码。留空则用下面这个默认值。
DEFAULT_CAMPUS_SUBNETS = ("10.",)

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


# 同一个 IP 的「下线 / 登录」这类会改变门户会话状态的动作，一次只许一个跑。
#
# 为什么需要：界面的忙碌标志（state["busy"]）是**每个窗口各自**的普通布尔，
# 而后台任务跑在另一个线程里。程序**没有单实例闸门**（双击图标、托盘再点一次
# 都会起新进程），所以同时开两个窗口时：
#     窗口 A 点「切换账号」→ 后台线程开始下线
#     窗口 B 点「退出当前账号」→ 它自己的 busy 是 False，按钮没灰，于是也能点
# 两个动作各拿着同一个账号的密码去要 distoken、再各发一次下线请求。
# 谁先下线谁成功，后来那个面对的是一个**已经不在线的 IP**，门户不会回
# 「下线成功」，于是界面弹「退出失败，请稍后重试」——用户什么都没做错。
#
# 这是**进程内**的锁。多窗口其实是同一个进程里的多个顶层窗口，所以进程内锁
# 就能拦住它们；两个独立的 exe 进程拦不住 —— 那种情况由 portal_logout 的
# 重试 + 三态判定兜底（重复下线时门户回「未在线」，已按成功处理）。
_LOGOUT_LOCK = threading.Lock()

# 拿不到锁时等多久（秒）。等到了就按顺序执行，等不到就直接告诉用户
# 「另一个操作正在执行」——**不要**默默排队：两个「退出」排下来第二个必然
# 面对已下线的 IP，那正是这个 bug 的现象。
_LOGOUT_LOCK_WAIT = 8.0


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
        with open(probe, "w", encoding="utf-8") as f:
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
# 守护进程的「有人在跑」标记。里面记 pid 和启动时刻。
# ⚠️ 这个文件**可能被残留**：退出走的是 TerminateProcess（见 exit_now），
#    finally 和 atexit 都不会执行，所以不能指望正常删除它。
#    靠 guard_running() 里的「过期」判定自愈，别改成「存在即视为在跑」。
GUARD_FLAG = os.path.join(data_dir(), "guard.json")

# ============ 开机自启的节奏参数 ============
# 「开机自启生效相当慢」的实测定位（2026-09-19，见 auto_timing_probe.py）：
#   正常一次 --auto 只要 **3 秒**；但网络没就绪时会白烧 80 秒超时
#   （联网检测 16s + GET 10s + POST 15s + 认证后探测 41s），然后**一次不成就永久放弃**。
#   所以「慢」的根因不是等网卡（70 次运行里 wait_for_campus 耗时全是 0 秒），
#   而是「超时预算太长 + 没有重试」。下面这几个参数就是照这个结论定的。
AUTO_ATTEMPTS = 3            # --auto 一共试几轮
AUTO_RETRY_BACKOFF = 5       # 每轮之间等几秒（第 N 轮等 N × 这个值）
AUTO_WAIT_FIRST = 60         # 第一轮等校园网的秒数（跟以前一样，别加大：
                             #   实测 wait_for_campus 从来没等过，加大只是让
                             #   「真的不在校园网」时多耗时间）
AUTO_WAIT_RETRY = 20         # 后续几轮只给 20 秒
AUTO_NET_TIMEOUT = 5         # --auto 里联网检测用的超时。默认配置是 8 秒，
                             #   换算成每个探测目标 4 秒 → 4 个目标全超时 = 16 秒。
                             #   给 5 秒 → 每个 2.5 秒 = 最多 10 秒。省下的 6 秒
                             #   在开机那一刻很值；网络正常时第一个探测就返回了，
                             #   这个上限根本用不到。
GUARD_SECONDS = 1800         # 守护总时长：30 分钟（用户指定，覆盖开机 + 上课这段）
GUARD_POLL = 30              # 每 30 秒看一次
GUARD_CONFIRM = 2            # 「测不出来」要连续几次才动手（避免被一次网络抖动骗到）
GUARD_FAIL_BACKOFF_MAX = 300 # 重连失败后的最大退避（秒）。没有它的话，网络真断了
                             #   会在半小时里攒出几十次无效登录，把日志刷满。
GUARD_RESPECT_LOGOUT = 7200  # 用户主动退出后，多久之内不去打扰他（2 小时）

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


# ============ 退出路径诊断（仅当 CAMPUS_EXIT_TRACE=1 时生效）============
# 为什么要这个：GUI 关闭后进程不退，光从外面看只能得出「它还活着」，
# 分不清是卡在 Tk 收尾（mainloop 没返回）还是卡在 os._exit 里头的
# ExitProcess。这里按阶段打点，每个点单独开一次文件——进程被硬杀也不丢。
# 默认关着，不影响正常用户；排查时设环境变量再跑。
def exit_trace(msg):
    if not os.environ.get("CAMPUS_EXIT_TRACE"):
        return
    try:
        line = "%s.%03d  pid=%s  %s\n" % (
            datetime.now().strftime("%H:%M:%S"),
            datetime.now().microsecond // 1000,
            os.getpid(), msg)
        with open(os.path.join(data_dir(), "exit-trace.log"), "a",
                  encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def arm_exit_watchdog():
    """20 秒后如果进程还活着，把所有线程的 Python 栈 dump 出来。

    这是「卡在 Python 层」和「卡在 C 层」的判别器：如果卡在 os._exit 的
    ExitProcess 里，faulthandler 的定时器线程早就被 ExitProcess 干掉了，
    这个文件会是空的 —— 什么都不 dump 本身就是答案。
    """
    if not os.environ.get("CAMPUS_EXIT_TRACE"):
        return
    try:
        import faulthandler
        f = open(os.path.join(data_dir(), "exit-stacks.log"), "a",
                 encoding="utf-8")
        faulthandler.enable(file=f, all_threads=True)
        faulthandler.dump_traceback_later(20, repeat=True, file=f, exit=False)
        exit_trace("看门狗已布防（20s 后 dump 线程栈）")
    except Exception:
        exit_trace("看门狗布防失败:\n%s" % traceback.format_exc())


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


def note_logout():
    """记下「用户是**主动**退出的」这件事。

    ⚠️ 为什么必须记时间（2026-09-19 加）：`--guard` 会在开机后 30 分钟里
    自动重连。如果用户在这段时间里主动点了「退出当前账号」，守护进程一看
    「在校园网但没认证」就把他又登回去 —— 用户会觉得这软件在跟他对着干。
    光看 lastOnline 是否为空判不出来（开机时它本来就可能是空的），
    所以单独记一个时刻，守护据此避让一段时间。
    """
    store = load_accounts()
    store["lastOnline"] = ""
    store["lastLogout"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_accounts(store)


def logged_out_recently(within=GUARD_RESPECT_LOGOUT):
    """用户在最近 `within` 秒内主动退出过吗。判不出来时返回 False（不阻拦）。"""
    t = (load_accounts().get("lastLogout") or "").strip()
    if not t:
        return False
    try:
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - dt).total_seconds() < within


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


def _direct_handlers():
    """本程序所有请求都要用的「**不走系统代理**」处理器。

    为什么必须显式关掉代理（2026-09-19 实测踩到，属于静默失败）：
        urllib 的默认 opener 会走系统代理 —— 它既读 `HTTP_PROXY` /
        `http_proxy` / `HTTPS_PROXY` 环境变量，**在 Windows 上还会读
        IE/WinINET 注册表里的代理设置**（getproxies()）。只要机器上装了
        Clash / v2ray 这类会把系统代理打开的软件，或者有人设过环境变量，
        本程序**所有**请求都会绕去那个代理。
        而校园网门户就在局域网里，根本不该走代理 —— 表现是：TCP 能连通、
        但 GET/POST 全部超时，登录必然失败，日志里只看得到
        `<urlopen error timed out>`，**完全看不出是代理造成的**。
        本机沙箱里就是 `HTTP_PROXY=http://127.0.0.1:10389` 导致全部超时，
        一度被误判成「校园网故障」。

    ⚠️ updater.py 早就这么做了（它开头第 14 行记着同一个坑：系统代理指向
       不可用的 127.0.0.1:7897 时客户端直接卡死）。**门户会话当时漏了**，
       于是「检查更新」正常、登录却全超时 —— 这种「一半能通」最容易查错方向。
       两处现在用同一个理由，改一处别忘了另一处。

    直连才是正确行为，不只是为了绕开沙箱：
      · 门户在校园网内网，走公网代理必然失败；
      · 公网探测目标（msftconnecttest 之类）也必须**直连**才有意义 ——
        走代理就绕过了门户劫持，`test_internet()` 会把「未认证」误判成「已联网」。

    ⚠️ 一个反直觉的细节，别把它当 bug「修」掉：
        `build_opener(ProxyHandler({}))` 之后，`opener.handlers` 里**看不到**
        ProxyHandler —— `OpenerDirector.add_handler` 会把 `proxy_open` 当作
        「名字撞车」跳过，于是 ProxyHandler 一个协议方法都不贡献，根本不进列表。
        效果正是我们要的（完全不查代理）。已实测：设了 `HTTP_PROXY` 指向一个
        不通的地址，本 opener 仍然 0.5 秒直连成功。
    """
    return [urllib.request.ProxyHandler({})]


class Session(object):
    """最小会话：cookie 罐 + 固定请求头。够这个 portal 流程用。"""

    def __init__(self, headers=None):
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        handlers += _direct_handlers()
        self.opener = urllib.request.build_opener(*handlers)
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


def campus_subnets():
    """校园网给终端分配的网段前缀列表。可被 config.json 的 `campusSubnets` 覆盖。"""
    cfg = load_config()
    v = cfg.get("campusSubnets")
    if isinstance(v, str):
        items = [s.strip() for s in v.replace(";", ",").split(",")]
    elif isinstance(v, (list, tuple)):
        items = [str(s).strip() for s in v]
    else:
        items = []
    items = [s for s in items if s]
    return items or list(DEFAULT_CAMPUS_SUBNETS)


def portal_reachable(timeout=3.0):
    """门户主机的 80 端口通不通。True / False。"""
    cfg = load_config()
    host = urlparse(cfg.get("portalHost") or DEFAULTS["portalHost"]).hostname
    if not host:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, 80))
        return True
    except Exception:
        return False
    finally:
        s.close()


def on_campus_network(timeout=3.0):
    """本机是不是接在**校园网**上。三态：True / False / None。

    ⚠️ 这**不是**「能不能上网」—— 那是 test_internet() 的事，两者必须分开。
       在宿舍/家用 Wi-Fi 上：test_internet() = True，但 on_campus_network() = False。
       原来界面把前者直接当成后者显示，才会「随便连个网都显示已连接校园网」。

    判据（满足任一即为真）：
      1) 本机出口 IPv4 落在配置的校园网段（默认 10.*）
      2) 门户主机 TCP 可达

    第 2 条是**兜底**：万一学校换了网段，第 1 条会失效，但门户仍然连得上，
    不会把「其实在校园网」误判成「不在」。
    ⚠️ 已知残留风险：若门户恰好从公网也连得上，那么在校外用 10.x 的家庭路由器时
    可能误报。缓解办法是把 `campusSubnets` 配得更精确（例如 `10.15.`）。
    """
    ip = local_ipv4()
    if ip and any(ip.startswith(p) for p in campus_subnets()):
        return True
    if portal_reachable(timeout):
        return True
    if not ip:
        return None                     # 连出口 IP 都拿不到 → 判不了
    return False


# 网络状态。**只在这里定义一次** —— 界面和命令行各写一套迟早会漂移。
NET_OFF_CAMPUS = "off_campus"       # 不在校园网（跟能不能上网无关）
NET_OK = "ok"                       # 在校园网且已认证
NET_NEED_LOGIN = "need_login"       # 在校园网但被门户挡住
NET_UNKNOWN = "unknown"             # 判定不了

NET_TEXT = {
    NET_OFF_CAMPUS: "未连接校园网",
    NET_OK: "已连接校园网",
    NET_NEED_LOGIN: "已连校园网，未认证",
    NET_UNKNOWN: "网络状态未知",
}

NET_COLOR = {
    NET_OFF_CAMPUS: "#bbbbbb",
    NET_OK: "#28a745",
    NET_NEED_LOGIN: "#dc3545",
    NET_UNKNOWN: "#bbbbbb",
}


def network_state(timeout=None):
    """一句话结论，界面和命令行共用。

    先把「在不在校园网」和「认证没认证」两件事分开，再合成状态 ——
    这样「不在校园网」就不会被误报成「已连接校园网」。
    """
    on = on_campus_network()
    if on is False:
        # 不在校园网 → 不管公网通不通，都**不能**说「已连接校园网」。
        return NET_OFF_CAMPUS
    net = test_internet(timeout=timeout)
    if net is True:
        # on 可能是 None（拿不到出口 IP）—— 那种情况不能断言「在校园网」，
        # 宁可说未知，也不给用户一个可能错的结论。
        return NET_OK if on is True else NET_UNKNOWN
    if net is False:
        return NET_NEED_LOGIN if on is True else NET_UNKNOWN
    return NET_UNKNOWN


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


_MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


def norm_mac(m):
    """把各种写法（大写 / 连字符 / 带空格）统一成 `aa:bb:cc:dd:ee:ff`。"""
    return (m or "").strip().replace("-", ":").replace(" ", "").lower()


def valid_mac(m):
    """这个字符串能不能当 MAC 用。

    为什么要专门校验（2026-09-19 实测踩到）：
        开机自启那次登录，**门户自己在重定向 URL 里给了 `mac=00:00:00:00:00:00`**，
        而 portal_login 优先信任门户给的参数，于是拿全零 MAC 去认证。认证能过
        （门户不看 MAC 就发会话），但 BRAS 之后按 ARP 表对账时对不上，**几分钟后
        把会话踢掉** —— 用户看到的就是「连上几分钟后莫名其妙掉线」。
        日志证据：2026-09-19 11:49:49 那次 POST 的 mac 就是全零。

    为什么全零要单独判：`00:00:00:00:00:00` 格式上是合法 MAC，
    但它表示「未知」，是网卡未初始化 / ARP 表还没建立时的占位值，永远不该发出去。
    """
    m = norm_mac(m)
    if not _MAC_RE.match(m):
        return False
    if m == "00:00:00:00:00:00":          # 未知
        return False
    if m == "ff:ff:ff:ff:ff:ff":          # 广播
        return False
    # 第一个字节的最低位 = 组播位。源 MAC 不可能是组播地址。
    # ⚠️ 只查这一位：0x02（本地管理位）是合法的 —— Wi-Fi Direct 虚拟网卡
    #    （Windows 的「本地连接* N」）用的就是它，实测本机有 86:9e:.. / 8a:9e:..
    if int(m[:2], 16) & 0x01:
        return False
    return True


def _mac_from_psutil(ip):
    """按「哪块网卡拥有这个出口 IP」挑 MAC。挑不到返回 ""。

    顺序很重要：先找 v4 与出口 IP 相同的那块网卡（那才是真正在跑流量的），
    找不到才退回「第一块有合法 MAC 的物理网卡」。
    ⚠️ 不能直接用第一块 —— 实测本机网卡顺序是
    `以太网 / 本地连接*1 / 本地连接*2 / 以太网 2 / WLAN`，
    第一块「以太网」是没插网线的 169.254 地址，MAC 是 BC-FC-E7-C7-84-AA，
    拿它去认证就是错的（真值在 WLAN 上）。
    """
    try:
        import psutil
    except Exception:
        return ""
    others = ""
    for name, addrs in psutil.net_if_addrs().items():
        v4 = [a.address for a in addrs if a.family == socket.AF_INET]
        mac = ""
        for a in addrs:
            if a.family == psutil.AF_LINK and valid_mac(a.address):
                mac = norm_mac(a.address)
                break
        if not mac:
            continue
        if ip and ip in v4:
            return mac                      # 命中出口 IP，直接用它
        if not others and not _is_virtual_nic(name):
            others = mac                    # 退而求其次：非虚拟网卡
    return others


def _is_virtual_nic(name):
    """名字像虚拟网卡吗（Wi-Fi Direct / 回环 / 蓝牙 / 隧道）。

    只在「挑不到出口 IP 对应网卡」时才用得上，用来避免把
    「本地连接* 1」这种 Wi-Fi Direct 虚拟网卡的 MAC 当成本机 MAC。
    """
    n = (name or "").lower()
    return any(k in n for k in ("本地连接*", "loopback", "bluetooth", "vethernet",
                                "vmware", "virtualbox", "tap-", "tunnel", "wi-fi direct"))


def local_mac():
    """本机用于访问校园网的网卡 MAC。取不到返回 ""（调用方必须能容忍）。

    ⚠️ 这个值直接决定认证能不能站住：门户/BRAS 用它标识终端。
    发错了（或发了全零）认证照样「成功」，但会话会在几分钟内被踢掉，
    而且界面上看不出任何异常 —— 属于最难查的一类故障。
    """
    ip = local_ipv4()
    m = _mac_from_psutil(ip)
    if m:
        return m
    try:
        # ⚠️ 不能加 text=True：getmac 的输出是 **GBK**，用 UTF-8 解会抛
        # UnicodeDecodeError，而且异常发生在读取线程里 —— stdout 直接变成 None，
        # 后面 re.search(pattern, None) 抛 TypeError，**整个输出全丢**，
        # 看起来就像「getmac 没输出」。实测本机必现。
        out = subprocess.run(["getmac", "/fo", "csv", "/nh"], capture_output=True,
                             timeout=8, creationflags=CREATE_NO_WINDOW).stdout or b""
        text = out.decode("gbk", errors="replace")
        # ⚠️ 不能取第一个匹配：列表里可能混着 00-00-00-00-00-00（断开的网卡），
        # 取到它就是那个「全零 MAC」故障。逐个验，取第一个**合法**的。
        for cand in re.findall(r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})", text):
            if valid_mac(cand):
                return norm_mac(cand)
        log("getmac 有输出但没有合法 MAC：%r" % text[:200])
    except Exception:
        log("getmac 取 MAC 失败:\n%s" % traceback.format_exc())
    # 两条路都失败 → 返回空串。**必须留痕**：这个空值会直接填进登录表单的
    # mac 字段，门户拿不到 MAC 就会拒绝认证，而界面只会显示一句含糊的失败，
    # 完全看不出是 MAC 没取到。这是"登录失败但毫无线索"的典型来源。
    log("取本机 MAC 失败：psutil 和 getmac 都没拿到，登录可能被门户拒绝")
    return ""


def wait_for_campus(max_seconds=60):
    """轮询等待接入校园网。

    判据与 on_campus_network() 一致（网段 或 门户可达），但这里要**便宜**：
    每 0.5 秒只看一次本机 IP（纯本地、不发包），每 5 秒才做一次门户 TCP 探测
    —— 门户探测要 3 秒超时，每次都做会让 60 秒的等待只够轮询十几次。
    """
    deadline = time.time() + max_seconds
    next_portal = 0.0
    while time.time() < deadline:
        ip = local_ipv4()
        if ip and any(ip.startswith(p) for p in campus_subnets()):
            return True
        now = time.time()
        if now >= next_portal:
            next_portal = now + 5.0
            if portal_reachable(2.0):
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

    # ⚠️ **门户给的 mac 不可信**（2026-09-19 实测踩到，就是「连上几分钟后掉线」的根因）。
    #    开机那次 GET，门户在重定向 URL 里给的是 `mac=00:00:00:00:00:00`
    #    （它自己也还没查到 ARP 表）。拿它去认证：门户照发会话、程序探测也显示
    #    「认证成功」，但 BRAS 之后按真实 MAC 对账时对不上 → 几分钟后踢掉会话。
    #    所以在发出去之前一律换成校验过的真值。
    got = p.get("mac") or ""
    if valid_mac(got):
        p["mac"] = norm_mac(got)
    else:
        real = local_mac()
        log("门户给的 MAC 无效(%r)，改用本机 MAC(%r)" % (got, real))
        if valid_mac(real):
            p["mac"] = real
        else:
            # 两边都拿不到合法 MAC。**仍然发出去**（能上网几分钟总比完全不能好），
            # 但必须留痕 —— 否则用户只会看到「连上又掉」，日志里毫无线索。
            log("⚠️ 门户和本机都没给出合法 MAC，认证后可能被踢下线")
            p["mac"] = real or got

    # 同理校验 wlanuserip：门户偶尔会给空值，空着填进表单必被拒。
    if not (p.get("wlanuserip") or "").strip():
        p["wlanuserip"] = local_ipv4() or ""
        log("门户没给 wlanuserip，改用本机出口 IP：%r" % p["wlanuserip"])

    for k, v in LOGIN_EXTRA.items():
        p.setdefault(k, v)
    p["userId"] = uid
    p["passwd"] = pwd
    p.setdefault("url", TRIGGER_URL)

    query = urlencode({k: p.get(k, "") for k in
                       ("wlanacip", "wlanacname", "wlanuserip", "mac", "vlan", "url")})
    post_url = "%s/webauth.do?%s" % (host, query)
    log("POST %s" % post_url)
    post_ok = False
    try:
        r = sess.post(post_url, data=p, timeout=15)
        post_ok = True
        log("POST 状态: %s  长度: %s" % (r.status_code, len(r.text)))
    except Exception as e:
        log("POST 异常: %s" % e)

    # 门户建立会话要几秒。原来只 sleep 2 秒就单次判定，太急：
    # 实测 12:50:07 POST 返回 200、12:50:09 就判「认证后仍不通」并报登录失败，
    # 而网络其实几秒后就通了（门户重定向里甚至已经带着 act=LOGINSUCC）。
    # 结果是「明明登上了却报失败」—— switch 场景下还会触发一次没必要的回滚。
    # 改成隔几秒多探几次再下结论：正常 1 次就过，真的失败才会走满全程。
    #
    # ⚠️ 但**探测表要按 POST 有没有真的发出去来分档**（2026-09-19 加）：
    #    这张表的用途是「等门户把会话建起来」，不是「等一个死掉的网络活过来」。
    #    POST 抛异常说明请求根本没落地（开机那一刻的典型情形），这时按 41 秒
    #    走满全程纯属白等 —— 实测那次 21:47 就是这么烧掉 15 + 41 秒的。
    #    缩短到两次探测（约 18 秒），把「没落地」和「落地了但响应丢了」都覆盖住，
    #    剩下的交给 do_auto 的外层重试。
    delays = (2, 3, 3, 4, 4) if post_ok else (3, 5)
    if not post_ok:
        log("POST 未落地，探测表缩短为 %s" % (delays,))
    for i, delay in enumerate(delays, 1):
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


# 门户下线响应里表示「已经不在线了」的措辞。
# 实测正文形如 `--> WIFI authentication 下线成功!`；重复下线时门户会回
# 「未在线 / 已经下线 / 不存在」这类说法。这些都必须算**成功**——用户的意图是
# 「让这个 IP 下线」，而它本来就不在线，意图已经达成。
# 只按字面匹配已知措辞，匹配不上就退回网络探测判定（见下面 logout_result_is_ok）。
LOGOUT_ALREADY_OFFLINE = ("未在线", "不在线", "已经下线", "已下线", "没有在线",
                          "不存在", "请勿重复", "重复下线", "未登录")


def logout_body_verdict(text):
    """从下线响应正文里读出门户自己的结论，返回 True/False/None。

        True  —— 正文明确说了「下线成功」或「本来就不在线」（两者都算达成意图）
        False —— 正文明确说了下线没成功
        None  —— 看不出结论（空正文、模板变了、拿到的不是预期页面）

    为什么要读正文：原来的实现只把正文**打日志**，然后**完全靠「3 秒后再探一次
    能不能上网」来下结论**。那个探测是间接证据 —— 门户会话还没断干净、或者
    探测目标本身抽风，都会让一次**已经成功**的下线被报成「退出失败」。
    而正文是服务端对这次请求的直接答复，是**更硬**的证据。

    保守起见：认不出来一律返回 None，绝不猜 —— 猜错会静默失败（把没下线
    说成已下线），比多报一次失败严重得多。
    """
    if not text:
        return None
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\s+", " ", t)
    # 先判「本来就不在线」：有些门户在重复下线时回的话里同时含「下线」和
    # 「未在线」，顺序反过来会误判成成功后又落到失败分支。
    if any(k in t for k in LOGOUT_ALREADY_OFFLINE):
        return True
    if "下线成功" in t or "注销成功" in t or "已断开" in t or "断开成功" in t:
        return True
    if "下线失败" in t or "注销失败" in t or "断开失败" in t:
        return False
    return None


def portal_logout():
    """复刻认证后页面「离线」按钮：POST /webdisconn.do?<urlParameter>，
    必须带 other1=disconn 和 distoken

    返回 True/False/None 三态（与 test_internet 同样的理由 —— 「确定没成功」
    和「看不出来」必须分开）：
        True  —— 已确认下线（门户正文说的，或明确已能判定不在线）
        False —— 确定仍然在线（还能上网）
        None  —— 判不出来（请求异常、正文无法解读、探测也不确定）

    调用方**只应在 False 时报「退出失败」**；None 要按「可能要重试」处理，
    别把一次网络抖动当成一次确定的失败。
    """
    cfg = load_config()
    host = cfg.get("portalHost") or DEFAULTS["portalHost"]
    token = get_distoken()
    if not token:
        log("未能取得 distoken，无法下线")
        # 拿不到 token 是**能力问题**不是结论：可能是取 token 的网络请求失败，
        # 也可能真的没在线。不在这里下判断，交给调用方按 None 重试。
        return None
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
    body = ""
    try:
        r = http_post(post_url, data=form, timeout=15, headers={"User-Agent": UA})
        body = r.text or ""
        txt = re.sub(r"(?s)<script.*?</script>", " ", body)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()[:120]
        log("  状态 %s  正文: %s" % (r.status_code, txt))
    except Exception as e:
        log("  异常: %s" % e)
    # 服务端对**这次请求**的直接答复，比 3 秒后的间接探测更硬。
    verdict = logout_body_verdict(body)
    if verdict is True:
        log("  门户正文确认已下线（含「本来就不在线」）")
        return True
    if verdict is False:
        log("  门户正文明确说下线失败")
        return False
    time.sleep(3)
    # 正文读不出结论时才靠网络探测兜底。三态：只有「明确还能上网」才算没下线；
    # 测不出来时返回 None（**不能**当成成功 —— 见 do_logout，那会让界面显示
    # 「已退出」而实际还在线）。（与原来的 `not test_internet()` 的差别就在这：
    # 原来单点失败会被当成下线成功。）
    net = test_internet(timeout=5)
    if net is False:
        log("已确认下线（正文无结论，网络探测显示已被门户拦截）")
        return True
    if net is True:
        log("  仍可联网，下线未生效")
        return False
    log("  下线结果无法判定（正文无结论，网络探测也不确定）")
    return None


# ==================== 业务动作 ====================
def _log_stage(tag, t0):
    """记一行「某阶段花了多久」。

    为什么要专门记（2026-09-19 加）：用户报「开机自启生效相当慢」，但原来的
    日志只有「开始检查」和「校园网就绪」两行，中间到底是在等网卡还是在烧
    超时**完全分不出来** —— 当时只能另写 auto_timing_probe.py 去反推。
    留一行耗时，下次一眼就能定位。
    """
    log("  [耗时] %s %.1f 秒" % (tag, time.time() - t0))


def do_auto(attempts=AUTO_ATTEMPTS):
    """开机自启 / 静默登录。返回退出码。

    ⚠️ 为什么要重试（2026-09-19 修的，就是「开机自启生效相当慢」的根因）：
        原来只试一次。而开机那一刻网络栈往往还没就绪（DNS 没通、门户不响应），
        实测一次失败要白烧 80 秒超时（联网检测 16 + GET 10 + POST 15 + 探测 41），
        然后**直接放弃**。用户看到的就是「等了半天没连上，而且它再也不会重来」。
        现在改成试 AUTO_ATTEMPTS 轮、轮间退避；网络一就绪，下一轮 3 秒就成。
        实测正常路径只要 3 秒（70 次历史运行里 wait_for_campus 耗时全是 0），
        所以重试的代价几乎全在「本来就会失败」的场景里 —— 正是该重试的场景。
    """
    cfg = load_config()
    uid = cfg.get("userId") or ""
    pwd = cfg.get("passwd") or ""
    log("=== 开始检查 (Action=auto, 账号=%s) ===" % uid)
    if not uid or not pwd:
        log("账号或密码为空，退出")
        write_result("auto", "ERR_CONFIG", False, "账号或密码为空")
        return 4

    t_all = time.time()
    last_code, last_msg = 4, "未检测到校园网，请确认已连接校园网 Wi-Fi"
    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1:
            backoff = AUTO_RETRY_BACKOFF * (attempt - 1)
            log("--- 第 %d/%d 轮重试（先等 %d 秒）---" % (attempt, attempts, backoff))
            time.sleep(backoff)
        else:
            log("--- 第 %d/%d 轮 ---" % (attempt, attempts))

        t0 = time.time()
        wait = AUTO_WAIT_FIRST if attempt == 1 else AUTO_WAIT_RETRY
        if not wait_for_campus(wait):
            log("%d 秒内未检测到校园网" % wait)
            _log_stage("等校园网", t0)
            last_code, last_msg = 4, "未检测到校园网，请确认已连接校园网 Wi-Fi"
            continue
        log("校园网就绪")
        _log_stage("等校园网", t0)

        t0 = time.time()
        net = test_internet(timeout=AUTO_NET_TIMEOUT)
        _log_stage("联网检测", t0)
        if net is True:
            log("网络已通，无需认证")
            _log_stage("总计", t_all)
            write_result("auto", "OK", True, "网络已连接")
            return 0
        log("联网检测：%s，开始登录" % ("未认证" if net is False else "无法判定"))

        t0 = time.time()
        ok = portal_login(uid, pwd)
        _log_stage("登录", t0)
        update_account_record(uid, pwd, ok)
        if ok:
            log("=== 认证成功（第 %d 轮）===" % attempt)
            _log_stage("总计", t_all)
            write_result("auto", "OK", True, "网络已连接")
            return 0
        log("=== 第 %d 轮认证后仍不通 ===" % attempt)

        # 密码**确定**是错的：再试多少轮结果都一样，别让用户在开机那几分钟干等。
        # （pw_ok 是 None 表示「判不出来」，那属于网络问题，要继续重试。）
        pw_ok = check_password(uid, pwd)
        if pw_ok is False:
            log("密码校验不通过，不再重试")
            _log_stage("总计", t_all)
            write_result("auto", "LOGIN_FAILED", False, "登录失败，请检查密码是否正确")
            return 2
        last_code, last_msg = 2, "登录失败，网络异常，请稍后重试"

    log("=== %d 轮都没成功 ===" % attempts)
    _log_stage("总计", t_all)
    write_result("auto", "NO_GATEWAY" if last_code == 4 else "LOGIN_FAILED",
                 False, last_msg)
    return last_code


# ==================== 守护（--guard）====================
GUARD_IDLE = "idle"          # 什么都不用做
GUARD_WAIT = "wait"          # 测不出来，再观察一次
GUARD_RELOGIN = "relogin"    # 确认掉线，重连


def guard_running():
    """已经有一个守护在跑吗。返回 (bool, 说明)。"""
    info = read_json(GUARD_FLAG)
    if not isinstance(info, dict):
        return False, ""
    try:
        started = float(info.get("started"))
    except (TypeError, ValueError):
        return False, "记录里没有启动时刻"
    age = time.time() - started
    if age < 0:
        return False, "记录时刻在未来（时钟被改过）"
    # ⚠️ **过期记录必须当作「没在跑」**：退出走 exit_now() → TerminateProcess，
    #    finally 不会执行，guard.json 经常是残留的。只看「文件在不在」的话，
    #    残留一份就再也不会启动守护 —— 而且是**静默**的，界面上完全看不出来。
    if age > GUARD_SECONDS + 300:
        return False, "记录已过期（%.0f 秒前）" % age
    pid = info.get("pid")
    if not pid:
        return False, "记录里没有 pid"
    try:
        import psutil
        alive = psutil.pid_exists(int(pid))
    except Exception:
        # 判不了就保守当作在跑：宁可少起一个守护，也不要起两个 ——
        # 两个守护会同时重连，反而更容易被 BRAS 踢。
        return True, "无法确认进程 %s 是否还在（保守当作在跑）" % pid
    return (True, "PID %s" % pid) if alive else (False, "进程 %s 已退出" % pid)


def guard_decide(net, on_campus, unknown_streak):
    """守护根据一次检测结果决定做什么。**纯函数、不发任何请求**，所以能直接测。

    net / on_campus 都是三态（True / False / None），含义同 test_internet()
    和 on_campus_network()。unknown_streak 是「连续测不出是否联网」的次数。
    """
    if on_campus is not True:
        # 不在校园网（或判不出来）：什么都不做。
        # 在家里、用手机热点时它本来就不该去登录 —— 登了也没用，只会把日志刷满。
        return GUARD_IDLE
    if net is True:
        return GUARD_IDLE
    if net is False:
        # 在校园网、但流量被门户挡着 —— 这就是「被踢下线」的样子。重连。
        return GUARD_RELOGIN
    # net is None：测不出来。可能只是一次抖动，**连够 GUARD_CONFIRM 次才动手**，
    # 免得被一次瞬时失败牵着走（每 30 秒重连一次，本身就可能被 BRAS 盯上）。
    return GUARD_WAIT if unknown_streak < GUARD_CONFIRM else GUARD_RELOGIN


def run_guard(seconds=GUARD_SECONDS, poll=GUARD_POLL):
    """开机后的一小段守护：掉线了自己重连，到点就退出。返回退出码。

    为什么需要它（2026-09-19 用户报的第三个问题）：
        `--auto` 登录成功后**进程就退了**。之后不管会话是被 BRAS 对账踢掉的、
        还是碰上学校那边的空闲超时，都**没有任何东西再去重连** —— 用户看到的
        就是「连上几分钟后莫名其妙掉线」，而且不会自己连回来。

    为什么只工作 30 分钟（用户明确要求）：
        开机 + 上课这一段是最容易掉线的时段，守住它收益最大；再往后就不值得
        留一个常驻进程了。到点自动退出，不留后台进程。

    ⚠️ 退出走 exit_now() → TerminateProcess，**finally / atexit 都不会执行**，
       所以 guard.json 必然残留 —— 这是设计上接受的，靠 guard_running() 的
       「过期」判定自愈。别改成「文件存在即视为在跑」。

    ⚠️ 已确认**不会挡住自我更新**（不用再查一遍）：更新走的是
       updater.apply_update()，它用 os.rename 把运行中的 exe 改名成 .old
       —— Windows 把运行中的映像映射成 FILE_SHARE_DELETE，所以**有几个进程
       正跑着它都不影响改名**（updtest_running_exe.py 实测过）。
       守护会继续从改名后的 .old 跑完剩余时间，然后正常退出。
       （0.3.3 起那个 .old 落在**数据目录**而不是 exe 同目录，所以桌面上不会
         出现它；跨卷时才退回 exe 同目录 —— 见 updater.work_dir_for。）
    """
    running, why = guard_running()
    if running:
        log("=== 已有守护在跑（%s），本进程直接退出 ===" % why)
        return 0
    cfg = load_config()
    uid = cfg.get("userId") or ""
    pwd = cfg.get("passwd") or ""
    if not uid or not pwd:
        log("=== 守护：账号或密码为空，退出 ===")
        return 0
    write_json(GUARD_FLAG, {"pid": os.getpid(), "started": time.time(),
                            "version": VERSION})
    log("=== 守护开始：共 %d 秒，每 %d 秒查一次（pid=%s）==="
        % (seconds, poll, os.getpid()))
    deadline = time.time() + seconds
    streak = 0        # 连续「测不出是否联网」的次数
    fails = 0         # 连续重连失败次数
    next_beat = time.time() + 300
    while True:
        left = deadline - time.time()
        if left <= 0:
            break
        time.sleep(min(poll, left))
        if time.time() >= deadline:
            break
        try:
            if time.time() >= next_beat:
                next_beat = time.time() + 300
                log("守护运行中（还剩 %d 秒）" % int(deadline - time.time()))
            on = on_campus_network(2.0)
            # 不在校园网就不必花时间做联网检测 —— 那一项才是贵的（最多 10 秒）。
            net = test_internet(timeout=AUTO_NET_TIMEOUT) if on is True else None
            act = guard_decide(net, on, streak)
            if act == GUARD_WAIT:
                streak += 1
                log("守护：在校园网但测不出是否联网（连续第 %d 次）" % streak)
                continue
            streak = 0
            if act != GUARD_RELOGIN:
                continue
            # 用户主动退出过就别跟他对着干。**每一拍都要重新看** ——
            # 用户完全可能在守护运行期间才点的「退出当前账号」。
            if logged_out_recently():
                log("守护：用户刚主动退出过，这一拍不重连")
                continue
            log("=== 守护：检测到掉线，重新登录 ===")
            ok = portal_login(uid, pwd)
            update_account_record(uid, pwd, ok)
            if ok:
                fails = 0
                log("=== 守护：重连成功 ===")
                write_result("guard", "OK", True, "掉线后已自动重连")
            else:
                # 重连失败要退避：网络真断了的时候，每 30 秒重试一次会在
                # 半小时里攒出几十次无效登录，把日志刷满、还可能被 BRAS 盯上。
                fails += 1
                nap = min(GUARD_FAIL_BACKOFF_MAX, poll * (2 ** fails))
                nap = min(nap, max(0.0, deadline - time.time()))
                log("=== 守护：重连失败（连续第 %d 次），下次多等 %d 秒 ==="
                    % (fails, int(nap)))
                write_result("guard", "LOGIN_FAILED", False, "掉线后自动重连失败")
                if nap > 0:
                    time.sleep(nap)
        except Exception:
            # 守护里任何异常都不能让循环挂掉 —— 挂了就再也没人重连了，
            # 而且用户只会看到「又掉线了」，日志里什么线索都没有。
            log("守护循环异常（已忽略，继续）:\n%s" % traceback.format_exc())
    log("=== 守护结束（已跑满 %d 秒）===" % seconds)
    try:
        os.remove(GUARD_FLAG)
    except OSError:
        pass
    return 0


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
        note_logout()
        write_result("logout", "OK", True, "当前未登录校园网")
        return 0

    # 拿「共享 IP 的写权限」。另一个窗口/线程正在下线时它是拿不到的。
    # 没有它的话，两个窗口各发一次下线请求，谁先谁成功，后到那个面对的是
    # 一个已经不在线的 IP —— 界面就弹「退出失败，请稍后重试」，
    # 而用户什么都没做错（这就是这次修的 bug 的主因之一）。
    if not _LOGOUT_LOCK.acquire(timeout=_LOGOUT_LOCK_WAIT):
        log("=== 另一个操作正在执行（%.0f 秒内没拿到锁），中止 ===" % _LOGOUT_LOCK_WAIT)
        write_result("logout", "BUSY", False,
                     "另一个操作正在执行，请等它结束后再试")
        return 3
    try:
        # 重试：portal_logout() 返回 None 表示**判不出来**（拿不到 distoken、
        # 请求异常、正文读不出结论、网络探测也不确定）——那多半是一次网络抖动，
        # 再试一次往往就成。返回 False 才是「确定还在线」，重试没有意义。
        #
        # 为什么必须重试：原来只试一次，任何一次抖动都直接弹「退出失败，请稍后重试」。
        # 而下线请求本身是幂等的（门户对已下线的 IP 回「未在线」这类话，已按成功处理），
        # 多试几次不会造成额外影响，却能把大部分瞬时失败消化掉。
        last = None
        for attempt in (1, 2, 3):
            v = portal_logout()
            last = v
            if v is True:
                log("=== 已下线（第 %d 次尝试）===" % attempt)
                note_logout()
                write_result("logout", "OK", True, "已退出当前账号")
                return 0
            if v is False:
                log("=== 下线未生效：门户仍认定为在线（第 %d 次尝试）===" % attempt)
                break
            log("=== 下线结果不确定，稍后重试（第 %d 次尝试）===" % attempt)
            if attempt < 3:
                time.sleep(2)
    finally:
        _LOGOUT_LOCK.release()

    if last is False:
        write_result("logout", "LOGOUT_FAILED", False, "退出失败，请稍后重试")
        return 3
    # 三次都判不出来：**不能说「已退出」**（那会让用户在还联网的情况下以为
    # 自己退了），但也要把「可能已经退了、只是确认不了」说清楚，
    # 否则用户会以为程序坏了。
    log("=== 下线结果无法确认（已重试 3 次）===")
    write_result("logout", "LOGOUT_UNKNOWN", False,
                 "无法确认是否已退出，请检查网络状态后重试")
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
        # portal_logout() 现在返回三态，`not portal_logout()` 会把 None 也当失败 ——
        # 那正是「判不出来」的合法情形（网络抖动），直接中止切换太武断。
        # 只有 False（**确定**还在线）才中止：那时再登目标账号必然被服务端拒绝，
        # 报「此IP已在线」，切换其实没发生。
        # 与 do_logout 共用同一把锁：切换的第一步就是下线，和「退出当前账号」
        # 争的是同一个门户会话。两个同时跑会互相把对方的 IP 弄下线。
        if not _LOGOUT_LOCK.acquire(timeout=_LOGOUT_LOCK_WAIT):
            log("=== 另一个操作正在执行（%.0f 秒内没拿到锁），中止切换 ===" % _LOGOUT_LOCK_WAIT)
            write_result("switch", "BUSY", False,
                         "另一个操作正在执行，请等它结束后再试")
            return 3
        try:
            out = portal_logout()
        finally:
            _LOGOUT_LOCK.release()
        if out is False:
            log("=== 下线失败：当前账号仍在线，已中止切换 ===")
            write_result("switch", "LOGOUT_FAILED", False, "切换失败：当前账号仍在线，请稍后重试")
            return 3
        if out is None:
            # 判不出来：继续走登录。最坏情况是当前账号还在线、登录被拒，
            # 那由下面 portal_login 的探测暴露出来并触发回滚 ——
            # 比在这里无谓地中止一次切换要好。
            log("下线结果不确定，仍继续登录目标账号（失败会回滚）")
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
# 自启快捷方式带的命令行参数。
#   --auto  : 静默登录（开机那一刻跑）
#   --guard : 登录完再守 30 分钟，掉线了自动重连（见 run_guard）
# ⚠️ 这里是**唯一来源**：set_autostart() 用它写 .lnk，
#    autostart_outdated() 用它判断老链接要不要更新。两处都别再写字面量。
AUTOSTART_ARGS = "--auto --guard"
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
    """开机自启开着没有。**计划任务和启动文件夹任一条在，就算开着。**

    两条路都要认：老用户升级过来时自启还在启动文件夹里（见 autostart_upgrade），
    只认计划任务的话界面上那个勾会突然变空 —— 用户会以为自启被关了。
    """
    return task_exists() or len(autostart_links()) > 0


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
    项，而且 `set_autostart()` 也会拒绝。
    """
    if not is_frozen():
        return False
    if task_stale():
        return True
    links = autostart_links()
    if not links:
        return False
    return all(_lnk_points_to_me(p) is False for p in links)


# 认得出的命令行开关（白名单）。判断 .lnk 里的参数时**只能按这个表认**。
# 为什么必须白名单（2026-09-19 实测）：参数串在 .lnk 里跟后面的字符串之间
# **没有可靠的分隔符** —— 紧跟其后的那一个字符是二进制残留，实测见过 `?`
# （0x3F）也见过 `A`（0x41）。撞上字母时，`--guard` 会被粘成 `--guardA`。
# 所以「凡是 --xxx 都算」不行，按字符类截断也不行，只能认自己写过的开关。
KNOWN_SWITCHES = ("--auto", "--guard", "--switch", "--selftest", "--guitest",
                  "--purge", "--version", "--checkupdate", "--logout")


def _match_switch(tok):
    """把一个 token 认成已知开关。认不出返回 ""。

    ⚠️ 用 startswith 而不是相等：token 末尾可能粘着残留字符（见 KNOWN_SWITCHES）。
    """
    low = (tok or "").lower()
    for s in sorted(KNOWN_SWITCHES, key=len, reverse=True):
        if low.startswith(s):
            return s
    return ""


def _lnk_arguments(path):
    """读 .lnk 里的命令行参数（小写，形如 `--auto --guard`）。读不出来返回 ""。

    跟 `_lnk_points_to_me` 一样**不用 pylnk3** —— 它解析中文路径会丢段。
    参数是纯 ASCII，快捷方式里按 UTF-16LE 明文存着，扫一遍就够。
    奇偶两种对齐都试：字符串数据的起始偏移不保证是偶数。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return ""
    for off in (0, 1):
        text = raw[off:].decode("utf-16-le", "ignore")
        i = text.find("--auto")
        if i < 0:
            continue
        out = []
        # 只看开头这一小段，免得把后面别的字段里恰好出现的开关也吸进来。
        for tok in text[i:i + 120].split()[:6]:
            s = _match_switch(tok)
            if not s:
                break            # 遇到认不出的就当参数到头了
            out.append(s)
        if out:
            return " ".join(out)
    return ""


def autostart_args():
    """当前自启项实际带的参数（给 --selftest 展示用）。没有就返回 ""。

    计划任务优先：升级刚做完时两条路可能短暂并存（任务建好了、.lnk 还没删掉），
    这时该报出来的是真正会生效的那条。
    """
    st = task_state()
    if st["exists"]:
        return st["arguments"]
    for p in autostart_links():
        a = _lnk_arguments(p)
        if a:
            return a
    return ""


def autostart_outdated():
    """自启项指向的 exe 是对的，但**参数是旧版本的**（少了 --guard）。

    为什么必须单独判（2026-09-19 加）：
        `autostart_stale()` 只看**目标路径**。0.3.0 把参数从 `--auto` 改成
        `--auto --guard` —— 老用户的自启项指向的 exe 完全正确，所以
        `autostart_stale()` 返回 False、勾照样打着、开机也照样登录，
        只是**没有守护**：掉线后不会自动重连。
        不提示的话，用户升级到 0.3.0 却发现「掉线还是不重连」，
        会以为是没修好 —— 而真正的原因是那份开机自启还停在旧参数上。
    """
    if not is_frozen():
        return False
    if task_outdated():
        return True
    links = autostart_links()
    if not links:
        return False
    # 参数该带哪些，唯一来源是 AUTOSTART_ARGS —— 别再写 "--guard" 这种字面量，
    # 否则以后给 AUTOSTART_ARGS 加开关时这里会漏判。
    want = AUTOSTART_ARGS.split()
    return all(_lnk_points_to_me(p) is True
               and not all(a in _lnk_arguments(p).split() for a in want)
               for p in links)


# ==================== 开机自启 · 计划任务（首选机制）====================
# 为什么不再用启动文件夹（2026-09-20 实测，证据在本机事件日志里）：
#   同一次开机，OS 启动 = 09:32:33，用户登录 = 09:32:52：
#     09:32:57.322  GHelper.exe    <- 计划任务 \GHelper（登录时触发）
#     09:32:59.274  PowerToys.exe  <- 计划任务 \PowerToys\Autorun for Hobson
#     09:33:13.294  Shell 发出 DesktopStartupApps 信号（桌面就绪）
#     09:33:38.021  HKCU\...\Run 的第一项才开始执行 —— 中间白等了 24.7 秒
#     09:33:58.2    我们（启动文件夹里的 .lnk）—— 排在 Run 键 6 项之后
#   ⇒ 登录触发的计划任务在**开机后 25 秒**就跑起来了，启动文件夹要等到 **85 秒**。
#     而程序自己被拉起之后只花 0.3 秒（认证逻辑）+ 约 2 秒（onefile 解压）。
#     慢的从来不是认证，是「什么时候轮到我们」。换成计划任务能提前约 60 秒。
#
# 任务名必须**纯 ASCII**：它要进 PowerShell 命令行，也是任务定义的文件名。
AUTOSTART_TASK_NAME = "CampusLogin"

# 任务定义 XML。几条**不能删**的设置：
#   MultipleInstancesPolicy=IgnoreNew  同会话重复触发时不要起第二份（两份守护互相不知道对方）
#   DisallowStartIfOnBatteries=false   笔记本用电池时也要跑（默认是「用电池就不启动」）
#   ExecutionTimeLimit=PT0S            不限时长。默认 72 小时；守护只跑 30 分钟，
#                                      但写死「不限」，免得以后改守护时长被它砍掉
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>%s</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%s</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>%s</Arguments>
      <WorkingDirectory>%s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _tasks_dir():
    """计划任务的定义目录。

    ⚠️ 这个目录**不能列**（普通用户 listdir 会被拒绝访问），但按名字打开文件
    没问题 —— 所以下面只做 isfile / open，别写 os.listdir。
    """
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                        "System32", "Tasks")


def task_def_path():
    return os.path.join(_tasks_dir(), AUTOSTART_TASK_NAME)


def _task_text():
    """读计划任务的定义文件（UTF-16LE + BOM）。读不到返回 ""。

    为什么读文件、不问 PowerShell：勾选状态和失效提示每次开界面都要看，
    拉一次 PowerShell 要 ~1 秒，白等。任务定义就是个 UTF-16 的 XML 文件，
    实测普通用户直接读得到（见 _tasks_dir 的说明）。
    """
    try:
        with open(task_def_path(), "rb") as f:
            return f.read().decode("utf-16-le", "ignore")
    except Exception:
        return ""


def _tag(text, name):
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), text, re.S)
    return m.group(1).strip() if m else ""


def _norm_path(p):
    return os.path.normcase((p or "").strip().replace("/", "\\"))


def task_state():
    """计划任务当前的形状：存在 / 跑什么 / 带什么参数 / 是不是登录触发。只读文件。"""
    text = _task_text()
    if not text:
        return {"exists": False, "command": "", "arguments": "", "logon": False}
    return {"exists": True,
            "command": _tag(text, "Command"),
            "arguments": _tag(text, "Arguments"),
            "logon": "<LogonTrigger>" in text}


def task_exists():
    return task_state()["exists"]


def task_installed():
    """任务在、登录触发、而且跑的就是**当前这个 exe**。"""
    st = task_state()
    return bool(st["exists"] and st["logon"]
                and _norm_path(st["command"]) == _norm_path(app_path()))


def task_stale():
    """任务在，但指向的是别的 exe（用户把 exe 挪了位置）。"""
    st = task_state()
    return bool(st["exists"] and st["command"]
                and _norm_path(st["command"]) != _norm_path(app_path()))


def task_outdated():
    """任务在、目标也对，但参数是旧版本的（少了 --guard）。

    和 autostart_outdated() 是同一件事的两条路：以前用 .lnk 时老参数少了 --guard，
    现在换成任务 —— 将来要是再改 AUTOSTART_ARGS，老任务也得能被认出来。
    """
    st = task_state()
    if not (st["exists"] and st["logon"]):
        return False
    if _norm_path(st["command"]) != _norm_path(app_path()):
        return False
    have = set(st["arguments"].split())
    return not all(a in have for a in AUTOSTART_ARGS.split())


def _powershell():
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                        "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def _ps(script, timeout=60):
    """跑一段 PowerShell，返回 (returncode, 输出文本)。

    ⚠️ 输出只用来记日志 / 排障，**不要拿它做判断**：中文 Windows 上管道 stdout
    是 cp936，我们的路径里有中文，读回来必然乱码。要回读结果就落盘再读
    （项目里 --selftest 已经吃过一次这个亏，见那边的注释）。
    """
    exe = _powershell()
    if not os.path.isfile(exe):
        return -1, "找不到 powershell.exe"
    try:
        p = subprocess.run([exe, "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-Command", script],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return -1, traceback.format_exc()


def current_sid():
    """当前用户的 SID 字符串（S-1-5-...）。拿不到返回 ""。

    为什么要自己算：建任务的 XML 里要写它，而用 whoami / PowerShell 去问
    要额外拉一个子进程（~1 秒），而且中文输出还得再解一次码。这里直接问内核。

    ⚠️ 下面几个 API **必须显式声明 argtypes / restype**（2026-09-20 踩过）：
       ctypes 对没声明参数类型的函数，会把 Python 整数按 **32 位** 传。
       SID 指针是 64 位的，被截掉高 32 位之后 ConvertSidToStringSidW 必然失败，
       函数就静默返回 ""（现象是 task_create 只会说「取不到当前用户 SID」）。
       实测：声明前返回空，声明后立刻拿到 S-1-5-21-...。
    """
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        TOKEN_QUERY, TokenUser = 0x0008, 1

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

        h = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(h)):
            return ""
        try:
            need = wintypes.DWORD(0)
            # 第一次调用只问长度，必然返回 0（ERROR_INSUFFICIENT_BUFFER = 122），
            # 这不是错误 —— 只要 need 被填上了就继续。
            advapi32.GetTokenInformation(h, TokenUser, None, 0, ctypes.byref(need))
            if not need.value:
                return ""
            buf = ctypes.create_string_buffer(need.value)
            if not advapi32.GetTokenInformation(h, TokenUser, buf, need.value,
                                                ctypes.byref(need)):
                return ""
            # TOKEN_USER 的第一个成员是 SID_AND_ATTRIBUTES{ PSID Sid; DWORD Attributes }
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_ptr:
                return ""
            out = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr),
                                                   ctypes.byref(out)):
                return ""
            try:
                return out.value or ""
            finally:
                kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                kernel32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def _xml_esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def task_create():
    """建/更新「登录时触发」的计划任务。返回 (ok, 说明)。

    为什么走 PowerShell + 完整任务 XML，而不是 schtasks.exe 的命令行：
      · schtasks 的 `/sc onlogon` 在**非提权**下到底能不能用，本机没法验证
        （schtasks.exe 在沙箱里被黑名单拦着），不敢当主路径；
        PowerShell 的 Register-ScheduledTask 已实测非提权可用。
      · 任务里要嵌中文 exe 路径。塞命令行要过好几层引号 + 代码页；
        走 XML 文件（UTF-16 落盘、Get-Content -Encoding Unicode 读回）
        实测回读一字不差。
    """
    sid = current_sid()
    if not sid:
        return False, "取不到当前用户 SID"
    exe = app_path()
    xml = TASK_XML % (_xml_esc("%s：开机后自动登录校园网，并守护 30 分钟" % APP_TITLE),
                      _xml_esc(sid), _xml_esc(sid), _xml_esc(exe),
                      _xml_esc(AUTOSTART_ARGS), _xml_esc(os.path.dirname(exe)))
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".xml", prefix="campuslogin-task-")
        os.close(fd)
        # UTF-16LE + BOM，跟 XML 声明里的 encoding="UTF-16" 对上
        with open(tmp, "wb") as f:
            f.write(b"\xff\xfe")
            f.write(xml.encode("utf-16-le"))
        rc, out = _ps("Register-ScheduledTask -TaskName '%s' -Xml "
                      "(Get-Content -LiteralPath '%s' -Raw -Encoding Unicode) -Force "
                      "| Out-Null; 'OK'" % (AUTOSTART_TASK_NAME, tmp))
        if rc != 0:
            return False, "注册失败：%s" % out.strip()[-200:]
    except Exception as e:
        return False, "建任务时出错：%s" % e
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                # 删不掉就留在 %TEMP% 里。只是一份任务定义 XML，不含账号密码，
                # 但留个痕 —— 免得以后有人在 %TEMP% 里看到它一头雾水。
                log("临时的任务定义 XML 没删掉: %s" % tmp)
    # ⚠️ 必须回读。Register-ScheduledTask 返回 0 只说明「这次调用没报错」，
    #    不代表任务真的长成了我们要的样子（指向谁 / 参数 / 触发类型都要核）。
    st = task_state()
    if not task_installed():
        return False, "任务建好了但回读不对（目标=%r 参数=%r 登录触发=%s）" % (
            st["command"], st["arguments"], st["logon"])
    return True, ""


def task_delete():
    """删掉计划任务。返回 (ok, 说明)。本来就没有也算成功。"""
    if not task_exists():
        return True, ""
    rc, out = _ps("Unregister-ScheduledTask -TaskName '%s' -Confirm:$false; 'OK'"
                  % AUTOSTART_TASK_NAME)
    if rc != 0:
        return False, "删除失败：%s" % out.strip()[-200:]
    return (True, "") if not task_exists() else (False, "删了但任务文件还在")


def autostart_mode():
    """当前自启走的是哪条路：`task`（计划任务）/ `lnk`（启动文件夹）/ `""`（没开）。"""
    if task_exists():
        return "task"
    return "lnk" if autostart_links() else ""


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


def _remove_links(links):
    """删掉启动文件夹里的自启快捷方式。返回删不掉的列表。

    删不掉也必须留痕：残留的旧链接（比如指向 .bat 的）和新建的自启项会
    **同时触发**，等于开机登录两遍。
    """
    left = []
    for p in links:
        try:
            os.remove(p)
        except Exception:
            left.append(p)
            log("自启快捷方式删不掉，可能残留:\n%s\n%s" % (p, traceback.format_exc()))
    return left


def set_autostart_lnk(on):
    """老机制：在启动文件夹里放 / 删 .lnk。返回 (ok, 说明)。"""
    d = startup_dir()
    if not os.path.isdir(d):
        return False, "找不到启动文件夹"
    try:
        if not on:
            _remove_links(autostart_links())
            return (True, "") if not autostart_links() else (False, "快捷方式未能删除")
        # 清掉旧的（可能指向 .bat），统一换成指向 exe 的
        _remove_links(autostart_links())
        exe = app_path()
        lnk_path = os.path.join(d, AUTOSTART_LNK_NAME)
        # ⚠️ 参数里必须带 --guard。`--auto` 登录成功就退出了，
        #    掉线之后没人重连（见 run_guard 的说明）。
        #    改这里要同步改 autostart_outdated() 里认的参数，
        #    否则老用户的 .lnk 不会被判成「要更新」。
        make_lnk(lnk_path, exe, arguments=AUTOSTART_ARGS, icon=exe,
                 work_dir=os.path.dirname(exe))
        return (True, "") if os.path.isfile(lnk_path) else (False, "快捷方式未生成")
    except Exception as e:
        return False, str(e)


def set_autostart(on):
    """开 / 关开机自启。返回 (ok, 说明)。

    首选**计划任务**（登录时触发），建不了才退回启动文件夹的 .lnk ——
    两条路差约 60 秒，理由见 AUTOSTART_TASK_NAME 那段。
    ⚠️ 两条路**不能同时留着**：任务和 .lnk 都会在开机时触发，等于同时起两份
       `--auto --guard`，两个守护互相不知道对方（一个以为掉线了去重连，
       另一个也在动），白多一次认证、甚至互踢。所以开的时候建了任务就把 .lnk 清掉。
    """
    if not is_frozen():
        return False, "源码运行时无法设置自启"
    if on:
        ok, why = task_create()
        if not ok:
            # 建不了任务（组策略禁用、任务计划服务停了、PowerShell 被拦……）
            # 也必须能用，只是慢一些。界面上照样显示「已开启」，
            # 退回这件事留在日志里，--selftest 也会报。
            log("建计划任务失败，退回启动文件夹自启：%s" % why)
            return set_autostart_lnk(True)
        _remove_links(autostart_links())
        return True, ""
    # 关：两条路都要清，留哪条都会让「已关闭」变成假话
    _ok_task, why_task = task_delete()
    _remove_links(autostart_links())
    if not is_autostart_on():
        return True, ""
    return False, why_task or "自启项未能删除"


# 升级标记：老版本的自启全在启动文件夹里，升级后要自动换成计划任务。
# 记一次就够 —— 建任务要拉一次 PowerShell（~1 秒），每次启动都试会拖慢开界面。
AUTOSTART_UPGRADE_MARK = os.path.join(data_dir(), "autostart-upgrade.json")


def autostart_upgrade():
    """把老版本留在启动文件夹里的自启，升级成登录计划任务。返回 (换了没, 说明)。

    为什么要**自动**做，而不是让用户「取消再勾一次」（2026-09-20 加）：
        这次修复的全部价值就是「开机后早约 60 秒」。用户升级之后如果自启还挂在
        启动文件夹上，他明天开机还是等 85 秒 —— 等于没修。
        升级后第一次运行顺手换掉，才算真的修好。

    源码运行不做：那时 `app_path()` 是脚本路径，建出来的任务会指向 python.exe。
    """
    if not is_frozen():
        return False, ""
    try:
        mark = read_json(AUTOSTART_UPGRADE_MARK) or {}
    except Exception:
        mark = {}
    if mark.get("done"):
        return False, ""
    links = autostart_links()
    if not links and not task_exists():
        # 用户本来就没开自启 —— 别自作主张给他建任务
        write_json(AUTOSTART_UPGRADE_MARK, {"done": True, "ok": True, "why": "没开自启"})
        return False, ""
    # ⚠️ 只有「当前跑的这份 exe 就是用户装的那份」才允许动自启（2026-09-20 踩到，
    #    真事）：构建脚本的冒烟测试会拿 dist21 里的新 exe 跑一次 --selftest，
    #    那次运行如果也去迁移，就会把自启指到**构建产物**上、还顺手删掉用户的
    #    .lnk —— 一次构建就把用户的机器搞坏。判据用 .lnk 自己：它记着用户装在哪。
    #    ⚠️ 跳过时**绝不能写升级标记** —— 标记在 ProgramData 里是全机共享的，
    #       写了的话真正装的那份下次启动就被跳过了，修复对用户等于没发生。
    if links and not all(_lnk_points_to_me(p) is True for p in links):
        log("自启升级跳过：当前 exe（%s）不是启动文件夹里自启项指向的那份" % app_path())
        return False, ""
    st = task_state()
    if st["exists"] and not task_installed() and not links:
        # 任务在、但指向别处，又没有 .lnk 可以当参照 —— 不知道用户装的到底是哪份，
        # 那就不猜。界面会把这种「已失效」显式提示出来，用户取消再勾一次即可。
        log("自启升级跳过：已有任务指向 %s，当前 exe 是 %s，不确定该用哪个"
            % (st["command"], app_path()))
        return False, ""
    if task_installed():
        if links:
            # 任务已经在跑了，.lnk 是残留 —— 留着会和任务同时触发
            _remove_links(links)
            log("自启已经是计划任务，清掉了启动文件夹里的残留")
        write_json(AUTOSTART_UPGRADE_MARK, {"done": True, "ok": True})
        return False, ""
    ok, why = task_create()
    if ok:
        _remove_links(links)
        write_json(AUTOSTART_UPGRADE_MARK, {"done": True, "ok": True})
        log("开机自启已从「启动文件夹」升级为「登录计划任务」（开机后能提前约 60 秒）")
        return True, ""
    write_json(AUTOSTART_UPGRADE_MARK, {"done": True, "ok": False, "why": why})
    log("自启升级成计划任务失败，继续用启动文件夹（会慢约 60 秒）：%s" % why)
    return False, why


# ==================== 使用说明 ====================
# 说明文字直接写进程序，不再随 exe 附带 .txt —— 拷一个 exe 过去就能看到。
# 面向使用者：不写实现细节，短句、少术语，第一次用的人照着做就行。
# （排障用的 --selftest 之类**不写进这里** —— 它们由 --help 单独列出，见下面的
#   SWITCH_HELP。这一段是给普通用户看的，出现 --xxx 只会让人困惑。）
# 标题行用拼接的方式带上版本号，**不把它写死在下面的长文本里** ——
# 否则以后改版本号时很容易漏掉这一处，用户看到的说明就成了旧版本。
#
# ⚠️ 守护分钟数这里写**字面量 30**，不用 %-格式化、也不做运行时替换。
#    原因：HELP_TEXT 是要被 help_check.py 在**打包后的 exe 里逐字节核对**的，
#    而运行时拼出来的字符串在归档里只以「模板 + 参数」的形式存在
#    （实测：写 `守 __GUARD__ 分钟` 再 replace，exe 里搜得到的是 `__GUARD__`，
#    搜不到渲染后的句子 —— 报了一条假失败）。
#    漂移风险改由 help_check.py 拿 GUARD_SECONDS 反算比对来兜：
#    改常量忘了改文案，那边会立刻报 BAD。
HELP_TEXT = (("校园网自动登录 —— 使用说明（v%s）\n" % VERSION) + r"""
【第一次使用】

  1. 填上你的校园网账号和密码
  2. 点「仅保存」
  3. 勾选最下面的「开机时自动登录校园网」

  以后开机就会自动联网，不用再管。

  联网之后它还会继续守 30 分钟：这段时间里要是掉线了，会自动重连。
  30 分钟一到就自己退出，不会在后台常驻。


【三个按钮】

  切换到此账号
      换账号时用。现在登的是别人的号，点它换成你的。
      没切成功会自动退回原来的账号，不会让你卡在断网状态。
      本来就在用这个账号的话，会直接告诉你，不会白断一次网。

  仅保存
      只记住账号密码，不马上切换。

  退出当前账号
      主动下线。下线后本机暂时上不了网。
      下线要连校园网的门户，偶尔会碰上网络抖动，程序会自动再试两次，
      不会一次不成就报「退出失败」。
      如果同时开了两个设置窗口，一个在操作时另一个会提示
      「另一个操作正在执行」，等前一个结束再点就行。
      没测准是否真退成功时，会明确说「无法确认是否已退出」——
      宁可让你自己再看一眼，也不会谎报「已退出」。


【密码框怎么用】

  密码默认是看不到的，显示成一串小星号，旁边的人瞄一眼也看不出内容。

  正在输入时，刚敲进去的那一个字符会露出来一下，方便你确认有没有打错，
  前面的字符仍然是星号。

  想看完整密码，点密码框右边的「显示」，再点一下「隐藏」就盖回去。
  鼠标点到别处、或者关掉窗口，密码会重新变回星号。

  从别处复制密码粘进来，用 Ctrl+V。想改中间的字，先退格删掉再重打
  （输入位置固定在末尾）。


【顶部的网络状态】

  绿点  已连接校园网       在校园网里，并且已经认证，可以正常上网。
  红点  已连校园网，未认证  在校园网里，但被登录页挡住了，需要登录。
  灰点  未连接校园网       现在连的不是校园网（比如家里或手机的 Wi-Fi）。
                          这种状态下点登录是没用的。
  灰点  网络状态未知       这次没检测出来，不代表没网，
                        点「切换到此账号」试一次。


【账号密码存在哪】

  存在这台电脑的  C:\ProgramData\CampusLogin  里，不在这个程序里。

  所以把「校园网自动登录.exe」这一个文件拷给别人，
  不会带出你的账号、密码和登录记录。

  想在本机彻底清掉账号密码，在命令行执行：
      校园网自动登录.exe --purge


【检查更新】

  标题右边那行小字（就是写着「v版本号　检查更新」的那块）就是入口，点它一下。
  （版本号写的是当前版本，不用记具体数字。）

  如果有新版本，会问你「是否现在下载并升级」。
  点「是」之后：下载 → 校验 → 替换 → 自动重新打开，整个过程不用你动手。
  升级只换程序本身，保存在 C:\ProgramData\CampusLogin 里的账号不动。

  升级过程中产生的临时文件（下载中的安装包、旧版本备份）也放在那个目录，
  不会写到你放 exe 的地方 —— 升级完 exe 所在文件夹里还是只有 exe 一个文件。
  （0.3.2 及之前不是这样：那会儿会往 exe 同目录丢一个 .old 文件。）

  为什么升级前要「校验」：下载下来的文件会跟发布方公布的哈希值对一遍，
  对不上就直接丢掉，不会装一个来路不明的文件。

  如果显示「检查更新失败」，多半是当时网络不通，稍后再点一次就行。
  注意：**失败时会明确说「失败」，不会含糊地显示「已是最新版本」。**
  这两件事必须分清楚 —— 没查成功不等于没有新版本。


【注意】

  · 只适用于本校校园网。
  · 部分杀毒软件可能误报，加白名单即可。
  · 开机联网后程序还会守 30 分钟，掉线了自动重连；到点自己退出。
    这段时间里你要是主动点了「退出当前账号」，它不会把你登回去。
  · 主动退出账号后，校园网通常几十秒内会自动重新连上，
    这是学校那边的设置，不是故障。
  · 这个 exe 放在哪个文件夹都能正常用，文件夹名带中文、带空格也没关系；
    账号密码存在系统目录里，不跟着 exe 走。
  · 但如果把 exe 挪到别的位置，「开机自启」会失效 —— 开机自启项记的是原来的
    位置。这时界面会在「开机自启」下面提示你，把那个勾取消、再重新勾一次就好。
    升级到新版本后如果提示「旧设置」，同样处理：取消再重新勾一次。
  · 「开机自启」是靠系统「任务计划程序」里一个叫 CampusLogin 的任务实现的
    （开机登录后自动跑一次）。所以任务管理器里的「启动应用」列表看不到它，
    要去任务计划程序里看。在界面里把勾取消，这个任务会被删掉。
  · 出问题时看日志：C:\ProgramData\CampusLogin\campus-login.log
""")


# 命令行开关清单，**故意不并进 HELP_TEXT**（2026-09-19 决定）：
#   1. HELP_TEXT 是给普通用户的「使用说明」，同一份文本也在界面的说明窗口里显示
#      （run_gui 里 txt.insert("1.0", HELP_TEXT)）。往里面塞 --xxx 只会让
#      第一次用的人莫名其妙。
#   2. 更要紧的是 help_check.py 会在**打包后的 exe 归档里逐字节核对** HELP_TEXT。
#      往里加字会让那条检查报 BAD —— 那不是「检查过时了」，是提醒你别动这段文本。
# 所以 --help 打的是两截：HELP_TEXT + 本段。改这里不影响 help_check.py。
#
# ⚠️ 写这里的每一条都必须在 main() 里真有对应分支，且行为与描述一致 ——
#    这份清单是用户排查问题的第一手依据，写错比不写更坏。
SWITCH_HELP = r"""
【命令行开关】

  平时用不到：双击 exe 走界面就行。下面这些是给排障和脚本用的。

  --help / -h / /?    显示这份说明，然后退出
                      （没有终端时——比如双击——会弹一个窗口显示）
  --version / -v      显示版本号，然后退出
                      （打包版没有终端，会弹一个窗口显示）
  --purge             清掉本机保存的账号密码；退出码 0=清干净了，1=有文件删不掉
  --selftest          打一份环境自检：版本、数据目录、自启参数、本机 IP、
                      联没联网、在不在校园网、更新地址……
                      结果同时写到数据目录下的 selftest.txt
  --checkupdate       只查一次更新，不下载也不替换；结果写 checkupdate.txt。
                      退出码 0=已是最新或有新版，1=没查出来
  --guitest           无窗口地构建一遍界面，用来确认界面运行库打包完整
                      （结果写 guitest.txt）
  --switch <学号>     切换到指定账号，密码取自已保存的账号。
                      没给学号会打印用法并退出（退出码 2）
  --auto              只做一次自动登录，然后退出
  --auto --guard      自动登录后继续守护 30 分钟（开机自启用的就是这两个）
  --guard             先试一次登录，再进守护（单独用，排障用）
  --logout            主动退出当前账号（下线）

  上面这些命令都是「跑完就退出」，不会弹主界面。
  不带任何开关（直接双击）就是正常开界面。
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


def window_geometry(sw, sh, reqh):
    """按屏幕尺寸和内容需要的高度算窗口几何，返回 (w, h, x, y)。

    **宽度只按屏幕算**，跟内容想要多宽无关（内容宽度由下面 820 的下限兜住）。
    原来签名里还有个 `reqw` 参数，但全仓没有任何调用点传它、函数体里也从不使用
    —— 一个"看起来会让宽度自适应内容、实际完全不影响"的死参数，只会误导人，
    已经删掉。真要按内容调宽度，得先有调用点传 `winfo_reqwidth()` 再说。

    **高度必须按屏幕收口。** 原来的写法只把高度限制在 560..1000，完全没看屏幕多高 ——
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


def drain_queue(q, handle, empty_exc=queue.Empty):
    """把队列里的消息全部处理掉，返回 (成功条数, 出错条数)。

    抽成模块级函数是为了**能测** —— 原来这段逻辑内嵌在 GUI 闭包里，
    不建真窗口就碰不到，于是它带着一个 bug 活了很久。

    两条设计要点，都是踩过的坑：

      1. 「队列空了」用 `queue.Empty` **精确**判定，它是正常结束条件，不是错误。
         原来写成 `except Exception: pass` 把两者混在一起，结果是真错误被当成
         "没消息了"静默吞掉，日志里一个字都看不到，排查时无从下手。
      2. 单条消息出错只影响那一条，后面的继续处理。原来一条出错就中断整批，
         排队等着的其它消息（比如网络状态更新）永远处理不到。

    注意：`handle` 自己负责"不管出什么事都要收尾"的清理（比如解除忙碌状态），
    用 try/finally 写在它里面。
    """
    n_ok = 0
    n_err = 0
    while True:
        try:
            msg = q.get_nowait()
        except empty_exc:
            break                       # 队列空了 = 正常，不是错误
        try:
            handle(msg)
            n_ok += 1
        except Exception:
            n_err += 1
            log("界面刷新异常:\n%s" % traceback.format_exc())
    return n_ok, n_err


# ==================== 界面 ====================
def update_dest_path(exe=None):
    """更新下载文件（`.new` / `.new.part`）该落到哪。源码运行时返回 None。

    **为什么抽成模块级函数**（而不是像原来那样写在 start_upgrade 的闭包里）：
        闭包只有点界面才跑得到，脚本断言不了 —— 于是「临时文件到底落在哪」
        这件事**一直没被验证过**，0.3.2 之前就因此把 `.old` 丢在了用户桌面上。
        现在 `--selftest` 直接报这个路径，verify_exe.py 拿真 exe 跑一遍就能断言。
        （和 `_self_update_desc()` 同一个理由。）

    exe=None → 用当前进程（打包时是 exe 自己，源码运行时是 None）。
    """
    if exe is None:
        exe = sys.executable if is_frozen() else None
    if not exe:
        return None
    # work_dir_for 会判同卷：数据目录若在别的盘，os.replace 跨卷会直接失败，
    # 此时它自己退回 exe 同目录 —— 宁可脏一点，也不能更新不了。
    return os.path.join(updater.work_dir_for(exe, data_dir()),
                        os.path.basename(exe) + ".new")


def _self_update_desc():
    """给 --selftest 用的一句话：能不能自我替换，不能的话为什么。

    抽出来是为了 selftest 那段保持「只拼字符串」——那里已经有 14 行字面量了，
    再塞判断逻辑进去，以后改起来没人敢动。
    """
    try:
        ok, detail = updater.can_self_update()
    except Exception:
        return "判定失败（见日志）"
    return "能（%s）" % detail if ok else "不能（%s）" % detail


def update_wiring_check(btn_ver):
    """在真实界面里验证「版本号小字 → 检查更新」这条接线真的通了。

    为什么要有这一条（和 pwd_wiring_check 同一个理由）：
    `updater.py` 的自检测的是纯逻辑，`grep 'bind("<Button-1>")'` 只说明
    源码里出现过这行字 —— **都证明不了「点下去真的会跑检查」**。常见的静默
    失败是：控件被别的控件盖住、bind 写在了没 pack 的对象上、lambda 抓错闭包。
    这些光看代码都看不出来，表现就是「用户点了版本号，什么也没发生」，
    而且**没有任何报错**。

    做法：把 `updater.check` 临时换成探针，往那个 Label 上真发一个
    <Button-1>，然后确认探针被调到了 —— 这验证的是**真实的那条调用链**，
    而不是我另外写的一个平行函数。比对完把探针换回去。
    """
    if btn_ver is None:
        raise AssertionError("版本号控件不存在，标题行的更新入口没建出来")
    # 文案只做**前缀**匹配（`v0.1.0`），不要求整串相等 ——
    # 后面可能为了好看加/改后缀（比如「检查更新」），
    # 写死整串会让测试因为一次文案微调就变红，那种红没人愿意查。
    text = str(btn_ver.cget("text"))
    if not text.startswith("v%s" % VERSION):
        raise AssertionError(
            "更新入口控件的文字是 %r，应以 'v%s' 开头（版本号必须能被看到）" % (
                text, VERSION))
    # 1. 控件得是「看起来能点」的（否则用户根本不会去点）。
    cur = str(btn_ver.cget("cursor"))
    if cur != "hand2":
        raise AssertionError("版本号控件的 cursor 是 %r，应为 hand2（看不出可点）" % cur)
    # 2. 得真的挂着 <Button-1> 的绑定。
    if not btn_ver.bind("<Button-1>"):
        raise AssertionError("版本号控件没有绑定 <Button-1>，点了不会有反应")

    # 3. 换探针，真发点击事件，看它会不会被调到。
    calls = []
    real = updater.check

    def probe(cur_ver, *a, **k):
        calls.append(cur_ver)
        # 返回 CURRENT 是最短路径：不弹确认框、不下载。
        return updater.STATE_CURRENT, cur_ver

    updater.check = probe
    try:
        btn_ver.event_generate("<Button-1>")
        # 事件处理里是起后台线程 + 往队列丢消息，等一小会儿让线程跑起来。
        # 不能只 update_idletasks —— 那不等线程。
        for _ in range(40):
            btn_ver.update()
            if calls:
                break
            time.sleep(0.02)
    finally:
        updater.check = real

    if not calls:
        raise AssertionError(
            "给版本号控件发了 <Button-1>，但 updater.check 没被调用（接线断了）")
    return "更新入口接线：cursor=hand2、<Button-1> 已绑定、点击确实触发了检查"


# 界面配色。**提到模块级**是为了让 run_gui 之外的窗口也能用同一套 ——
# `--help` 在打包版里要弹一个说明窗口（没有 stdout，print 出去看不见），
# 颜色写两份迟早会漂移，两块界面就不一样了。
BG = "#f0f2f5"
CARD = "#ffffff"
FG = "#222222"
SUB = "#666666"
BLUE = "#0078d4"


def _ui_font(root, tkfont):
    """按屏幕高度挑字号 + 挑一个装得上的中文字体。返回 (字体名, 字号)。"""
    try:
        sh = root.winfo_screenheight()
    except Exception:
        sh = 1080
    base = 12 if sh >= 1400 else 10
    fam = "Microsoft YaHei UI"
    try:
        if fam not in tkfont.families():
            fam = "Microsoft YaHei"
    except Exception:
        fam = "Microsoft YaHei"
    return fam, base


def show_text_window(title, text, width=780, height=640):
    """把一个长文本用「可滚动、可复制」的只读窗口显示出来。

    为什么需要它（2026-09-19 实测）：打包版是 `--windowed`，`sys.stdout` 是 None，
    `print` 出去的东西直接进 devnull。`--help` 如果只 print，用户双击、或者在
    cmd 里敲，都**什么都看不到** —— 只会觉得「这命令没反应」，比不实现还糟。
    `--version` 早就为同一件事弹窗了，这里保持一致。

    返回 True = 用户把窗口关掉了；False = 窗口没建起来（调用方自己兜底）。
    """
    try:
        import tkinter as tk
        import tkinter.font as tkfont

        r = tk.Tk()
        r.title(title)
        r.configure(bg=BG)
        ico = icon_path()
        if ico:
            try:
                r.iconbitmap(default=ico)
            except Exception:
                pass
        fam, base = _ui_font(r, tkfont)
        r.geometry("%dx%d" % (width, height))

        body = tk.Frame(r, bg=CARD)
        body.pack(fill="both", expand=True, padx=14, pady=(12, 0))
        txt = tk.Text(body, wrap="word", font=(fam, base), bg=CARD, fg=FG,
                      relief="flat", bd=0, padx=6, pady=2, cursor="arrow")
        sb = tk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", text)
        txt.configure(state="disabled")      # 只读；仍可选中、可 Ctrl+C 复制

        bar = tk.Frame(r, bg=CARD)
        bar.pack(fill="x", padx=14, pady=12)
        tk.Button(bar, text="知道了", font=(fam, base), bg=BLUE, fg="#ffffff",
                  activebackground="#106ebe", activeforeground="#ffffff",
                  relief="flat", bd=0, cursor="hand2", padx=26, pady=6,
                  command=r.destroy).pack(side="right")
        r.mainloop()
        return True
    except Exception:
        log("显示文本窗口失败:\n%s" % traceback.format_exc())
        return False


def run_gui(smoke=False):
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import messagebox

    arm_exit_watchdog()
    exit_trace("run_gui 开始（smoke=%s）" % smoke)

    root = tk.Tk()
    if smoke:
        root.withdraw()          # 自检时不显示窗口
    root.title("%s v%s · 设置" % (APP_TITLE, VERSION))
    root.configure(bg=BG)

    # 按屏幕高度决定字号，避免高分屏上字太小。
    # 字体挑选和字号规则跟 `--help` 的说明窗口共用一份（_ui_font），别再抄一遍。
    fam, BASE = _ui_font(root, tkfont)
    TITLE = BASE + 5
    SMALL = BASE - 2

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
    # 版本号紧跟在标题右边，小字弱色。用户要报问题时第一眼就能看到它。
    # **同时它也是「检查更新」的入口**（做成链接样式的小字，不新增第四个按钮 ——
    # 主按钮固定三个是既有的界面铁律，改按钮要同步改 set_busy 的控件元组）。
    #
    # ⚠️ 视觉上必须让人看出「这行字能点」：只绑 cursor 是没用的 ——
    # 截图里它跟普通灰字一模一样，用户根本不会去点。所以给它加：
    #   ① 下划线（链接的通用约定）
    #   ② 浅蓝底色（和后面的灰底「使用说明」按钮区分开，但不抢主按钮的视觉）
    # 这两个加起来，一眼就知道是可点的东西。
    btn_ver = tk.Label(head, text="v%s　检查更新" % VERSION,
                       font=(fam, SMALL, "underline"),
                       fg="#0b5ed7", bg="#e7f0ff", cursor="hand2", padx=7, pady=1)
    btn_ver.pack(side="left", padx=(8, 0))
    # hover 时加深底色，进一步确认「可点」。
    btn_ver.bind("<Enter>", lambda e: btn_ver.configure(bg="#d2e4ff"))
    btn_ver.bind("<Leave>", lambda e: btn_ver.configure(bg="#e7f0ff"))
    btn_ver.bind("<Button-1>", lambda e: on_check_update())
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
            old_args = autostart_outdated()
        except Exception:
            stale = old_args = False
        # 注意：这里是普通 Label，**不认 Markdown** —— 用「」而不是 ** 来强调，
        # 否则用户看到的就是一串星号（老版本就是这么显示的）。
        if stale:
            warn_auto.configure(
                text="⚠ 开机自启项指向的是 exe 的「旧位置」，开机时实际不会生效。\n"
                     "   把下面的勾取消、再重新勾选一次即可修好。")
            warn_auto.pack(anchor="w", pady=(6, 0))
        elif old_args:
            # 0.2.0 装的 .lnk 参数是 `--auto`，没有 `--guard` —— 开机照常登录，
            # 但掉线后不会自动重连。不提示的话用户会以为新版本没修好。
            warn_auto.configure(
                text="⚠ 开机自启还是「旧设置」：开机照常登录，但掉线后不会自动重连。\n"
                     "   把下面的勾取消、再重新勾选一次即可升级。")
            warn_auto.pack(anchor="w", pady=(6, 0))
        else:
            warn_auto.pack_forget()

    # --- 状态 ---
    state = {"busy": False, "queue": None, "buttons": None,
             # 升级用的临时状态。都要走界面线程，所以放在这里而不是局部变量。
             "pending": None, "dl_text": ""}

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
        # 升级期间版本号小字也不能点，否则会并发起两个检查/两次替换。
        try:
            btn_ver.configure(cursor="arrow" if b else "hand2")
        except Exception:
            pass
        # 用「改文案」而不是「加控件」来表示忙碌：不加第四个按钮，
        # 也就不用去动 set_busy 那个控件元组（那是最容易被改漏的地方）。
        try:
            if b and state["dl_text"]:
                btn_ver.configure(text=state["dl_text"])
            elif not b:
                btn_ver.configure(text="v%s　检查更新" % VERSION,
                                  fg="#0b5ed7", bg="#e7f0ff",
                                  font=(fam, SMALL, "underline"))
        except Exception:
            pass

    def refresh_status():
        def work():
            st = network_state()
            # 程序刚起来时第一个网络请求赶上 DNS 冷启动（实测首次 7.4 秒），
            # 有可能「测不出来」。隔几秒再试一次，免得状态栏一直停在未知上
            # ——用户打开程序第一眼看到的就是这行状态。
            if st == NET_UNKNOWN:
                time.sleep(3)
                st = network_state()
            state["queue"].put(("status", st))
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

    def handle_msg(msg):
        """处理一条队列消息。异常往上抛，由 drain_queue 记日志并继续下一条。"""
        kind, payload = msg
        if kind == "status":
            st = payload
            dot.itemconfigure(dot_id, fill=NET_COLOR.get(st, "#bbbbbb"))
            status_text.configure(text=NET_TEXT.get(st, "网络状态未知"))
        elif kind == "job":
            code, text = payload
            try:
                load_all()
                show(text, "ok" if code == 0 else "err")
                refresh_status()
            finally:
                # **必须在 finally 里**：上面任何一步抛异常，界面就会永久卡在
                # "忙碌"——三个按钮全灰点不动，而用户看不到任何原因，
                # 看起来就是"窗口卡死了"。这正是最难查的那种症状。
                set_busy(False)
        elif kind == "upd_progress":
            # 下载进度。只改文案，不碰按钮状态 —— 进度消息会来很多次，
            # 每次都去动控件代价大而且容易闪。
            state["dl_text"] = payload
            try:
                btn_ver.configure(text=payload)
            except Exception:
                pass
        elif kind == "upd_result":
            # 参数：state（updater 的四态之一）+ info
            st, info = payload
            try:
                if st == updater.STATE_NEWER:
                    # 有新版且校验信息齐全 → 问用户要不要升级。
                    # **这一步必须在界面线程里问**，后台线程弹 messagebox 会出问题。
                    v = info["version"]
                    ok = messagebox.askyesno(
                        APP_TITLE,
                        "发现新版本 v%s。\n\n%s\n\n是否现在下载并升级？\n"
                        "（升级过程会关闭当前窗口，稍后自动重开）" % (
                            v, info.get("notes") or "无更新说明"))
                    if not ok:
                        show("已跳过升级，当前仍是 v%s" % VERSION, "info")
                        set_busy(False)
                        return
                    state["pending"] = info
                    state["dl_text"] = "下载中 0%"
                    show("正在下载 v%s…" % v, "info")
                    start_upgrade(info)
                elif st == "done":
                    # 替换已经成功，现在重启到新版本。
                    show("升级完成，正在重新启动…", "ok")
                    root.update_idletasks()
                    restart_self()
                else:
                    show(updater.describe(st, info),
                         "ok" if st in (updater.STATE_CURRENT,) else "info")
                    set_busy(False)
            except Exception:
                # 任何意外都不能让界面卡在忙碌态。
                log("处理更新结果异常:\n%s" % traceback.format_exc())
                show("检查更新时出错，请查看日志", "err")
                set_busy(False)

    def start_upgrade(info):
        """下载 + 校验 + 替换自己。全在后台线程里做，界面只收进度。"""
        def work():
            exe = None
            dest = None
            try:
                exe = sys.executable if getattr(sys, "frozen", False) else None
                if exe is None:
                    state["queue"].put(("upd_result", (
                        updater.STATE_MANUAL, "当前是源码运行，无法自我替换，请手动下载")))
                    return
                # 更新临时文件（.new.part / .new / .old）统一放**程序数据目录**，
                # 不放 exe 同目录。理由（2026-09-20 用户反馈）：用户常把 exe 直接
                # 放桌面 —— 下载期间桌面上会多出一个 12 MB 的 `.new.part`，
                # 替换后还会留下几 MB 的 `.old`（要等下次启动才删）。
                # 对不懂的人来说就是「桌面莫名多了个怪文件，又不敢删，删错了程序还没了」。
                # 路径只算一处（update_dest_path）—— --selftest 报的也是它，
                # 免得「界面往东、自检报西」。
                dest = update_dest_path(exe)
                work_dir = os.path.dirname(dest)
                state["queue"].put(("upd_progress", "下载中 0%"))

                def on_prog(got, total):
                    # 节流：每 3% 才推一次，不然队列会被进度消息淹没。
                    pct = 0 if not total else int(got * 100 / total)
                    if pct < 3 or pct == getattr(on_prog, "_last", -1):
                        return
                    if pct < getattr(on_prog, "_last", 0) + 3 and pct != 100:
                        return
                    on_prog._last = pct
                    state["queue"].put(("upd_progress", "下载中 %d%%" % pct))

                def on_retry(attempt, attempts, why):
                    # 重试必须说一句：否则进度会卡在某个百分比不动，
                    # 用户以为死机了就会再点一次 —— 那就并发下两份、还可能互相覆盖。
                    state["queue"].put(("upd_progress", "网络中断，正在重试 %d/%d…"
                                        % (attempt, attempts)))

                ok, reason = updater.download(
                    info["url"], dest, sha256=info.get("sha256"),
                    size=info.get("size"), on_progress=on_prog, on_retry=on_retry)
                if not ok:
                    # 下载失败时 download() 自己会清掉 `.part`，但 `.new` 可能
                    # 是上一轮留下的（下载成功、替换失败）。12 MB 的东西别留在盘上。
                    updater.discard(dest)
                    state["queue"].put(("upd_result", (
                        updater.STATE_UNKNOWN, "下载失败：%s" % reason)))
                    return

                state["queue"].put(("upd_progress", "正在替换…"))
                # work_dir 必须一路传下去：`.old` 落在哪由它决定，
                # 漏传就退回 exe 同目录（桌面），等于这次修复没生效。
                ok, reason = updater.apply_update(exe, dest, work_dir)
                if not ok:
                    updater.discard(dest)
                    state["queue"].put(("upd_result", (
                        updater.STATE_UNKNOWN, "升级失败：%s" % reason)))
                    return
                # 替换成功 → 重启新版本。**先放消息再退出**，让界面把提示显示出来。
                state["queue"].put(("upd_result", (
                    "done", "升级完成，正在重新启动…")))
            except Exception:
                log("升级异常:\n%s" % traceback.format_exc())
                # 异常路径同样别留残骸（`.part` 可能下到一半）。清不掉也无所谓，
                # 下次启动的 cleanup_stale() 还会再扫一遍。
                if dest:
                    updater.discard(dest + ".part")
                    updater.discard(dest)
                state["queue"].put(("upd_result", (
                    updater.STATE_UNKNOWN, "升级过程出错，请查看日志")))
        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            drain_queue(state["queue"], handle_msg)
        except Exception:
            log("界面轮询异常:\n%s" % traceback.format_exc())
        # 无论上面发生了什么，下一次轮询都要排上。否则轮询链条一断，
        # 界面就再也不刷新了（后台任务的结果永远回不来）。
        try:
            root.after(120, poll)
        except Exception:
            log("排下一次轮询失败:\n%s" % traceback.format_exc())

    # --- 按钮动作 ---
    def restart_self():
        """用新 exe 重新拉起自己，然后硬退出当前进程。

        为什么必须「先起新的、再退旧的」：
        `updater.apply_update` 已经把旧 exe 改名成 `.old`、新 exe 写到原路径。
        这里只是启动同一个路径，拿到的是新版本。
        ⚠️ `.old` 现在落在**程序数据目录**（不是 exe 同目录，见 updater.work_dir_for）。
        所以万一它这次删不掉（旧进程刚 TerminateProcess、句柄还没放干净），
        也只是在数据目录里躺到下次启动 —— 用户看不到，桌面不会被污染。
        ⚠️ 退出走 exit_now()（内部是 TerminateProcess）—— 不能用 os._exit()，
        那会在 Tcl/Tk 的 DLL_PROCESS_DETACH 上卡 11~12 秒甚至永久卡死。
        """
        try:
            subprocess.Popen([sys.executable], close_fds=True)
        except Exception:
            log("重启自身失败:\n%s" % traceback.format_exc())
            show("升级已完成，请手动重新打开程序", "ok")
            set_busy(False)
            return
        exit_now(0)

    def on_check_update():
        """点版本号触发。全流程在后台线程，界面只收队列消息。"""
        if state["busy"]:
            show("正在处理上一个操作，请稍候", "info")
            return
        state["dl_text"] = ""
        set_busy(True)
        show("正在检查更新…", "info")

        def work():
            try:
                st, info = updater.check(VERSION)
            except Exception:
                log("检查更新异常:\n%s" % traceback.format_exc())
                st, info = updater.STATE_UNKNOWN, "检查更新时出错"
            state["queue"].put(("upd_result", (st, info)))
        threading.Thread(target=work, daemon=True).start()

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
        win.title("使用说明 · %s v%s" % (APP_TITLE, VERSION))
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
        txt.insert("1.0", HELP_TEXT + SWITCH_HELP)
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
        if not want:
            show("已关闭开机自启", "ok")
            return
        # 说清楚走的是哪条路。只有建不了计划任务时才会退回启动文件夹，
        # 而那条路要等到开机后约 85 秒（计划任务约 25 秒）—— 不告诉用户的话，
        # 他下次开机还是会觉得「怎么又这么慢」。
        if autostart_mode() == "task":
            show("已开启开机自启", "ok")
        else:
            show("已开启开机自启（本机退回用「启动文件夹」，开机会慢约 1 分钟）", "ok")

    # --- 启动 ---
    # queue 已在模块顶层导入（poll() 要按名字捕获 queue.Empty）
    state["queue"] = queue.Queue()
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
        # 更新入口接线自检：真给版本号小字发一个点击，确认整条链通。
        # 同 pwd_wiring_check —— 光看代码证明不了「点下去真的会跑」。
        log(update_wiring_check(btn_ver))
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
    # 点 X 时先记一笔。行为与 Tk 默认（无 handler 时直接 destroy）完全一致，
    # 只是为了知道「关闭请求确实到了主线程」，把卡点范围再收窄一格。
    def _on_wm_close():
        exit_trace("收到 WM_DELETE_WINDOW（用户点了 X）")
        root.destroy()
        exit_trace("root.destroy() 返回")

    root.protocol("WM_DELETE_WINDOW", _on_wm_close)
    exit_trace("即将进入 mainloop")
    root.mainloop()
    exit_trace("mainloop 已返回")
    # mainloop 返回后**不要再走解释器正常收尾**：--windowed 打包后 Tk 在收尾阶段
    # 偶发死锁（界面已经关掉、文件也落盘了，进程却一直不退，实测卡过 10 分钟以上），
    # 结果是留一个看不见的僵尸进程在那儿占着。
    # 只在**打包后**硬退：从源码跑时（ui_shot.py / layout_probe.py 会调 run_gui）
    # 要让它正常返回，否则脚本自己的收尾输出会被这一下截掉。
    if is_frozen():
        exit_now(0)
    exit_trace("源码运行：正常返回，不硬退")
    return 0


def hard_kill(code):
    """不执行 DLL 卸载回调地把本进程干掉。Windows 上没杀成则返回 False。

    为什么不能用 os._exit：`os._exit` → CRT `_exit` → **`ExitProcess`**，
    而 ExitProcess 的第 3 步是「依次调用所有已加载 DLL 的 DLL_PROCESS_DETACH」。
    实测（2026-09-17，插桩 exit_trace_probe.py）：卡点精确落在这一步 ——
    日志停在「即将硬退」之后再无输出，faulthandler 的 20s 定时器线程
    也被 ExitProcess 提前终止（dump 文件 0 字节），子进程 CPU 70 秒只涨 0.4s
    （阻塞等待，非忙等）。典型成因是某个线程被终止时正持有加载器锁，
    detach 枚举就永远等下去；本程序有 daemon 线程在跑网络轮询，
    退出瞬间正好撞上就是**偶发**的。

    `TerminateProcess` 不走 DLL 卸载，因此没有这个死锁面。代价是不执行
    CRT 的 atexit / 缓冲刷新 —— 本程序所有文件写入都用 `with open(...)`
    或 `os.replace` 即时落盘，没有待刷的缓冲，所以没有影响。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.TerminateProcess.restype = ctypes.c_int
        # 成功的话这里不会返回；返回了就说明没杀成，交给调用方兜底。
        k32.TerminateProcess(k32.GetCurrentProcess(), code & 0xFFFFFFFF)
        exit_trace("!! TerminateProcess 返回了（没杀成），退回 os._exit")
        return False
    except Exception:
        exit_trace("TerminateProcess 异常:\n%s" % traceback.format_exc())
        return False


# ==================== 入口 ====================
def exit_now(code):
    """立刻结束进程，不走 Python 的正常收尾。

    坑一（已修）：--windowed 打包后，Tk 在解释器收尾阶段偶发死锁——界面已经
    建好、结果文件也落盘了，进程却一直不退出。所以这里直接硬退，不跑
    mainloop 之后的解释器收尾。

    坑二（已修，2026-09-17）：光换成 os._exit 还不够 —— `os._exit` 在
    Windows 上仍走 ExitProcess，会执行 DLL_PROCESS_DETACH，在那一步同样会
    偶发死锁（详见 hard_kill 的说明）。现在优先用 TerminateProcess。

    安全性：所有文件写入都用 `with open(...)` 或 `os.replace` 即时落盘，
    没有待刷的缓冲；onefile 的 _MEI 临时目录由 bootloader 父进程负责清理，
    子进程硬退不影响。
    """
    exit_trace("exit_now(%r) 进入" % code)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    exit_trace("即将硬退（TerminateProcess）")
    hard_kill(code)
    # 非 Windows，或 TerminateProcess 意外没杀成：退回 os._exit。
    os._exit(code)


def main():
    # --windowed 打包后 stdout/stderr 是 None，某些库会因此报错。
    # 显式指定 utf-8 + errors="replace"：中文 Windows 的默认编码是 cp936，
    # 一旦有代码 print 出 cp936 表示不了的字符（emoji 之类）就会抛
    # UnicodeEncodeError —— 而这里本来就是为了"兜住异常"才接的 devnull。
    # ⚠️ 先把「本来有没有 stdout」记下来，**再**做下面的 devnull 兜底 ——
    #    兜底之后 sys.stdout 永远不是 None，就再也分辨不出「双击（没有终端）」
    #    和「从终端里启动」了。`--help` 要靠这个决定打印还是弹窗（见那边的注释）。
    had_stdout = sys.stdout is not None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

    args = [a.lower() for a in sys.argv[1:]]

    # --version：纯查询，没有副作用，所以放在最前面。
    # 也**必须**在 migrate 之前 —— 只想问一句版本号，不该顺手改动数据目录。
    # （args 已转小写，所以 -V 在这里就是 -v，不用另判一次。）
    if "--version" in args or "-v" in args:
        text = "%s %s" % (APP_TITLE, VERSION)
        if is_frozen():
            # 坑：--windowed 打包出来的 exe 没有 stdout（sys.stdout 是 None），
            # print 出来的东西直接扔掉，用户什么也看不到。弹窗才看得见。
            try:
                import tkinter as _tk
                import tkinter.messagebox as _mb
                _r = _tk.Tk()
                _r.withdraw()
                _mb.showinfo("版本", text)
                _r.destroy()
            except Exception:
                log("--version 弹窗失败:\n%s" % traceback.format_exc())
        else:
            print(text)
        exit_now(0)

    # --help：把内置的使用说明打到终端。同 --version，纯查询、不碰数据目录。
    #
    # ⚠️ 必须在**落到 GUI 之前**认领它（2026-09-19 实测踩到）：0.3.1 之前没有这个
    #    分支，`--help` 不被任何分支认领 → 一路走到最后开 GUI 弹个窗口；而
    #    --windowed 的 exe 没有 stdout，用户既看不到说明也看不到任何错误，
    #    只会觉得「敲了个 --help 就弹窗，莫名其妙」。
    #    这个意图一直写在 HELP_TEXT 上方的注释里（「排障用的 --selftest 之类
    #    留在 --help 里」），但分支本身从没实现 —— 注释描述的是设计意图，不是现状，
    #    别拿它当「这功能已经在了」的证据。0.3.1 补上。
    # 顺带认 `-h`（Unix 习惯）和 `/?`（Windows 习惯）。args 已转小写，够用。
    #
    # 输出是两截：HELP_TEXT（面向使用者，也被界面上的说明窗口复用）
    #             + SWITCH_HELP（命令行开关）。
    # 分开的理由写在 SWITCH_HELP 上面 —— 核心是别动 HELP_TEXT，
    # help_check.py 要在打包后的 exe 里逐字节核对它。
    #
    # ⚠️ 打包版要**弹窗**，不能只 print（2026-09-19 实测）：`--windowed` 的 exe
    #    双击起来没有可用的 stdout，print 出去就没了 —— 用户敲了 --help 什么也
    #    看不到，比不实现还糟。
    #    ⚠️ 但**不能只看 is_frozen 就弹窗**：从终端里启动时标准句柄是继承来的，
    #       实测在 Git Bash 里跑 `CampusLogin.exe --help`，文字真的打到了终端上。
    #       那种情况下弹窗反而多此一举（2800 字的说明塞进弹窗也不好读）。
    #       所以判据是「打包版 **且** 没有 stdout」才弹窗 —— had_stdout 是 main()
    #       开头、做 devnull 兜底**之前**记下来的（兜底之后就永远不是 None 了）。
    #    ⚠️ 这里和 `--version` 的行为**故意不一样**：--version 只要打包就弹窗。
    #       别顺手去「统一」它们 —— version_test.py 第 6 节正把 --version 的
    #       「10 秒不退出（停在模态窗上）」当成成功信号在断言。
    if "--help" in args or "-h" in args or "/?" in args:
        text = HELP_TEXT + SWITCH_HELP
        if is_frozen() and not had_stdout:
            if not show_text_window("%s v%s · 命令行说明" % (APP_TITLE, VERSION), text):
                log("--help 的窗口没能显示出来，说明文字没能到用户眼前")
        else:
            try:
                print(text)
            except UnicodeEncodeError:
                # 系统区域不是中文时，控制台代码页放不下这些汉字。一个「帮助」命令
                # 自己崩掉是最糟的失败方式，所以降级成「能打多少打多少」。
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(text.encode(enc, "replace").decode(enc, "replace"))
        exit_now(0)

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
        # 不记日志的话，新机器上"账号没继承过来"就完全没线索：
        # 用户只看到一张空表单，分不清是"本来就没有旧数据"还是"继承时炸了"。
        log("继承旧数据失败（不影响启动）:\n%s" % traceback.format_exc())

    # 老版本的自启挂在启动文件夹上，开机后要等 85 秒才轮到它（见 AUTOSTART_TASK_NAME
    # 那段实测）。升级后第一次运行就换成登录计划任务 —— 用户不该为了这次修复
    # 自己去「取消再勾一次」。
    try:
        autostart_upgrade()
    except Exception:
        log("自启升级检查失败（不影响启动）:\n%s" % traceback.format_exc())

    # 清理上一次自我更新留下的 .old / .new / .new.part。**必须放在这里**
    # （每次启动都跑）：替换时旧 exe 正被当前进程占用，删不掉；只能等它退出后、
    # 下次启动时清。失败不影响使用 —— 留一个几 MB 的残留文件而已。
    #
    # ⚠️ **必须传 data_dir()**：0.3.3 起更新临时文件落在数据目录，漏传就会退回
    #    exe 同目录（用户的桌面），新位置的 `.old` 永远没人清、一直累积。
    #    cleanup_stale 两个位置都扫，所以老版本（≤0.3.2）留在桌面的历史残留
    #    也能一并收掉 —— 只清新位置等于没修好。
    try:
        updater.cleanup_stale(sys.executable if is_frozen() else None, data_dir())
    except Exception:
        log("清理旧版本残留失败:\n%s" % traceback.format_exc())

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
        try:
            g_running, g_why = guard_running()
        except Exception:
            g_running, g_why = False, "读取失败"
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
            # 自启走的是哪条路 —— 这是排查「开机启动慢」的第一手信息。
            # 计划任务（登录时触发）在开机后约 25 秒跑起来，启动文件夹要等到约 85 秒
            # （实测见 AUTOSTART_TASK_NAME 那段）。
            "自启方式: %s" % {"task": "计划任务（登录时触发）",
                              "lnk": "启动文件夹（比计划任务晚约 60 秒）",
                              "": "未开启"}[autostart_mode()],
            "计划任务: %s" % task_def_path(),
            "计划任务存在: %s" % task_exists(),
            # 这两行是排查「升级了、掉线却还是不重连」的第一手信息：
            # 多半是自启项还停在旧参数（--auto，没有 --guard）上。
            "自启参数: %s" % (autostart_args() or "（读不出来）"),
            "自启参数陈旧: %s" % autostart_outdated(),
            "自启已失效: %s" % autostart_stale(),
            "守护: %s" % ("在跑（%s）" % g_why if g_running else "没在跑"),
            "本机IP: %s" % local_ipv4(),
            "本机MAC: %s" % local_mac(),
            "联网: %s" % {True: "已联网", False: "未认证（被门户拦截）",
                          None: "无法判定（请求异常/超时）"}[test_internet()],
            # ⚠️ 这两行必须**分开**报。合成一句就会重演那个 bug：
            #    「能上网」被当成「在校园网」，在宿舍 Wi-Fi 上也显示已连接校园网。
            "在校园网: %s" % {True: "是", False: "否（网段 %s，校园网段 %s）"
                              % (local_ipv4(), campus_subnets()),
                              None: "无法判定"}[on_campus_network(1.5)],
            # 更新入口。这两行是排查「检查更新失败」时的第一手信息 ——
            # 不用去猜程序里写的是哪个地址。
            # ⚠️ 只列地址，**不发网络请求**：--selftest 本来是快操作（3 秒），
            #    加一次联网会拖慢它，而且网络结果本来就该用 --checkupdate 单独看。
            "更新清单: %s" % updater.MANIFEST_URL,
            "更新兜底: %s" % updater.FALLBACK_MANIFEST_URL,
            "可自我更新: %s" % _self_update_desc(),
            # 更新临时文件（.new/.new.part）和旧版本备份（.old）落在哪个目录。
            # **这行是「更新不再污染桌面」这条修复唯一的验证通道** —— GUI 里那段
            # 路径计算脚本点不到，只能靠它报出来做断言（见 verify_exe.py）。
            # 期望值 = 上面的「数据目录」；**绝不是「程序路径」所在的那个目录**。
            "更新临时目录: %s" % (os.path.dirname(update_dest_path() or "")
                                  or "（源码运行，不更新）"),
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
            # 落盘失败要留痕：这个文件是 --windowed 打包后**唯一**能验到结果的通道
            # （那种 exe 没有 stdout）。写不进去却没人知道，验证脚本就只能去读
            # 上一轮留下的旧文件 —— 那会报出"假通过"。
            log("selftest.txt 写入失败:\n%s" % traceback.format_exc())
        exit_now(0)

    # --checkupdate：命令行查一次版本（不下载、不替换）。
    # 有这个分支是为了**能自动化验证**：--windowed 的 exe 没有 stdout，
    # 光靠点界面没法在脚本里断言「更新接口通不通」。结果同时落盘。
    if "--checkupdate" in args:
        st, info = updater.check(VERSION)
        text = "检查更新: %s" % updater.describe(st, info)
        print(text)
        try:
            with open(os.path.join(data_dir(), "checkupdate.txt"), "w",
                      encoding="utf-8") as f:
                f.write("%s\n状态: %s\n清单地址: %s\n" % (
                    text, st, updater.MANIFEST_URL))
        except Exception:
            log("checkupdate.txt 写入失败:\n%s" % traceback.format_exc())
        exit_now(0 if st in (updater.STATE_CURRENT, updater.STATE_NEWER) else 1)

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
            # 同 selftest.txt：这是 --windowed 下唯一的验证通道，写失败必须留痕，
            # 否则验证脚本会去读上一轮的旧标记，把失败报成通过。
            log("guitest.txt 写入失败:\n%s" % traceback.format_exc())
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
        code = 1
        try:
            code = do_auto()
        except Exception:
            log("auto 异常:\n%s" % traceback.format_exc())
        if "--guard" in args:
            # ⚠️ **不能** exit_now(do_auto() 的返回值) —— 那会带着失败码退出，
            #    守护根本没机会跑。而 do_auto 失败恰恰是最需要守护的时候
            #    （开机那一下没连上，30 秒后网络就绪了，守护能补上）。
            try:
                code = run_guard()
            except Exception:
                log("guard 异常:\n%s" % traceback.format_exc())
        exit_now(code)
    if "--guard" in args:
        # 单独给 --guard 也能跑：先试一次登录，再进守护。排障时用。
        try:
            do_auto(attempts=1)
        except Exception:
            log("auto 异常:\n%s" % traceback.format_exc())
        try:
            exit_now(run_guard())
        except Exception:
            log("guard 异常:\n%s" % traceback.format_exc())
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
