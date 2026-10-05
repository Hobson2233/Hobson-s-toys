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

# 界面配色。**和站点 CSS 共用一份**（2026-10-05 抽出，理由见 palette.py）。
# 以前这 19 个常量长在本文件里，而站点那边另有一份手工同步的副本 ——
# 实测两边重合 15 个，改配色要记得改两处，迟早会漏。
# 和 updater 同理：顶层 import，**打包时必须让 PyInstaller 看见它**。
from palette import (BG, CARD, FG, SUB, LINE, FIELD, SEC_FG,
                     BLUE, BLUE_DARK, BLUE_SOFT,
                     OK, OK_SOFT, DANGER, DANGER_SOFT, WARN,
                     VIOLET, VIOLET_SOFT, CYAN, CYAN_SOFT)

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
VERSION = "0.4.2"

# ==================== 学校参数：换学校只改 config.json ====================
#
# 🔴 本节列出的键 + 下面那几个常量，是**所有「跟哪所学校有关」的默认值**。
#    换学校 / 换校区**不用改源码** —— 把要改的键写进本机 config.json 就行：
#        %ProgramData%\CampusLogin\config.json
#    （README 的「适配其它学校」一节列的正是这几个键，两边要一起改。）
#
#    可覆盖的键（config.json 键名 → 默认值 → 取值函数）：
#        portalHost      门户主机          "http://202.196.169.166"     load_config()
#        wlanAcIp        接入控制器 IP     "10.0.1.10"                  load_config()
#        wlanAcName      接入控制器名      "ZZHK-ZAX-BRAS"              load_config()
#        timeoutSec      超时（秒）        8                            load_config()
#        campusSubnets   校园网段前缀      ["10."]                      campus_subnets()
#        triggerUrl      认证后跳转 URL    "http://1.1.1.1/"            trigger_url()
#        checkTargets    连通性检测目标    [[url, 期望正文, 名字], …]    check_targets()
#        loginExtra      门户表单模板      {键: 值, …}                   login_extra()
#
#    ⚠️ 取值**一律走上面那个函数**，不要在别处直接引用常量 ——
#       直接引用的话 config.json 改了它也不跟着变，于是「配置看着写对了、
#       实际压根没生效」，正是本项目最警惕的那类静默错误。
#       （`campus_subnets()` 从 2026-09-19 就是这么做的，另外三个 2026-10-05 补齐。）
#    回归靠 `school_profile_test.py`（含阳性对照：故意写错的配置必须真的改变结果）。
SCHOOL_KEYS = ("portalHost", "wlanAcIp", "wlanAcName", "timeoutSec",
               "campusSubnets", "triggerUrl", "checkTargets", "loginExtra")

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

# 认证后要跳转到的目标 URL。**取值走 trigger_url()**（可被 config.json 的
# `triggerUrl` 覆盖），不要直接引用这个名字 —— 直接引用就等于配置改不动。
TRIGGER_URL = "http://1.1.1.1/"
# 连通性检测目标：逐个试，任一通过即算「已联网」。**取值走 check_targets()**。
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

# 「用户清过凭据」的标记。
# ⚠️ **故意不放进 PRIVATE_FILES** —— purge 自己不能删它，否则下次启动
#    migrate_legacy_data() 又会把旧位置的账号继承回来，等于白清。
#    它本身不含任何账号信息，就是个带时间戳的小文件。
PURGED_FLAG = os.path.join(data_dir(), "purged.flag")


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


# ==================== 账号文件的互斥 ====================
#
# 🔴 为什么需要（2026-10-03，有复现脚本 _repro_accounts_race.py）：
#     `upsert_account` / `update_account_record` 都是「读 → 改 → 写」三步，
#     而写账号的线程**不止一个**：
#       · GUI 主线程：保存、删除账号
#       · GUI 后台任务线程：切账号 / 退出账号（start_job 起的 daemon 线程）
#       · 守护进程（独立进程，--auto --guard）—— 这条锁管不到，见下
#     两个写者交错时后写的覆盖先写的，账号就没了。
#     实测：6 个写者各写 30 个**互不相同**的 uid，期望 181 个，
#     实际只剩 31~61 个 —— 丢 66%~83%。
#
# 🔴 为什么锁的是「整段读-改-写」而不是 load/save 各一把：
#     只给 load_accounts / save_accounts 各包一把锁是**假的修复** ——
#     锁在两步之间就放掉了：
#         T1 读 → T2 读 → T1 写 → T2 写      ← T2 覆盖掉 T1 的改动
#     看着像修好了，其实一次都没拦住。所以必须提供 mutate_accounts()，
#     让调用方把三步放在同一个临界区里。
#
# ⚠️ 这个锁**只管本进程内的线程**。守护进程是另一个进程，锁文件才管得住它 ——
#    那是另一个问题，没在这个改动里处理。
_ACCOUNTS_LOCK = threading.RLock()

# 等锁的上限。超时后**照常执行**，不抛异常也不阻塞界面：
#   这个程序退出走 TerminateProcess，任何「无限等锁」都可能等不到；
#   宁可偶发一次覆盖，也不能让「加锁」变成新的卡死来源。
ACCOUNTS_LOCK_TIMEOUT = 2.0


def mutate_accounts(fn):
    """在锁里做一次完整的「读 → 改 → 写」。fn(store) 就地修改，返回值忽略。

    这是**唯一**推荐的账号写入口。直接用 load_accounts + save_accounts
    自己拼三步，就等于把上面那个坑又踩一遍。
    """
    got = _ACCOUNTS_LOCK.acquire(timeout=ACCOUNTS_LOCK_TIMEOUT)
    try:
        store = load_accounts()
        fn(store)
        save_accounts(store)
        return store
    finally:
        if got:
            _ACCOUNTS_LOCK.release()


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


# ---- 学校参数的取值函数（默认值在文件顶部「学校参数」一节，可被 config.json 覆盖）----
# 三个都收一个可选的已读好的 cfg，免得在一次登录里把同一个文件读三遍。
def _cfg_or_load(cfg):
    return cfg if isinstance(cfg, dict) else load_config()


def trigger_url(cfg=None):
    """认证后跳转的目标 URL。可被 config.json 的 `triggerUrl` 覆盖。"""
    v = _cfg_or_load(cfg).get("triggerUrl")
    return v.strip() if isinstance(v, str) and v.strip() else TRIGGER_URL


def check_targets(cfg=None):
    """连通性检测目标列表，元素是 (url, 期望正文, 名字)。可被 `checkTargets` 覆盖。

    config.json 里的写法（三种都认）：
        "checkTargets": ["http://a/generate_204"]                       # 只给 url
        "checkTargets": [["http://a/t.txt", "expected text"]]           # url + 正文
        "checkTargets": [["http://a/t.txt", "expected text", "short"]]  # 全给
    名字缺省时用域名，方便日志里一眼看出是谁挂了。
    """
    v = _cfg_or_load(cfg).get("checkTargets")
    if not isinstance(v, (list, tuple)) or not v:
        return list(CHECK_TARGETS)
    out = []
    for item in v:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), None, _short_name(item)))
            continue
        if isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
            url = item[0].strip()
            if not url:
                continue
            expect = item[1] if len(item) > 1 else None
            name = item[2] if len(item) > 2 and item[2] else _short_name(url)
            out.append((url, expect, str(name)))
    # 一条都没解析出来（写错了）→ 退回默认，别让程序变成「永远测不出网」
    return out or list(CHECK_TARGETS)


def _short_name(url):
    """从 URL 取一个短名字，用作日志里的目标名。"""
    host = urlparse(url).hostname or url
    return host.split(".")[-2] if host.count(".") >= 1 else host


def login_extra(cfg=None):
    """门户表单的公共字段模板。可被 config.json 的 `loginExtra` **浅合并**覆盖。

    浅合并 = 只覆盖你写出来的键，没写的仍用默认。
      · 为什么不做「整体替换」：换学校时多数字段（scheme / loginType / pageid…）
        其实一样，逼用户抄全 20 个键只会抄漏。
      · 为什么不做「深合并」：这里本来就是扁平一层，没有嵌套可深。
    ⚠️ 已知限制：**没法靠配置删掉一个默认字段**（值写 null 会被 load_config 丢掉）。
       真要删目前只能改源码 —— 暂不需要，先记在这儿。
    """
    v = _cfg_or_load(cfg).get("loginExtra")
    out = dict(LOGIN_EXTRA)
    if isinstance(v, dict):
        out.update(v)
    return out


def load_accounts():
    """读账号库。

    ⚠️ **只读**用可以直接调；只要打算改内容，就必须走 `mutate_accounts()` ——
       自己拼「load → 改 → save」等于绕过锁，见 mutate_accounts 的说明。
    """
    got = _ACCOUNTS_LOCK.acquire(timeout=ACCOUNTS_LOCK_TIMEOUT)
    try:
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
    finally:
        if got:
            _ACCOUNTS_LOCK.release()


def save_accounts(store):
    got = _ACCOUNTS_LOCK.acquire(timeout=ACCOUNTS_LOCK_TIMEOUT)
    try:
        write_json(ACCOUNTS_FILE, store)
    finally:
        if got:
            _ACCOUNTS_LOCK.release()


def upsert_account(uid, pwd, overwrite):
    """记录一个账号。

    overwrite=False 时**不覆盖**已存的密码 —— 切换账号时必须这样，
    否则回滚目标的密码就被新密码冲掉了，保险会失效。
    """
    def _do(store):
        if uid not in store["accounts"]:
            store["accounts"][uid] = {"passwd": pwd, "lastSuccess": "",
                                      "lastAttempt": "", "lastResult": ""}
        elif overwrite or not store["accounts"][uid].get("passwd"):
            store["accounts"][uid]["passwd"] = pwd

    mutate_accounts(_do)


def update_account_record(uid, pwd, ok):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _do(store):
        if uid not in store["accounts"]:
            store["accounts"][uid] = {"passwd": "", "lastSuccess": "",
                                      "lastAttempt": "", "lastResult": ""}
        a = store["accounts"][uid]
        a["lastAttempt"] = now
        a["lastResult"] = "成功" if ok else "失败"
        if ok:
            a["passwd"] = pwd
            a["lastSuccess"] = now
            store["lastOnline"] = uid

    mutate_accounts(_do)


def set_last_online(uid):
    def _do(store):
        store["lastOnline"] = uid
    mutate_accounts(_do)


def note_logout():
    """记下「用户是**主动**退出的」这件事。

    ⚠️ 为什么必须记时间（2026-09-19 加）：`--guard` 会在开机后 30 分钟里
    自动重连。如果用户在这段时间里主动点了「退出当前账号」，守护进程一看
    「在校园网但没认证」就把他又登回去 —— 用户会觉得这软件在跟他对着干。
    光看 lastOnline 是否为空判不出来（开机时它本来就可能是空的），
    所以单独记一个时刻，守护据此避让一段时间。
    """
    def _do(store):
        store["lastOnline"] = ""
        store["lastLogout"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mutate_accounts(_do)


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
    # 用户明确清过凭据 → 不再自动继承。
    # 没有这一条的话，界面上的「清除本机保存的账号密码」就是假的：清完重启，
    # 账号会从旧位置原样继承回来（2026-09-21 用 probes/_purge_reinherit.py
    # 实测复现过，连密码都一起回来了）。
    if os.path.isfile(PURGED_FLAG):
        log("本机凭据被用户清除过，跳过旧数据继承")
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
            # 这里要的是「整份替换成旧位置那一份」，所以 clear + update，
            # 而不是把旧账号并进当前账号库 —— 保持原语义，只是挪进锁里。
            def _do(store):
                store.clear()
                store.update(old_acc)
            mutate_accounts(_do)
        log("已从 %s 继承旧配置" % src)
        return True
    return False


def purge_local_data(legacy=True):
    """清除本机保存的账号密码等隐私数据。返回 (数据目录, 实际删掉的名字, 删不掉的)。

    只删 PRIVATE_FILES 里明确列出的文件 —— 不用通配符、不递归删目录，
    免得误伤。数据目录本身留着（里面已经空了，下次运行会照常重建）。

    legacy=True 时**连旧位置一起清**（%APPDATA%\\CampusLogin、C:\\tools）。
    为什么必须带上它们（2026-09-21 实测复现）：旧位置那份 config.json 是上一版
    exe 写的，migrate_legacy_data() 一直在拿它做继承；只清新位置的话，用户重启
    程序就会看到「账号又自己回来了」—— 因为 migrate 的判据正是「新位置没有
    config.json」，而清凭据恰好制造了这个条件。清不干净等于没清。
    """
    d = data_dir()
    removed, failed = [], []
    targets = [(d, n) for n in PRIVATE_FILES]
    if legacy:
        cur = os.path.abspath(d)
        for src in legacy_data_dirs():
            if not src or os.path.abspath(src) == cur:
                continue
            targets += [(src, n) for n in PRIVATE_FILES]
    for base, n in targets:
        p = os.path.join(base, n)
        if not os.path.isfile(p):
            continue
        # 旧位置的文件名和主目录重名，得标上目录才分得清是哪一个被删了 ——
        # 否则界面上只会看到两个「config.json」，用户没法判断清干净没有。
        label = (n if os.path.abspath(base) == os.path.abspath(d)
                 else os.path.join(base, n))
        try:
            os.remove(p)
            removed.append(label)
        except Exception as e:
            failed.append("%s(%s)" % (label, e))
    return d, removed, failed


def purge_local_data_with_marker():
    """清凭据的完整动作：先盖「用户主动退出」标记，再删文件。

    返回值和 purge_local_data() 一样 (数据目录, 删掉的, 删不掉的)。

    ⚠️ **顺序不能反**，所以把它抽成一个函数 —— 界面和命令行都只调这里，
    全项目就只有这一处需要保证顺序。
    （2026-09-21 记一笔：反过来的话，purge 刚删掉的 accounts.json 又会被
      note_logout 重建出来 —— 用户看到「已清除」，磁盘上却还留着一个文件。）

    标记在这里的作用是**盖住那最多 30 秒的窗口**：守护每 poll 秒才醒一次，
    醒来的第一件事虽然是查凭据还在不在（guard_should_exit_for_purge），
    但万一它这一拍先走到了重连判定，「刚主动退出过」就能拦住它拿着内存里
    那份密码再登一次 —— 用户刚点完清除，不该看到它又登回去。
    """
    note_logout()
    d, removed, failed = purge_local_data()
    # 最后留一个「用户清过凭据」的标记。删旧位置是尽力而为（权限、占用都可能
    # 让它失败），万一有文件没删掉，这个标记就是第二道闸 —— migrate_legacy_data()
    # 看到它就不会再把账号继承回来。写不进去也不影响本次清除的结果。
    try:
        with open(PURGED_FLAG, "w", encoding="utf-8") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        log("写清除标记失败（不影响本次清除）:\n%s" % traceback.format_exc())
    return d, removed, failed


def guard_should_exit_for_purge():
    """守护要不要因为「本机凭据已被用户清除」而退出。

    为什么需要它（2026-09-21 加）：界面上的「清除本机保存的账号密码」删的是
    **磁盘上的文件**，而守护是**另一个进程** —— 它在启动时就把账号密码读进了
    内存（run_guard 里的 uid / pwd 是循环外的局部变量，整个 30 分钟都不重读）。
    光删文件的话，那个进程内存里还留着一份密码，还要再跑最多半小时。
    用户点的是「清除」，程序里却还有一份，这不能算清干净。

    判据**只认「凭据文件不存在」这一个事实**：
      · 不用 load_config() 的结果 —— 它读失败、解析失败时同样返回空密码，
        磁盘抖动一下就会让守护误退，而后果是「掉线再也没人重连」。
      · 文件真被删掉，才是明确的「用户清了凭据」。

    ⚠️ 这是**长命进程里才跑得到**的判定：--selftest 走不到守护循环，
       所以它必须有独立的测试（见 updtest_purge.py），否则等于零覆盖。
    """
    return not os.path.isfile(CONFIG_FILE)


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
        for url, expect, _name in check_targets(cfg):
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
    NET_OFF_CAMPUS: "#94a3b8",
    NET_OK: "#16a34a",
    NET_NEED_LOGIN: "#dc2626",
    NET_UNKNOWN: "#94a3b8",
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
    # ⚠️ 网段列表**提到循环外**（2026-10-03）。campus_subnets() 会 load_config()，
    #    也就是每轮读一次 config.json —— 而上面那句「要**便宜**」是认真的：
    #    0.5 秒一轮 × 60 秒 = 120 次读盘，全是白读（配置不可能在这 60 秒里变，
    #    何况它还是本程序自己写的）。原来注释说「只看一次本机 IP」，
    #    实现却每轮读盘 —— 注释和实现对不上，是这次顺手修掉的原因。
    subnets = campus_subnets()
    while time.time() < deadline:
        ip = local_ipv4()
        if ip and any(ip.startswith(p) for p in subnets):
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
        "url": trigger_url(cfg),
    })


# ==================== portal ====================
# 门户表单的**公共字段**：同一套门户模板参数，登录 / 取认证页 / 下线都要带。
#
# ⚠️ 这里是**唯一来源**（2026-10-03 收敛）。以前 `_auth_page` 和 `portal_logout`
#    各自把同样这 20 个键**又抄了一遍**，三份内容一致纯属巧合 —— 哪天门户改了
#    `templatetype` 之类，改一处漏两处就会「能登录但取不到 distoken」，
#    而认证逻辑和日志全都正常，只有逐键对比表单才看得出来。
#    要加 / 改公共字段就只改这里；两个表单一律用 auth_form() / logout_form() 构造。
#    回归靠 `form_drift_test.py`（金标准 = 收敛前真正发出去的内容）。
#
# ⚠️ **取值走 login_extra()**（可被 config.json 的 `loginExtra` 覆盖），
#    不要直接引用这个名字 —— 这个字典是「默认模板」，不是「最终模板」。
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


def auth_form(uid, pwd):
    """取认证页（webauth.do）要 POST 的表单：公共字段 + 本次要认证的账号密码。

    ⚠️ 与 portal_login 的用法**不同**，别互相替代：
       portal_login 是拿门户重定向给的参数当底，本表用来**补缺**（setdefault，不覆盖门户值）；
       本函数是**从零构造**固定模板表单（那一步拿不到门户参数，也不需要）。
    模板本身走 login_extra()，所以 config.json 的 `loginExtra` 对这里同样生效。
    """
    return dict(login_extra(), userId=uid, passwd=pwd)


def logout_form(user, token):
    """下线（webdisconn.do）要 POST 的表单。

    与登录表单的差异（逐键核过，别凭印象改；金标准见 form_drift_test.py）：
        去掉 remInfo / desc_lb —— 这两个是**登录页的显示开关**，下线用不到
        多出 auth_type="0" / other1="disconn" —— 下线专用
        distoken 用真 token（登录表单那里是空串）、url / userId 补上
    模板本身走 login_extra()，所以 config.json 的 `loginExtra` 对这里同样生效。
    """
    form = dict(login_extra(), auth_type="0", distoken=token,
                url=trigger_url(), userId=user, other1="disconn")
    for k in ("remInfo", "desc_lb"):
        form.pop(k, None)
    return form


def portal_login(uid, pwd):
    cfg = load_config()
    host = cfg.get("portalHost") or DEFAULTS["portalHost"]
    turl = trigger_url(cfg)          # 认证后跳转目标（可被 config.json 覆盖）
    sess = Session({"User-Agent": UA})

    final_url = None
    try:
        r = sess.get(turl, timeout=10)   # urllib 默认就会跟随重定向
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
            "url": turl,
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

    for k, v in login_extra(cfg).items():
        p.setdefault(k, v)
    p["userId"] = uid
    p["passwd"] = pwd
    p.setdefault("url", turl)

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
    # 表单字段只在 LOGIN_EXTRA 定义一份，别在这儿再抄一遍
    # （这里以前就是第二份，靠人工和 LOGIN_EXTRA 保持一致）。
    form = auth_form(uid, pwd)
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
    # 下线表单 = 公共字段去掉两个登录页开关 + 下线专用字段。
    # 差异集中在 logout_form() 里，这里不再摆一份字面量（以前这里就是第三份）。
    form = logout_form(cfg.get("userId", ""), token)
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
    # 退出原因。最后那行日志要**如实**说 —— 写死「已跑满 N 秒」的话，因为
    # 凭据被清除而提前退出时日志会撒谎，排查的人会被这行带偏。
    why_exit = "已跑满 %d 秒" % seconds
    while True:
        left = deadline - time.time()
        if left <= 0:
            break
        time.sleep(min(poll, left))
        if time.time() >= deadline:
            break
        # 用户可能在守护运行期间清了本机凭据（设置页最下面那个入口）。凭据没了，
        # 这个进程既没有账号可用、也没理由继续把内存里那份密码留着 —— 自己退掉。
        # 放在循环体最前面（在 try 之外）：它不该被下面那个「异常也不能让循环挂掉」
        # 的兜底吞掉，而且要先于任何联网动作生效。
        if guard_should_exit_for_purge():
            why_exit = "本机凭据已被清除"
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
    log("=== 守护结束（%s）===" % why_exit)
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

  以后开机自动联网，不用再管。


【三个按钮】

  切换到此账号  换账号时用。没切成功会自动退回原来的账号，
                已经在用这个账号的话会直接告诉你。
  仅保存        只记住账号密码，不马上切换。
  退出当前账号  主动下线，下线后本机暂时上不了网。下线要连校园网的门户，
                偶尔会碰上网络抖动，程序会自动再试两次；没测准是否真退成功时
                会说「无法确认是否已退出」。


【密码框怎么用】

  密码默认显示成一串小星号。正在输入时，刚敲进去的那一个字符会露出来一下，
  前面的字符仍然是星号。

  点右边的「显示」看完整密码，点「隐藏」盖回去；鼠标点到别处或关掉窗口也会盖回。

  从别处复制密码粘进来，用 Ctrl+V。想改中间的字，先退格删掉再重打
  （输入位置固定在末尾）。


【顶部的网络状态】

  绿点  已连接校园网       在校园网里，并且已经认证，可以正常上网。
  红点  已连校园网，未认证  在校园网里，但被登录页挡住了，需要登录。
  灰点  未连接校园网       现在连的不是校园网（比如家里或手机的 Wi-Fi），
                          这种状态下点登录是没用的。
  灰点  网络状态未知       这次没检测出来，不代表没网，
                        点「切换到此账号」试一次。


【账号密码存在哪】

  存在这台电脑的  C:\ProgramData\CampusLogin  里，不在这个程序里。所以把
  「校园网自动登录.exe」这一个文件拷给别人，不会带出你的账号、密码和登录记录。

  想在本机彻底清掉，点界面最下面那个「清除本机保存的账号密码」
  （命令行也可以：--purge）。清掉之后要重新输入账号密码才能登录；
  如果开机自启还开着，建议把那个勾也取消掉 —— 否则开机时会跑一次但登不上。


【检查更新】

  点标题右边那行写着版本号的小字就是入口。有新版本会问你「是否现在下载并升级」，
  点「是」之后 下载 → 校验 → 替换 → 自动重新打开，不用你动手。
  升级只换程序本身，保存在 C:\ProgramData\CampusLogin 里的账号不动。

  升级过程中的临时文件也放在那个目录，不会写到你放 exe 的地方。
  旧版本备份会在下次启动时自动删掉；万一那会儿删不掉（比如程序还开着），
  它过一会儿会自己再试几次。

  下载下来的文件会跟发布方公布的哈希值对一遍，对不上就直接丢掉。
  更新包只从 hobson2233.dpdns.org（本程序的发布站）下载。

  如果显示「检查更新失败」，多半是当时网络不通，稍后再点一次就行。
  失败时会明确说「失败」，不会含糊地显示「已是最新版本」。


【注意】

  · 只适用于本校校园网。部分杀毒软件可能误报，加白名单即可。
  · 联网之后它还会继续守 30 分钟，掉线了自动重连，到点自己退出。
    这段时间里你要是主动点了「退出当前账号」，它不会把你登回去。
  · 主动退出账号后，校园网通常几十秒内会自动重新连上（学校那边的设置）。
  · 这个 exe 放在哪个文件夹都能正常用，文件夹名带中文、带空格也没关系。
    但挪到别的位置后「开机自启」会失效（自启项记的是原来的位置），
    界面会在「开机自启」下面提示你，把勾取消再重新勾一次就好。
    升级到新版本后如果提示「旧设置」，同样处理：取消再重新勾一次。
  · 「开机自启」是靠系统「任务计划程序」里一个叫 CampusLogin 的任务实现的
    （开机登录后自动跑一次），所以任务管理器里的「启动应用」列表看不到它，
    要去任务计划程序里看。在界面里把勾取消，这个任务会被删掉。
  · 同时开了两个设置窗口时，一个在操作，另一个会提示「另一个操作正在执行」。
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

  平时用不到，双击 exe 走界面就行。下面这些是给排障和脚本用的。
  打包版没有终端，--help 和 --version 会弹一个窗口显示。

  --help / -h / /?    显示这份说明，然后退出
  --version / -v      显示版本号，然后退出
  --purge             清掉本机保存的账号密码；退出码 0=清干净了，1=有文件删不掉
                      （界面最下面的「清除本机保存的账号密码」是同一个操作）
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

        # 配色跟着 run_gui 那套走（BG/CARD/LINE/BLUE_* 都是模块级常量）。
        # 这里以前散着一堆字面量（#c9ced6 / #0b5ed7 / #eef4fb），
        # 换主题时必然漏掉一处，所以一并收敛。
        #
        # 2026-10-04 换成 Canvas 圆角底（`round_panel`），高度**和旧版一致**：
        #   旧：1px 描边 + `ipady=6`          → 内容上下各 7
        #   新：panel `pad_y=5` + `ipady=2`   → 内容上下各 7
        # 水平：左边旧 `1+10=11`、新 `8+3=11`；右边旧 `1+8=9`、新 `1+8=9`。
        # `self.box` 仍然是那个「可以 pack 的控件」（现在是 Canvas）——
        # pwd_gui_test 和 run_gui 都是直接 pack 它，没有别的用法。
        self.box, _body = round_panel(parent, tk, 8, 5, 5)
        self.entry = tk.Entry(_body, font=font_main, relief="flat", bd=0,
                              bg=CARD, fg=fg, highlightthickness=0,
                              insertbackground=fg)
        self.entry.pack(side="left", fill="x", expand=True, ipady=2, padx=(3, 0))
        self.button = tk.Button(_body, text="显示", font=font_small,
                                relief="flat", bd=0, bg=CARD, fg=BLUE_DARK,
                                activebackground=BLUE_SOFT, activeforeground=BLUE_DARK,
                                cursor="hand2", padx=10, command=self.toggle_reveal)
        self.button.pack(side="right", padx=(4, 1), ipady=2)

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

# 「界面自检」失败的固定标识，和上面那个各管一摊。**build.py 也从这里导入。**
#
# ⚠️ 为什么必须单独有一个串（2026-10-04 实测踩到）：
#     build.py 里 `--guitest` 是**软门槛**（`must=False`）—— 理由是受控会话里
#     建 Tk 会飘（有时 0.9s、有时 14s、有时不返回），不能拿它当构建成败的判据。
#     所以「界面没建起来」只打印一行参考；**只有**命中 WIRING_FAIL_PHRASE 才硬拦。
#     于是界面自检（比如「按钮和背景同色」）抛出的 GUI_FAIL 会被当成
#     「环境问题」轻轻放过 —— 门槛看着接上了，其实根本没拦。
#     分开的理由和上面那句一样：确定性代码错误要硬拦，环境性抖动只能参考。
#     这个串就是给前者的。
UI_FAIL_PHRASE = "界面自检失败"

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

OUTER_PAD = 14      # 最外层留白
INNER_PAD = 14      # 卡片内留白
# 0.3.6 起改成 0。它原本记的是「给标题栏/边框留的余量」，但 Tk 的
# `geometry("WxH")` 里的 H **就是客户区高度、不含标题栏**（实测：
# 传 851 → winfo_height()=851、holder 实际也拿到 851-28=823）。
# 也就是说这 40px 从来不是被标题栏吃掉的，而是一路变成卡片底部的空白 ——
# 实测 holder 需要 783、实际给到 783，客户区 851 里有 68px（28 外层留白
# + 40 这个 EXTRA_H）全是富余。窗口不吃紧时无所谓，在 16:10 上就是白高 40px。
# 留着这个常量是为了不改调用点的签名，值归零即「不再额外加高」。
EXTRA_H = 0
SCREEN_MARGIN_H = 80    # 给任务栏 + 标题栏留的余量
# 窗口高度最多占屏幕的比例。0.85 —— 留出上方标题栏和下方任务栏，也让窗口
# 四周有点桌面透出来，不至于「贴满上下沿」。**这个比例就是「16:10 屏上显得
# 太长」的正解**：16:10 的高本来就只有宽的 62.5%，如果高度再按内容硬撑，
# 就会一路顶到屏幕边。按下限和上限同时收口，窗口才是「跟着屏幕走」的。
SCREEN_H_RATIO = 0.85
MIN_H = 560         # 再矮也不小于这个（低于它就一定挂滚动条了）


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

    **高度上限还要按屏幕比例收。** 0.3.5 的写法上限是个死数 1000：在 16:10 的
    笔记本屏上（2560x1600 @150% → Tk 只看到 1707x1067），内容需要 887px，
    算出来 963px 窗口，占掉屏高的 90% —— 上下几乎没有余量，看着又高又挤。
    改成 `sh * SCREEN_H_RATIO` 之后，窗口高度跟着屏幕走：大屏不再顶满，
    小屏仍然被 `sh - SCREEN_MARGIN_H` 兜住。**上限取两者更严的那个**，
    所以「绝不超出屏幕」这条原有保证一点没松。

    **高度不再额外加 EXTRA_H。** 见上面常量的说明：`geometry` 的高就是客户区，
    再 +40 只会变成卡片底部的空白。现在 `h` 直接等于内容需要的高度
    （或上限，谁小听谁的）—— 实测客户区 851 里那 68px 富余全部消失。
    """
    max_w = max(360, sw - 40)
    max_h = max(320, sh - SCREEN_MARGIN_H)
    # 比例上限和绝对上限取更严的：大屏上比例生效，小屏上 sh-80 生效。
    ratio_h = int(sh * SCREEN_H_RATIO) if sh > 0 else max_h

    w = max(820, min(1180, int(sw * 0.44)))
    h = max(MIN_H, min(ratio_h, max_h, reqh + EXTRA_H))
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


# 更新残留清理的重试节奏（秒）。为什么需要重试 —— 2026-09-20 实测（_repro_lock.py）：
#   替换完成后，旧 exe 改名成的 `.old` 往往**还被人占着**。最典型的是守护进程：
#   更新发生时它正跑着那个 exe，改名后它继续从 `.old` 跑完剩下的时间（最长 30 分钟），
#   这期间 Windows 拒绝删除该文件。而启动清理原本**只跑一次、失败就算了** ——
#   于是 `.old` 会一直留在原地，用户看到的就是「更新完 .old 还在」。
#   所以：清不掉就按下面的节奏重试，窗口必须盖过守护的 30 分钟。
STALE_SWEEP_DELAYS = (20, 60, 180, 300, 600, 900, 1200, 1800)


def sweep_stale_once():
    """清一次更新残留。返回 `(删掉的文件名列表, 没删掉的文件名列表)`。

    清不掉**不是错误**（有实例正占着），所以要把「还剩什么」报出来给重试逻辑用。
    传 data_dir() 是必须的：0.3.3 起临时文件落在数据目录，漏传就会退回 exe 同目录
    （用户的桌面），新位置的 `.old` 永远没人清、一直累积。
    """
    exe = sys.executable if is_frozen() else None
    if not exe:
        return [], []
    removed, left = updater.cleanup_stale_all(exe, data_dir())
    return ([os.path.basename(p) for p in removed],
            [os.path.basename(p) for p in left])


def sweep_stale_retry():
    """后台重试清残留，直到清干净或用完重试窗口。只在还有残留时才起。"""
    left = []
    for delay in STALE_SWEEP_DELAYS:
        time.sleep(delay)
        removed, left = sweep_stale_once()
        if removed:
            log("重试清理更新残留成功：%s" % "、".join(removed))
        if not left:
            return
    log("更新残留始终删不掉（有实例长期占用？）：%s" % "、".join(left))


def stale_leftover_desc():
    """`--selftest` 用：现在还剩哪些更新残留。没有就报「无」。

    为什么值得占一行：用户报「更新完还有 .old」时，第一件事就是看这一行 ——
    不用再去翻目录、也不用猜。清不掉的原因（被守护占着）也在这里显形。
    """
    try:
        _removed, left = sweep_stale_once()
    except Exception:
        return "查询失败（见日志）"
    return "、".join(left) if left else "无"


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


# 界面配色**不在本文件里** —— 见 `repo/palette.py`（2026-10-05 抽出）。
# 本文件在顶部 `from palette import (...)`。
#
# 为什么要单独一个模块：这些颜色**站点 CSS 也要用**，而两边原本是手工同步的
# 两份副本（实测重合 15 个）。收敛之后 `make_site.py` 也从 palette 生成
# CSS 变量，改一次配色软件和网站一起变。
#
# ⚠️ 想加新颜色就往 palette.py 里加，**别在本文件里另起一份** ——
#    那正是这次要消掉的问题。

# 状态徽章的浅底，和 NET_COLOR 一一对应。
# ⚠️ 放在这里（UI 配色区）而不是紧挨着 NET_COLOR（文件中部）：
#    它要引用 OK_SOFT / DANGER_SOFT，而那两个常量在下面才定义。
#    写死在 NET_COLOR 旁边就得把 `#e7f7ec` 再抄一遍 —— 抄一遍就是一处漂移源。
#    NET_COLOR 本身不依赖任何 UI 常量（命令行也要用），所以它留在原处。
NET_SOFT = {
    NET_OFF_CAMPUS: "#eef2f7",
    NET_OK: OK_SOFT,
    NET_NEED_LOGIN: DANGER_SOFT,
    NET_UNKNOWN: "#eef2f7",
}


def _hex_rgb(s):
    """`"#rrggbb"` -> (r, g, b)；不是这个格式就返回 None。"""
    s = (s or "").strip()
    if len(s) != 7 or not s.startswith("#"):
        return None
    try:
        return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None


def _walk_widgets(w):
    yield w
    for c in w.winfo_children():
        for x in _walk_widgets(c):
            yield x


# 两个颜色至少差这么多（单通道最大差，0~255）才算「看得出是两种颜色」。
# 参照现有取值定：账号行「选用」按钮 #f1f5f9 落在白卡 #ffffff 上，差 14 ——
# 那是肉眼一眼就能分辨的程度。取 8 留一点余量，但足够拦住「几乎同色」。
_CONTRAST_MIN = 8

# 默认「禁用灰」在浅底上撞了背景时改用的颜色。
# 比 BG(#eef1f6) 深 15 级、比 CARD(#ffffff) 深 32 级，两个底上都看得出来。
_DISABLED_FALLBACK = "#dfe5ee"


def _color_delta(a, b):
    """两个 `"#rrggbb"` 的单通道最大差（0~255）。任一个解析不了就返回 None。"""
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    if ra is None or rb is None:
        return None
    return max(abs(ra[i] - rb[i]) for i in range(3))


def _pick_disabled_bg(disabled_bg, parent_bg):
    """给「禁用态」挑个底色：跟背景分不开就换一档看得见的灰。

    ⚠️ 为什么单独抽成函数（2026-10-04）：
        这段判断必须能**脱离 Tk** 单独测 —— 而它原本长在 round_button 里，
        round_button 要建 Canvas，受控会话里建 Tk 时好时坏（同一份代码实测
        0.9s / 14s / 直接挂住，见 wiring_gate_test.py 顶部）。判定逻辑是这条
        门槛的核心，不能跟 Tk 的脾气绑在一起。
    """
    d = _color_delta(disabled_bg, parent_bg)
    if d is not None and d < _CONTRAST_MIN:
        return _DISABLED_FALLBACK
    return disabled_bg


def ui_contrast_check(root):
    """在**真实控件树**里核对：每个圆角按钮的填充色都和它坐的背景分得开。

    为什么需要（2026-10-04 实测踩到）：
        「使用说明」按钮的静止底色一开始写成了 `#eef1f6` —— 那正是页面底色 BG。
        结果按钮和背景完全融成一片，截图里就剩四个灰字，跟普通说明文字没区别，
        用户根本不会去点。这类错**不抛任何异常**：逻辑自检全绿、窗口照常显示、
        `--guitest` 也是 GUI_OK，只有把截图放大若干倍才看得出来。
        静态读源码同样靠不住 —— `bg="#eef1f6"` 和几百行外的 `BG = "#eef1f6"`
        隔着半屏，肉眼对不上。

        所以这里**不看源码，直接问控件**：「你的填充色和你的背景色一样吗？」
        要查的三种填充色都挂在按钮上（见 round_button 里的 `_rb_*`）：
        静止态、禁用态 —— 这两个都必须和背景分得开。

    为什么用「色差阈值」而不是「是否相等」：
        相等只是最极端的情况。差 2~3 级的两种灰，肉眼同样分不出来，
        但 `==` 判不出来。阈值取 _CONTRAST_MIN，并且**说明这个数从哪来**，
        免得以后有人以为它是随手挑的。

    找不到任何按钮时**抛异常而不是返回 OK**：
        否则一旦按钮不再挂 `_rb_bg`，这条自检就变成永远通过的空转 ——
        「永远通过」和「通过」在输出上一模一样，正是本项目最忌讳的假绿灯。

    返回一句人话结论；发现可疑就抛 AssertionError —— 消息**以 UI_FAIL_PHRASE 打头**，
    这样 --guitest 不但会落成 GUI_FAIL，build.py 还能靠那个串认出「这是确定性的
    界面缺陷」并硬拦构建。不带那个串的话，只会被当成 Tk 环境抖动轻轻放过。
    """
    checked = 0
    bad = []
    for w in _walk_widgets(root):
        fill = getattr(w, "_rb_bg", None)
        if fill is None:
            continue
        checked += 1
        text = getattr(w, "_rb_text", "?")
        back = getattr(w, "_rb_parent_bg", None)
        for label, color in (("静止态", fill),
                             ("禁用态", getattr(w, "_rb_disabled_bg", fill))):
            delta = _color_delta(color, back)
            if delta is None:
                continue
            if delta < _CONTRAST_MIN:
                bad.append("「%s」的%s底色 %s 和它坐的背景 %s 几乎同色"
                           "（单通道最大差 %d，要求 >= %d）"
                           % (text, label, color, back, delta, _CONTRAST_MIN))
    if not checked:
        raise AssertionError(
            UI_FAIL_PHRASE + "：控件树里一个圆角按钮都没找到 —— 要么界面没建起来，"
            "要么按钮不再挂 _rb_bg 了。这条自检正在空转，不能算通过。")
    if bad:
        raise AssertionError(UI_FAIL_PHRASE + "：有按钮看不出是个按钮"
                             "（会和背景融成一片）：\n  " + "\n  ".join(bad))
    return "圆角按钮配色自检：%d 个按钮的静止态/禁用态都与背景可区分（阈值 %d）" % (
        checked, _CONTRAST_MIN)


def _draw_round_rect(cv, x1, y1, x2, y2, r, fill, tags=None):
    """在 Canvas 上画一个圆角矩形：两个矩形 + 四个扇形。

    ⚠️ 为什么不用 `create_polygon(..., smooth=True)`（2026-10-04 实测踩到）：
        Tk 的 smooth 是**过线段中点的二次 B 样条**，顶点只当控制点用。
        后果有两个，都很要命：
          ① 画出来的形状整体**缩进**控制多边形内部 —— 按钮底边会离开
             Canvas 边缘，下面露出一条背景色；
          ② 实际圆角半径只有给定值的一半左右 —— r=9 在 36px 高的按钮上
             放大 4 倍才勉强看出一丝倒角，等于没做。
        这个写法是**精确**的：角就是四分之一的圆，边就是边。

    tags：给画出来的 6 个图元打同一个标签，调用方才能整批 `delete` / `tag_lower`。
          卡片是**会重画**的（窗口一改宽高就得重画一次），没有标签就只能
          `delete("all")` —— 那会把 create_window 放进去的内容控件一起干掉。
    """
    if r <= 0 or x2 - x1 <= 2 * r or y2 - y1 <= 2 * r:
        cv.create_rectangle(x1, y1, x2, y2, fill=fill, outline="", tags=tags)
        return
    cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="", tags=tags)
    cv.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="", tags=tags)
    d = 2 * r
    # Tk 的角度：0° 在三点钟方向，逆时针为正。
    for ax, ay, start in ((x1, y1, 90), (x2 - d, y1, 0),
                          (x1, y2 - d, 180), (x2 - d, y2 - d, 270)):
        cv.create_arc(ax, ay, ax + d, ay + d, start=start, extent=90,
                      style="pieslice", fill=fill, outline="", tags=tags)


def pill_radius(height):
    """胶囊形半径：给定高度，返回「看着是圆的、又不会退化成直角」的半径。

    🔴 为什么是 `height//2 - 1` 而不是 `height//2`：
        `_draw_round_rect` 在 `2r >= 短边` 时会**直接退回画一个直角矩形**
        （那是给 r<=0 准备的兜底）。取 `height//2` 正好踩在边界上 ——
        圆角会**静默消失**，而且没有任何报错，只能靠肉眼在图里发现。
        减 1 就稳了。
    """
    return max(4, int(height) // 2 - 1)


def round_chip(parent, tk, text, font, fg, soft, page=CARD, pad_x=9,
               height=18, radius=None, command=None, cursor="hand2",
               hover=None):
    """圆角小标签（徽章）。可以当纯标签（不传 command），也可以当小按钮用。

    ⚠️ 为什么不用 `tk.Label` 加个底色：**Label 只有直角**。
        「当前使用」「上次登录」这两个标签就坐在账号行里、紧挨着账号数字，
        方角在圆角卡片中间特别扎眼 —— 那正是用户说的「太方正」。
        同样的问题也出在标题行的「检查更新」和隐私区的「清除本机保存的账号密码」上。
    ⚠️ 文字挂在 `_rb_text` 上（和 round_button 用同一个属性名）：
        Canvas 的 `create_text` 内容**查不出来**，探针和截图脚本就靠这个属性
        才认得出「这里有一块写了字的控件」。不挂的话，`layout_probe` 那张
        「最靠下的控件」表里它就是个无名氏 —— 而最靠下的往往正是最该被认出来的。
    ⚠️ 高度**故意做得比行高低**（18 vs 约 24）：徽章是靠 `pack(side="left")`
        **垂直居中**在行里的，所以它多高都不影响行高，也就不会动 CONTENT_H。
        哪天想把它改得比行高还高，就得重新量内容高度了。

    🔴 兼容性契约（**改这里之前先看 run_gui 怎么用它**）：
        它在 run_gui 里替代的是两个 `tk.Label` 做的小链接，必须支持：
          · `configure(text=... / fg=... / bg=... / font=... / cursor=...)`
            —— `set_busy()` 忙碌时会把下载进度写进版本号那一行
          · `cget("text")` / `cget("cursor")`
            —— `update_wiring_check()` 要读它确认「版本号看得见、能点」
          · `bind("<Button-1>")` / `event_generate("<Button-1>")`
            —— 接线自检会**真发一个点击事件**，确认它真的会跑到检查更新
        ⚠️ 少支持哪一个，表现都是**静默的**：`set_busy` 那段整个包在
           `try/except: pass` 里，改了不生效也一声不吭。
           所以这一组接口不是「锦上添花」，是必须。
    """
    import tkinter.font as tkfont

    st = {"text": text, "font": font, "fg": fg, "bg": soft, "hover": hover,
          "hovering": False, "state": "normal"}
    r = pill_radius(height) if radius is None else radius

    def measure():
        try:
            return tkfont.Font(font=st["font"]).measure(st["text"]) + 2 * pad_x
        except Exception:
            return len(st["text"]) * 12 + 2 * pad_x

    cv = tk.Canvas(parent, width=measure(), height=height, bg=page,
                   highlightthickness=0, bd=0,
                   cursor=cursor if command else "arrow")

    def redraw(_e=None):
        try:
            w = measure()
            if int(cv.cget("width")) != w:
                cv.configure(width=w)          # 文案变长/变短要跟着变宽
        except Exception:
            return                             # 控件正在被销毁，画不了就算了
        bg = st["bg"]
        if st["hovering"] and st["hover"]:
            bg = st["hover"]
        cv.delete("all")
        _draw_round_rect(cv, 0, 0, w, height, r, bg)
        cv.create_text(w / 2.0, height / 2.0, text=st["text"], font=st["font"],
                       fill=st["fg"])
        cv._rb_text = st["text"]

    if command:
        def _click(_e=None):
            if st["state"] == "disabled":
                return
            command()
        cv.bind("<Button-1>", _click)
        cv.bind("<Enter>", lambda e: (st.update(hovering=True), redraw()))
        cv.bind("<Leave>", lambda e: (st.update(hovering=False), redraw()))

    def configure(**kw):
        state = kw.pop("state", None)
        if state is not None:
            st["state"] = state
            cv.configure(cursor="arrow" if state == "disabled" else cursor)
        for k in ("text", "font", "fg", "bg", "hover"):
            if k in kw:
                st[k] = kw.pop(k)
        if kw:
            tk.Canvas.configure(cv, **kw)
        redraw()

    def cget(key):
        if key in ("text", "font", "fg", "bg"):
            return st[key]
        if key == "state":
            return st["state"]
        return tk.Canvas.cget(cv, key)

    cv.configure = configure
    cv.cget = cget
    # 挂出填充色，让 ui_contrast_check 也覆盖到这些小链接 ——
    # 「点得到、但看不出是个能点的东西」和「按钮和背景同色」是同一类静默缺陷。
    cv._rb_bg = soft
    cv._rb_parent_bg = page
    redraw()
    return cv


def round_check(parent, tk, text, var, command, font, page=CARD, fg=FG,
                box=18, radius=5, gap=9, pad_y=3, disabled_fg="#a3adbb"):
    """圆角复选框：Canvas 画的圆角方框 + 对勾，右边跟一句说明文字。返回外层 Frame。

    ⚠️ 为什么不用 `tk.Checkbutton`：
        Windows 上它画的是**系统主题的方角指示器** —— 在一屏圆角里就是个异类，
        而 Tk 没有任何开关能改它的形状（`indicatoron=0` 会把整个控件变成
        一个方块按钮，更难看）。
    ⚠️ 为什么返回的是 Frame 而不是那个 Canvas：
        点文字也该能切换（复选框的通用约定），所以方框和文字得在同一个容器里、
        绑同一份点击处理。外面再包一层的另一个好处是 `pack()` 的行为和
        原来的 Checkbutton 一样（横向、居左）。
    ⚠️ `pad_y=3` 不是随手挑的：`tk.Checkbutton` 自带指示器内边距，一行是 30px；
        这里文字 24px，加 3+3 才凑回 30 —— 不改这个数，「开机自启」那张卡就会
        矮 6px，CONTENT_H 跟着变。

    🔴 兼容性契约（**改这里之前先看 run_gui 怎么用它**）：
        · `configure(state="normal"/"disabled")` —— `load_all()` 会用它
        · `cget("text")` —— 探针要能读到它的文字
      这两条失效都是**静默的**：禁用不生效 = 用户以为关了自启、其实还开着。
    """
    st = {"state": "normal", "on": bool(var.get())}

    wrap = tk.Frame(parent, bg=page)
    cv = tk.Canvas(wrap, width=box, height=box, bg=page,
                   highlightthickness=0, bd=0, cursor="hand2")
    cv.pack(side="left", pady=pad_y)
    lab = tk.Label(wrap, text=text, font=font, bg=page, fg=fg, cursor="hand2")
    lab.pack(side="left", padx=(gap, 0), pady=pad_y)

    def redraw():
        disabled = st["state"] == "disabled"
        on = st["on"]
        if disabled:
            edge = _DISABLED_FALLBACK
            inner = _DISABLED_FALLBACK if on else page
        else:
            edge = BLUE if on else "#c7d0dc"
            inner = BLUE if on else page
        cv.delete("all")
        # 1.5px 圆角描边 = 外圈描边色 + 内圈缩进 1.5 的填充色。
        # 选中时内外同色 → 一整块实心圆角方块，正是想要的样子。
        _draw_round_rect(cv, 0, 0, box, box, radius, edge)
        _draw_round_rect(cv, 1.5, 1.5, box - 1.5, box - 1.5,
                         max(radius - 1.5, 2), inner)
        if on:
            # 对勾。坐标按 box 归一化，改 box 大小不用重算。
            cv.create_line(box * 0.26, box * 0.53, box * 0.43, box * 0.70,
                           box * 0.75, box * 0.31, fill="#ffffff", width=2.1,
                           capstyle="round", joinstyle="round")

    def sync(*_a):
        """外部改了 `var`（load_all 同步真实状态、勾选失败后回滚）也要跟着重画。"""
        st["on"] = bool(var.get())
        redraw()

    def toggle(_e=None):
        if st["state"] == "disabled":
            return
        st["on"] = not st["on"]
        var.set(st["on"])            # 走 trace → sync → redraw
        redraw()
        command()

    for w in (cv, lab):
        w.bind("<Button-1>", toggle)
    var.trace_add("write", sync)

    def configure(**kw):
        state = kw.pop("state", None)
        if state is not None:
            st["state"] = state
            off = state == "disabled"
            for w in (cv, lab):
                w.configure(cursor="arrow" if off else "hand2")
            lab.configure(fg=disabled_fg if off else fg)
        if kw:
            tk.Frame.configure(wrap, **kw)
        redraw()

    def cget(key):
        if key == "text":
            return text
        if key == "state":
            return st["state"]
        return tk.Frame.cget(wrap, key)

    # 覆盖实例上的方法（和 round_button 同样的手法：模块顶层不 import tkinter，
    # 所以没法写 `class _RoundCheck(tk.Frame)`）。**故意不做 `config` 别名** ——
    # 见 round_button 里那段说明：漏掉的那次宁可响亮地报错。
    wrap.configure = configure
    wrap.cget = cget
    wrap._ck_text = text
    redraw()
    return wrap


# 圆角面板：Canvas 画底 + 内容 Frame。**card()、状态条、输入框都用它** ——
# 一份实现，免得几处各画一遍圆角、半径还不一样。
ROUND_PANEL_TAG = "panelbg"


def round_panel(parent, tk, pad_x, pad_y, radius, fill=CARD, line=LINE, page=BG,
                stretch=False):
    """造一个圆角面板，返回 `(canvas, body)`。

    用法：`cv, body = round_panel(parent, tk, 19, 15, 14)`，把内容塞进 body，
    再 `cv.pack(fill="x", ...)`。

    ⚠️ 约束（**必须成立，否则圆角会被内容糊成方的**）：
        `pad_x >= radius` 且 `pad_y >= radius`。
        body 是一个**矩形** Frame，底色 = fill。它一旦伸进角上那块圆弧区域，
        就会用一块白方块把圆角盖掉 —— 而且**不抛任何异常**，只是看着
        「圆角没生效」。所以这里直接 assert，宁可当场炸也不要静默退回直角。

    ⚠️ 高度得自己跟着内容走：
        Frame 会自动被子控件撑开，**Canvas 不会** —— 它的 `height` 是个固定值。
        不补这一步，卡片会永远停在 Canvas 的默认高度（7cm ≈ 265px），
        内容直接被切掉。做法是 body 每次 `<Configure>`（= 内容变了）就把
        Canvas 高度设成 `body 需要的高度 + 2*pad_y`。
        实测这个高度和旧实现（`Frame + highlightthickness=1` +
        `body.pack(padx=18, pady=14)`）**逐像素相等** —— 见 card() 里的算式。

    ⚠️ `stretch=True` 是**另一种模式**，给「跟着窗口长大」的容器用
        （说明窗口的正文区）：这时高度交给几何管理器（`fill/expand`），
        `fit()` 直接不干活，同时 body 也铺满整块卡片。
        两种模式不能混：既 `expand` 又自己设 `height`，谁最后写谁生效，
        表现出来就是「窗口拉大了、卡片没跟着长」。

    ⚠️ 不要在 Canvas 上 `delete("all")`：
        `create_window` 放进去的 body 是**画布上的一个图元**，`delete("all")`
        会把它一起删掉（内容控件连带消失）。重画只删 `ROUND_PANEL_TAG`。
    """
    if pad_x < radius or pad_y < radius:
        raise ValueError(
            "round_panel: pad_x/pad_y 必须 >= radius，否则内容矩形会盖住圆角"
            "（pad_x=%s pad_y=%s radius=%s）" % (pad_x, pad_y, radius))

    cv = tk.Canvas(parent, bg=page, highlightthickness=0, bd=0)
    body = tk.Frame(cv, bg=fill)
    win = cv.create_window(pad_x, pad_y, window=body, anchor="nw")
    memo = {"h": -1}
    # 描边色放在一个**可变容器**里，而不是闭包里的普通变量 ——
    # 输入框聚焦时要把描边从灰改成蓝（见 panel_set_line），
    # 而 redraw 是闭包，只有能改到它读的那个对象才生效。
    edge = {"line": line}

    def fit(_e=None):
        if stretch:
            # 拉伸模式：高度由几何管理器给（`fill="both", expand=True`），
            # **不能自己设 height** —— 那会和 expand 抢，谁最后写谁生效，
            # 表现出来就是「窗口拉大了，卡片没跟着长」或者反过来闪一下。
            return
        try:
            want = body.winfo_reqheight() + 2 * pad_y
        except Exception:
            return                              # 控件正在被销毁，问不到就收手
        # 记住上次设过的值：不然 `configure(height=...)` → `<Configure>` →
        # 再 `configure` 会自激成死循环。
        if want != memo["h"]:
            memo["h"] = want
            cv.configure(height=want)

    def redraw(_e=None):
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
        except Exception:
            return
        if w <= 2 * radius or h <= 2 * radius:
            # 还没布局完（初始是 1x1）。这时候画会落成一小块方角色片，
            # 比不画还难看 —— 等下一次 <Configure>。
            return
        cv.delete(ROUND_PANEL_TAG)
        # 1px 圆角描边 = 外圈描边色 + 内圈缩进 1px 的面板底。
        # 不用 create_rectangle(outline=...) 是因为那个 outline 是**方角**的，
        # 一圈方框套在圆角上，四个角立刻露馅。
        _draw_round_rect(cv, 0, 0, w, h, radius, edge["line"],
                         tags=ROUND_PANEL_TAG)
        _draw_round_rect(cv, 1, 1, w - 1, h - 1, max(radius - 1, 2), fill,
                         tags=ROUND_PANEL_TAG)
        cv.tag_lower(ROUND_PANEL_TAG)

    def on_cv(_e=None):
        fit()
        redraw()
        # 不包 try：`win` 是 create_window 那个图元，**我们从不 delete 它**
        # （重画只删 ROUND_PANEL_TAG，见文件头说明），所以这里不该失败。
        # 真失败了就该响 —— 吞掉它 = 内容宽度永远不跟着窗口走，而且毫无提示。
        w = max(cv.winfo_width() - 2 * pad_x, 1)
        if stretch:
            # 拉伸模式下内容也要跟着铺满，否则文字区只占卡片顶部一小块，
            # 下面留一大片白 —— 看着像「没加载完」。
            cv.itemconfigure(win, width=w,
                             height=max(cv.winfo_height() - 2 * pad_y, 1))
        else:
            cv.itemconfigure(win, width=w)

    body.bind("<Configure>", fit)
    cv.bind("<Configure>", on_cv)
    # 先摆一次：不然首帧高度是 Canvas 的默认 7cm，会闪一条空带。
    # 内容加进来之后 body 会再触发一次 <Configure>，那时才是最终高度。
    fit()
    # 挂出来供探针/测试核对「这是个圆角面板，不是普通 Frame」。
    # 不加这一手的话，测试只能去猜控件树形状，猜错了也不报错。
    cv._rp_body = body
    cv._rp_radius = radius
    cv._rp_pad = (pad_x, pad_y)
    cv._rp_edge = edge
    cv._rp_redraw = redraw
    return cv, body


def panel_set_line(cv, color):
    """改圆角面板的描边色并立刻重画（输入框聚焦变蓝用）。

    ⚠️ 必须真的重画一次：`_draw_round_rect` 是把颜色**烤进图元**的，
        改个变量不会让已经画上去的线变色 —— 只改不重画 = 看着毫无反应，
        而且没有任何报错。
    """
    cv._rp_edge["line"] = color
    cv._rp_redraw()


# ==================== 线性小图标 ====================
# 形状都写在**归一化的 24x24 方框**里，绘制时按 size/24 缩放。
#
# ⚠️ 为什么自绘，不用 emoji / Unicode 符号（⚙ 🔒 👤 这类）：
#     Tk 在 Windows 上会把它们交给**字体**去渲染 —— 颜色不受我们控制
#     （深色底上可能是黑的、跟主题不搭），基线也不受控（大小跟着字号跑，
#     同一个图标在不同分区可能大小不一）。自绘的笔画颜色是我们指定的，
#     才能跟着配色走 —— 而「图标要能上色」正是这次改版的目的。
# ⚠️ 为什么不用图片（png/ico）：
#     打包版里多一个资源文件，就多一份「有没有被 PyInstaller 打进去」的风险
#     （本项目已经因为 res_path 踩过）。几条线就够的东西，零资源零依赖更划算。
#
# 造型约定（和站点上的内联 SVG 是同一套，看着才像一家人）：
#     只描边不填充、线宽 1.8/24、线头线角都是圆的。
_ICON_STROKE = 1.8

# name -> [(kind, ...), ...]
#   ("line", x1, y1, x2, y2)
#   ("poly", [(x, y), ...])              自动闭合
#   ("oval", x1, y1, x2, y2)             描边圆
#   ("rect", x1, y1, x2, y2)             描边方（锁体这种本来就方的形状用它）
#   ("arc",  x1, y1, x2, y2, start, extent)   描边弧
_ICON_SHAPES = {
    # 三横线 = 列表。
    # ⚠️ 原来画的是「三个小圆点 + 三条线」（2026-10-04 截图放大后发现）：
    #    点只有 2.6/24 大，缩到 13px 的徽章里就是 1.4 像素 —— 三个点糊成
    #    一条竖线，看着像个「I」。小尺寸下**细节就是噪声**，改成一目了然的
    #    三横线（长度递减，避免看着像汉堡菜单）。
    "list": [("line", 3.6, 6.6, 20.4, 6.6), ("line", 3.6, 12.0, 20.4, 12.0),
             ("line", 3.6, 17.4, 14.4, 17.4)],
    # 头 + 肩
    "user": [("oval", 8.4, 3.0, 15.6, 10.2),
             ("arc", 4.2, 12.4, 19.8, 24.0, 0, 180)],
    # 电源：缺口圆环 + 竖线
    "power": [("line", 12.0, 2.6, 12.0, 11.4),
              ("arc", 4.2, 4.2, 19.8, 19.8, 120, 300)],
    # 锁：上半圆弧（锁梁）+ 方体
    "lock": [("arc", 6.6, 3.4, 17.4, 14.2, 0, 180),
             ("rect", 4.2, 11.2, 19.8, 21.0)],
    # 盾牌
    "shield": [("poly", [(12.0, 2.6), (19.8, 5.4), (19.8, 11.4),
                         (12.0, 21.0), (4.2, 11.4), (4.2, 5.4)])],
    # 地球 = 网络状态
    "globe": [("oval", 2.6, 2.6, 21.4, 21.4), ("line", 2.6, 12.0, 21.4, 12.0),
              ("oval", 8.2, 2.6, 15.8, 21.4)],
    # 下载箭头（版本/更新）
    "down": [("line", 12.0, 2.8, 12.0, 15.2),
             ("poly", [(7.4, 10.8), (12.0, 15.4), (16.6, 10.8)]),
             ("poly", [(3.6, 16.4), (3.6, 20.6), (20.4, 20.6), (20.4, 16.4)])],
}


def _draw_icon(cv, name, color, cx, cy, size=14.0, width=_ICON_STROKE,
               tags=None):
    """以 (cx, cy) 为中心画一个 size×size 的线性图标，返回画了几个图元。

    不认识的名字返回 0 —— **调用方应该拿它当失败**（见 `icon_badge` 的说明）。
    """
    shapes = _ICON_SHAPES.get(name)
    if not shapes:
        return 0
    k = float(size) / 24.0
    x0, y0 = cx - size / 2.0, cy - size / 2.0
    w = max(width * k, 1.0)

    def X(v):
        return x0 + v * k

    def Y(v):
        return y0 + v * k

    n = 0
    for s in shapes:
        kind = s[0]
        if kind == "line":
            cv.create_line(X(s[1]), Y(s[2]), X(s[3]), Y(s[4]), fill=color,
                           width=w, capstyle="round", tags=tags)
        elif kind == "poly":
            pts = []
            for (a, b) in s[1]:
                pts += [X(a), Y(b)]
            pts += [X(s[1][0][0]), Y(s[1][0][1])]      # 闭合
            cv.create_line(*pts, fill=color, width=w, capstyle="round",
                           joinstyle="round", tags=tags)
        elif kind == "oval":
            cv.create_oval(X(s[1]), Y(s[2]), X(s[3]), Y(s[4]), outline=color,
                           width=w, tags=tags)
        elif kind == "rect":
            cv.create_rectangle(X(s[1]), Y(s[2]), X(s[3]), Y(s[4]),
                                outline=color, width=w, tags=tags)
        elif kind == "arc":
            cv.create_arc(X(s[1]), Y(s[2]), X(s[3]), Y(s[4]), start=s[5],
                          extent=s[6], style="arc", outline=color, width=w,
                          tags=tags)
        n += 1
    return n


def icon_badge(parent, tk, name, fg, soft, box=22, radius=7, icon=13.0,
               page=CARD):
    """圆角彩色小徽章：浅色圆角底 + 同色系线性图标。返回那个 Canvas。

    ⚠️ 图标名写错时**抛异常**，不静默画一个空徽章：
        空徽章看着只是「这里没图标」，跟设计如此完全一样 —— 正是本项目
        最忌讳的假绿灯。宁可当场炸。
    """
    cv = tk.Canvas(parent, width=box, height=box, bg=page,
                   highlightthickness=0, bd=0)
    _draw_round_rect(cv, 0, 0, box, box, radius, soft)
    if not _draw_icon(cv, name, fg, box / 2.0, box / 2.0, size=icon):
        raise ValueError("icon_badge: 不认识的图标名 %r（可选：%s）"
                         % (name, ", ".join(sorted(_ICON_SHAPES))))
    return cv


def round_button(parent, text, command, font, bg, fg, hover, active, parent_bg,
                 height=38, radius=9, width=None, pad_x=30,
                 disabled_bg="#eef1f6", disabled_fg="#a3adbb"):
    """造一个圆角按钮，返回控件。

    ⚠️ 为什么是**工厂函数**而不是 `class RoundButton(tk.Canvas)`：
        这个文件顶层**不 import tkinter**（所有用到 Tk 的地方都是延迟导入），
        这样打包版跑 `--version` / `--purge` / `--selftest` 时不必加载 Tk，
        `build.py` 也才能安全地 `import campus_login`。
        写成类就必须在模块顶层写 `class X(tk.Canvas)` —— 那就把 tkinter
        拉进导入期了。所以在函数里再 import，和文件里其它地方一致。

    ⚠️ 为什么不用 `tk.Button`：
        Windows 上的 tk.Button 是**直角 + 系统主题描边**，一排三个方块按钮
        是这套界面最显旧的地方，而 Tk 没有圆角控件，只能 Canvas 自己画。

    🔴 兼容性契约（**改这里之前先看 run_gui 里怎么用它的**）：
        · `set_busy()` 会对返回的控件调 `configure(state="disabled"/"normal")`
        · `render_accounts()` 会对它调 `winfo_exists()`
      这两条失效是**静默的** —— 按钮看着灰了却还能点，
      正好把「忙碌时并发写账号库」那个口子重新打开。所以下面把
      `configure` 重写成先吃掉 `state`，其余原样交给 Canvas。

      另外两个方法纯粹是**为测试脚本**留的（生产代码一处都不调）：
        · `invoke()` —— `ui_shot.py` 按文字找到按钮后靠它「点一下」
        · `cget("state")` —— 让脚本能断言「忙的时候它真的灰了」
      Canvas 上没有 `state` 这个选项，不自己接住就会抛 TclError。

      短别名 `config` **故意不做**（2026-10-04 删掉过一次）：
      它和 `configure` 在 Tk 里是**两个不同的函数对象**，光写
      `config = configure` 才等价；漏了别名，`btn.config(state=...)` 会绕过
      上面那段拦截、直接抛 TclError。与其假装是个完整 Button，不如让漏掉的那次
      响亮地报错 —— 静默失效才是这套界面真正怕的东西。
    """
    import tkinter as tk

    st = {"state": "normal", "hover": False, "pressed": False}

    # ⚠️ 默认的「禁用灰」`#eef1f6` 在**页面底色**上会跟背景融成一片（它正是 BG 本身），
    #    那样一禁用按钮就整个消失了，用户只会觉得界面坏了。这里只在它真的和
    #    背景分不开时才换一档更深的灰，调用方传了能用的颜色就不动它。
    disabled_bg = _pick_disabled_bg(disabled_bg, parent_bg)

    # 不给宽度就按文字量出来。Canvas 的默认宽度是 378px —— 拿它当按钮宽度
    # 会得到一个横贯整行的怪物，所以这里必须自己算。
    if width is None:
        try:
            import tkinter.font as tkfont
            width = tkfont.Font(font=font).measure(text) + pad_x
        except Exception:
            width = 120

    class _RoundButton(tk.Canvas):
        def __init__(self, master):
            super().__init__(master, height=height, width=width,
                             highlightthickness=0, bd=0, bg=parent_bg)
            # 文字挂在属性上，供脚本按文字找控件（ui_shot.find_button）。
            # Canvas 的 create_text 内容**查不出来**，不挂这一手，
            # 截图脚本就会静默地找不到按钮 —— 它只打印一行「没找到」，
            # 然后照样跑完，看起来像成功了。
            self._rb_text = text
            # 把三种状态的填充色也挂出来，供 ui_contrast_check 核对
            # 「这个按钮看得出是个按钮」—— 它不看源码，只问控件。
            self._rb_bg = bg
            self._rb_parent_bg = parent_bg
            self._rb_disabled_bg = disabled_bg
            self._redraw()
            self.bind("<Configure>", lambda e: self._redraw())
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<Button-1>", self._on_press)
            self.bind("<ButtonRelease-1>", self._on_release)

        def invoke(self):
            """按一下。tk.Button 也有这个方法，测试脚本两种按钮都能这么点。"""
            if st["state"] != "disabled":
                command()

        # --- 绘制 ---
        def _redraw(self):
            self.delete("all")
            try:
                w = self.winfo_width()
                h = self.winfo_height()
            except Exception:
                return
            if w <= 1:                       # 还没布局完，等 <Configure> 再来
                return
            if h <= 1:
                h = height
            disabled = st["state"] == "disabled"
            if disabled:
                cur_bg, cur_fg = disabled_bg, disabled_fg
            elif st["pressed"]:
                cur_bg, cur_fg = active, fg
            elif st["hover"]:
                cur_bg, cur_fg = hover, fg
            else:
                cur_bg, cur_fg = bg, fg
            _draw_round_rect(self, 0, 0, w, h, radius, cur_bg)
            self.create_text(w / 2, h / 2, text=text, font=font, fill=cur_fg)

        # --- 交互 ---
        def _on_enter(self, _e=None):
            st["hover"] = True
            self._redraw()

        def _on_leave(self, _e=None):
            st["hover"] = st["pressed"] = False
            self._redraw()

        def _on_press(self, _e=None):
            if st["state"] == "disabled":
                return
            st["pressed"] = True
            self._redraw()

        def _on_release(self, _e=None):
            was = st["pressed"]
            st["pressed"] = False
            self._redraw()
            if was and st["state"] != "disabled":
                command()

        # --- 兼容 tk.Button 的那一小撮接口 ---
        def configure(self, **kw):
            state = kw.pop("state", None)
            if state is not None:
                st["state"] = state
                super().configure(cursor="arrow" if state == "disabled" else "hand2")
                self._redraw()
            if kw:
                super().configure(**kw)

        def cget(self, key):
            if key == "state":
                return st["state"]
            return super().cget(key)

    return _RoundButton(parent)


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


def text_panel(parent, tk, fam, base, text):
    """在 `parent` 里搭一块「可滚动、可复制」的只读长文本区。返回 (外框, Text)。

    🔴 这是**唯一实现**（2026-10-05 收敛）。
       以前 `show_text_window()`（命令行 `--help` 用）和 `run_gui` 里那个
       「使用说明」按钮各写一份，两段代码几乎逐行相同。代价已经付过一次：
       2026-10-04 界面圆角化时只改了其中一份，症状是**弹窗截图字节一字没变**
       （ui_shots 的第 5 张），查了半天才发现还有第二份实现。
       以后改「看长文本的窗口」只改这里，别再抄第二份。

    `parent` 必须是**已经建好的窗口或 Toplevel** —— 本函数不建窗口，
    也不进 mainloop（调用方自己决定怎么跑）。
    """
    # 正文区：圆角卡片（`stretch=True` —— 它要跟着窗口长大，不是跟着内容）。
    # ⚠️ 滚动条**放在卡片外面**（兄弟节点，坐在灰底上）：
    #    Tk 的 Scrollbar 是系统原生控件，只有直角；塞在圆角卡片里，
    #    它那条直边会一直顶到卡片圆角上，看着就是「圆角破了」。
    #    挪出去之后它坐在页面底色上，谁也不碍着谁。
    wrap = tk.Frame(parent, bg=BG)
    wrap.pack(fill="both", expand=True, padx=OUTER_PAD, pady=(OUTER_PAD, 0))
    sb = tk.Scrollbar(wrap)
    sb.pack(side="right", fill="y", padx=(8, 0))
    body_cv, body = round_panel(wrap, tk, 15, 13, 12, stretch=True)
    body_cv.pack(side="left", fill="both", expand=True)
    txt = tk.Text(body, wrap="word", font=(fam, base), bg=CARD, fg=FG,
                  relief="flat", bd=0, padx=14, pady=12, cursor="arrow",
                  selectbackground=BLUE_SOFT, selectforeground=FG,
                  spacing1=2, spacing3=2)
    sb.configure(command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(fill="both", expand=True)
    txt.insert("1.0", text)
    txt.configure(state="disabled")      # 只读；仍可选中、可 Ctrl+C 复制
    return wrap, txt


def text_panel_bar(parent, tk, fam, base, command):
    """长文本窗口底部那条「知道了」按钮栏（和 `text_panel` 配套）。"""
    bar = tk.Frame(parent, bg=BG)
    bar.pack(fill="x", padx=OUTER_PAD, pady=OUTER_PAD)
    h = base * 2 + 16
    round_button(bar, "知道了", command, (fam, base),
                 bg=BLUE, fg="#ffffff", hover=BLUE_DARK, active="#1e40af",
                 parent_bg=BG, height=h, radius=pill_radius(h)
                 ).pack(side="right")
    return bar


def show_text_window(title, text, width=780, height=640):
    """把一个长文本用「可滚动、可复制」的只读窗口显示出来。

    为什么需要它（2026-09-19 实测）：打包版是 `--windowed`，`sys.stdout` 是 None，
    `print` 出去的东西直接进 devnull。`--help` 如果只 print，用户双击、或者在
    cmd 里敲，都**什么都看不到** —— 只会觉得「这命令没反应」，比不实现还糟。
    `--version` 早就为同一件事弹窗了，这里保持一致。

    返回 True = 用户把窗口关掉了；False = 窗口没建起来（调用方自己兜底）。

    窗口内容走 `text_panel` / `text_panel_bar` —— 和界面上那个「使用说明」
    按钮（`SettingsWindow.show_help`）共用同一份实现，别在这里再写一遍。
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

        text_panel(r, tk, fam, base, text)
        text_panel_bar(r, tk, fam, base, r.destroy)
        r.mainloop()
        return True
    except Exception:
        log("显示文本窗口失败:\n%s" % traceback.format_exc())
        return False


class LoginApp:
    """设置窗口。原来是 `run_gui()` 里一个约 1000 行的函数，里面套着 25 个嵌套闭包。

    为什么拆成类（2026-10-05 可维护性体检）：
        原来所有状态都是闭包变量 —— 「哪些会被改、被谁改」得从头读到尾才知道，
        嵌套函数在外部也完全不可测。拆完之后：
          · 状态 = `self.*` 属性，一眼列全；
          · 每个动作 = 一个方法，能单独读、单独调。
        **行为与拆分前逐像素一致** —— 验证方式见文件末尾 `run_gui()` 的说明。

    ⚠️ 这次只做「搬家」，**没有顺手改任何数值或顺序**：
       控件的创建顺序、`pack` 参数、每一处 pady 都是原样搬过来的 ——
       这个界面是按像素标定过的（见 CONTENT_H 的说明），
       顺序一变高度就可能跟着变。要调间距请走
       「改 → 跑 content_h_probe.py 重量 → 同步 geometry_test.CONTENT_H」那条路。
    """

    # 卡片的内边距与圆角半径。**这三个数是几何常量，不是审美参数** ——
    # 它们和旧实现（1px 描边的 Frame + `body.pack(padx=18, pady=14)`）
    # **逐像素等价**：
    #     旧：box 高 = body + 2×14(ipady) + 2×1(highlightthickness) = body + 30
    #         body 宽 = inner 宽 - 2×18 - 2×1                        = inner 宽 - 38
    #     新：cv  高 = body + 2×15                                    = body + 30
    #         body 宽 = cv 宽 - 2×19                                  = inner 宽 - 38
    # 也就是说：**光换卡片实现本身，内容高度一个像素都没变**（实测验证过：
    # 旧/新实现在同一份数据下都量到 814）。
    # 🔴 但这一轮改版最后 CONTENT_H 还是从 830 变成了 **827**，来源是另外三处
    #    （列表容器去掉方角描边 −2、开机自启换成圆角复选框 +1、隐私区的小链接
    #      换成圆角片 −2）。所以别把「card() 是等价的」误读成「这一轮不用重量」。
    # 🔴 谁要动这三个数，必须同时跑 `content_h_probe.py` 重新量，
    #    并把结果同步进 `geometry_test.py` 的 CONTENT_H / MEASURED_HEIGHTS。
    # 🔴 还有一条硬约束：`pad_x >= radius` 且 `pad_y >= radius`（round_panel 会断言）。
    #    违反的话 body 那块白矩形会盖住圆角，看着就像「圆角没生效」。
    #
    # （放在类属性而不是 __init__ 里：`card()` 的默认参数要在**类定义时**求值，
    #   那一刻还没有 self —— 见 `card()` 的签名。）
    CARD_PAD_X = 19
    CARD_PAD_Y = 15
    CARD_R = 14

    def __init__(self, smoke=False):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import messagebox

        # tk / messagebox 存成属性 —— 原来是闭包捕获，拆成类后各方法得自己拿得到。
        # 仍**不放到模块顶层** import：--selftest / --auto 这些路径不需要 Tk，
        # 顶层 import 会让它们在没有 Tk 的环境里也起不来（见文件顶部的用法说明）。
        self.tk = tk
        self.messagebox = messagebox
        self.smoke = smoke

        arm_exit_watchdog()
        exit_trace("run_gui 开始（smoke=%s）" % smoke)

        self.root = tk.Tk()
        if smoke:
            self.root.withdraw()          # 自检时不显示窗口
        self.root.title("%s v%s · 设置" % (APP_TITLE, VERSION))
        self.root.configure(bg=BG)

        # 按屏幕高度决定字号，避免高分屏上字太小。
        # 字体挑选和字号规则跟 `--help` 的说明窗口共用一份（_ui_font），别再抄一遍。
        self.fam, self.BASE = _ui_font(self.root, tkfont)
        self.TITLE = self.BASE + 5
        self.SMALL = self.BASE - 2

        self.root.option_add("*Font", (self.fam, self.BASE))
        ico = icon_path()
        if ico:
            try:
                self.root.iconbitmap(default=ico)
            except Exception:
                pass

        # --- 布局骨架 ---
        # 页面 = 冷灰底 + 一张张白卡片。原来是「一个大白卡包住全部内容」，
        # 分区之间只有一条 1px 细线，层次出不来；改成每区一张卡、卡间留 10px 灰缝，
        # 一眼能分出「状态 / 账号 / 设置 / 隐私」几块。
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=OUTER_PAD, pady=OUTER_PAD)

        # 内容放进可滚动区域。屏幕矮的时候（1366x768、1080p@150%…）内容会比窗口高，
        # 没有滚动的话底部「开机自启」就永远够不到 —— 那是真的没法开自启，不是难看。
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        holder = tk.Frame(canvas, bg=BG)
        inner = tk.Frame(holder, bg=BG)
        inner.pack(fill="both", expand=True, padx=(0, 2), pady=0)
        holder_id = canvas.create_window((0, 0), window=holder, anchor="nw")

        # 这四个要跨方法用（on_wheel / card / run），挂到 self 上。
        # outer / holder_id 只在 __init__ 里用，保持局部。
        self.canvas = canvas
        self.vsb = vsb
        self.holder = holder
        self.inner = inner

        # 分区标题的「配色 + 图标」表。key 就是标题原文。
        #
        # 🔴 为什么用表而不是在调用点一个个传色值：
        #    五个分区各传各的，迟早会出现「同一个绿两种写法」这种漂移；
        #    而且新增分区时**很容易忘了配色**，于是它默默变成灰字 —— 静默退化。
        #    这里故意**不给兜底**：标题不在表里就抛异常（见下面的 sec）。
        #    新增分区必须来这里登记，这是有意的摩擦。
        self.SEC_STYLE = {
            "已保存的账号": (VIOLET, VIOLET_SOFT, "list"),
            "账号信息":     (BLUE, BLUE_SOFT, "user"),
            "开机自启":     (OK, OK_SOFT, "power"),
            "隐私":         (DANGER, DANGER_SOFT, "lock"),
            "网络状态":     (CYAN, CYAN_SOFT, "globe"),
        }

        holder.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # 让 holder 始终和 canvas 一样宽，否则内容不会跟着窗口变宽
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(holder_id, width=e.width))

        # 绑在 toplevel 上：鼠标在任意子控件上滚都会冒到这一层，不用逐个控件去绑
        # （账号列表是动态重建的，逐个绑必然漏）。而且只影响这个窗口 ——
        # 说明弹窗是另一个 Toplevel，不受影响。
        self.root.bind("<MouseWheel>", self.on_wheel)

        # 标题行：「使用说明」放最右边，紧挨着窗口右上角的最小化按钮下方
        # ⚠️ 这里的间距值在 0.3.6 统一收紧过（14→10 这类），原因见 window_geometry
        # 的说明：16:10 屏上窗口本来就吃紧，每一段 pady 都在叠加成「太高」。
        # 标题行**不套卡片**，直接坐在灰底上 —— 它相当于页面标题，套卡反而变矮胖。
        head = tk.Frame(inner, bg=BG)
        head.pack(fill="x", pady=(2, 10))
        self.lbl(head, "%s · 设置" % APP_TITLE, self.TITLE, True, bg=BG).pack(side="left")
        # 版本号紧跟在标题右边，小字弱色。用户要报问题时第一眼就能看到它。
        # **同时它也是「检查更新」的入口**（做成链接样式的小字，不新增第四个按钮 ——
        # 主按钮固定三个是既有的界面铁律，改按钮要同步改 set_busy 的控件元组）。
        #
        # ⚠️ 视觉上必须让人看出「这行字能点」：只绑 cursor 是没用的 ——
        # 截图里它跟普通灰字一模一样，用户根本不会去点。所以给它加：
        #   ① 下划线（链接的通用约定）
        #   ② 浅蓝圆角底（和后面的灰底「使用说明」按钮区分开，但不抢主按钮的视觉）
        # 这两个加起来，一眼就知道是可点的东西。
        #
        # 2026-10-04 从 `tk.Label` 换成 Canvas 圆角片（`round_chip`）：
        # Label 只有直角，和周围的圆角卡片不搭。接口是对齐的（见 round_chip 的契约），
        # `set_busy()` 改文字/配色和 `update_wiring_check()` 的点击自检都照常工作。
        self.btn_ver = round_chip(head, tk, "v%s　检查更新" % VERSION,
                                  (self.fam, self.SMALL, "underline"), BLUE_DARK, BLUE_SOFT,
                                  page=BG, height=self.SMALL + 14, hover="#d3e0ff",
                                  command=lambda: self.on_check_update())
        self.btn_ver.pack(side="left", padx=(10, 0))
        # ⚠️ 静止态底色**必须比页面底色 BG 明显深一点**（2026-10-04 实测踩到）：
        #    第一版这里写的是 `bg="#eef1f6"` —— 那正是 BG 本身，于是按钮和背景完全
        #    融成一片，截图里就剩四个灰字，和普通说明文字毫无区别，用户根本不会去点。
        #    这类错**没有任何报错**，只有把截图放大才看得出来。
        #    取值参照账号行「选用」按钮（#f1f5f9 落在白卡 #ffffff 上，差 14 级）——
        #    这里也取与 BG 差 18 级左右的一档，保证一眼能看出是个按钮。
        round_button(head, "使用说明", lambda: self.show_help(), (self.fam, self.SMALL),
                     bg="#dbe1eb", fg="#475569", hover="#ccd5e3", active="#bfc9da",
                     parent_bg=BG, height=self.SMALL + 16,
                     radius=pill_radius(self.SMALL + 16)
                     ).pack(side="right")

        # --- 状态条 ---
        # 圆角卡 + 彩色圆角图标徽章 + 文字。
        # 原来是「4px 竖条 + 小圆点 + 文字」—— 三样都在传达同一个颜色，既挤、
        # 又都做不大（竖条 4px、圆点 10px）。现在合成一个**会跟着状态变色**的
        # 图标徽章，和分区标题同一套造型：一眼能看出「这是状态」，
        # 也不会跟下面那块整块彩色底的提示消息混起来。
        #
        # ⚠️ 内边距 12/17 是量出来的：旧实现是「1px 描边 + pady=11」= 24，
        #    这里 2×12 = 24，**逐像素等价**。半径必须 <= 内边距（round_panel 会断言）。
        status_cv, status_body = round_panel(inner, tk, 17, 12, 11)
        status_cv.pack(fill="x", pady=(0, 10))
        st_icon = tk.Canvas(status_body, width=22, height=22, bg=CARD,
                            highlightthickness=0, bd=0)
        st_icon.pack(side="left", padx=(0, 10))
        status_text = self.lbl(status_body, "正在检测网络状态…", self.BASE, bg=CARD, fg=FG)
        status_text.pack(side="left")
        self.st_icon = st_icon
        self.status_text = status_text

        self.draw_status_icon(NET_UNKNOWN)

        # --- 账号列表 ---
        # ⚠️ 这里**故意不给列表容器加描边**（2026-10-04）：
        #    原来是一个 `highlightthickness=1` 的方框。卡片本身已经圆角了，
        #    里面再套一个**方角**框，四个角立刻把圆角的观感破坏掉；
        #    而要把这个内框也做成圆角，就得给它上下各加 6px 内边距 ——
        #    内容高度会凭空长 10px。收益是一个内框，代价是重标定所有几何数字，
        #    不划算。改成「靠行与行之间的分隔线来分界」：分隔线天然是直的，
        #    不会跟圆角打架。
        c_acct = self.card()
        self.sec(c_acct, "已保存的账号")
        list_box = tk.Frame(c_acct, bg=CARD)
        list_box.pack(fill="x")
        list_inner = tk.Frame(list_box, bg=CARD)
        list_inner.pack(fill="x")
        self.list_inner = list_inner

        # --- 表单 ---
        c_form = self.card()
        self.sec(c_form, "账号信息")
        self.lbl(c_form, "学号 / 账号", self.SMALL, fg=SUB).pack(anchor="w", pady=(0, 5))
        self.var_uid = tk.StringVar()
        self.entry_box(c_form, self.var_uid)
        self.lbl(c_form, "密码", self.SMALL, fg=SUB).pack(anchor="w", pady=(12, 5))

        # --- 密码框（带隐私保护）---
        # 规则都在 PasswordField 里（那样才能单独建窗口测交互），这里只负责摆放。
        self.pwd_field = PasswordField(c_form, tk, (self.fam, self.BASE),
                                       (self.fam, self.SMALL), FG)
        self.pwd_field.box.pack(fill="x")

        # --- 按钮（从上到下排列，每个占一整行）---
        # 🔴 三个主按钮是界面铁律：`set_busy` 的控件元组就是这三个，改数量要同步改它。
        btns = tk.Frame(c_form, bg=CARD)
        btns.pack(fill="x", pady=(16, 0))
        self.btn_switch = self.mkbtn(btns, "切换到此账号",
                                     lambda: self.on_switch(), "primary")
        self.btn_save = self.mkbtn(btns, "仅保存", lambda: self.on_save())
        self.btn_out = self.mkbtn(btns, "退出当前账号", lambda: self.on_logout(), "danger")

        # --- 消息 ---
        # 为什么外面再套一个 holder（2026-10-04）：`pack_forget()` 之后再 `pack()`
        # 会把控件排到**最后**，而不是回到原位 —— 原来的写法让提示消息跑到窗口最底部
        # （隐私区下面），离刚点的按钮隔了一屏。holder 常驻、只增删里面的 msg，
        # 位置就固定在账号卡片下方了。
        msg_holder = tk.Frame(inner, bg=BG)
        msg_holder.pack(fill="x")
        self.msg = self.lbl(msg_holder, "", self.BASE, bg=BLUE_SOFT, fg=FG, anchor="w",
                            justify="left", wraplength=700, padx=14, highlightthickness=1,
                            highlightbackground=LINE)
        self.msg.pack(fill="x", ipady=10, pady=(0, 10))
        self.msg.pack_forget()

        # --- 开机自启 ---
        c_auto = self.card()
        self.sec(c_auto, "开机自启")
        self.var_auto = tk.BooleanVar()
        # 圆角自绘复选框（见 round_check 的说明）。接口和 tk.Checkbutton 对齐：
        # `configure(state=...)` / `cget("text")` 都能用，`variable` 换成显式传 var。
        self.chk = round_check(c_auto, tk, "开机时自动登录校园网", self.var_auto,
                               lambda: self.on_toggle_autostart(), (self.fam, self.BASE))
        self.chk.pack(anchor="w")

        # 「开机自启」勾打着、但快捷方式其实已失效时的提示。
        # 快捷方式存的是绝对路径，挪动 exe 就会指不到；而光看勾选状态发现不了，
        # 所以必须把这种静默失败显式说出来。默认不显示。
        #
        # ⚠️ 它必须**挂在这张卡里面**（2026-10-04）：refresh_auto_warning() 是
        #    pack_forget() + pack() 的写法，而 re-pack 会把控件排到**末尾**。
        #    挂在内层卡片里，末尾就是勾选框下面 —— 正好；挂在最外层 inner 上，
        #    它就会跑到整个窗口最底部（隐私区下面），离勾选框隔了一屏。
        self.warn_auto = self.lbl(c_auto, "", self.SMALL, False, WARN,
                                  justify="left", wraplength=680)

        # --- 隐私 / 清除本机凭据 ---
        # 为什么要有这个入口（2026-09-21）：以前清凭据只有命令行 `--purge`，
        # 对普通用户等于没有 —— 他要的是「别让这台电脑再留着我的密码」，
        # 不该逼他先学会开终端。
        # 做成和「检查更新」同款的小链接，**不新增第四个主按钮**（主按钮固定三个
        # 是界面铁律；这里也不进 set_busy 的控件元组 —— 忙碌拦截由 on_purge 自己做）。
        c_priv = self.card(pady=(0, 0))
        self.sec(c_priv, "隐私")
        self.lbl(c_priv, "账号密码只存在这台电脑上，不会上传到任何服务器。",
                 self.SMALL, fg=SUB).pack(anchor="w")
        # 同样换成圆角片（和版本号那行同一个理由：Label 只有直角）。
        # 和版本号那行同一个道理：光绑 cursor 是没用的 —— 截图里它跟普通灰字
        # 一模一样，用户根本不会去点。下划线 + 浅红底 + hover 加深，才看得出能点。
        self.btn_purge = round_chip(c_priv, tk, "清除本机保存的账号密码",
                                    (self.fam, self.SMALL, "underline"), DANGER, DANGER_SOFT,
                                    height=self.SMALL + 14, hover="#fbdada",
                                    command=lambda: self.on_purge())
        self.btn_purge.pack(anchor="w", pady=(10, 0))

        # --- 状态 ---
        self.state = {"busy": False, "queue": None, "buttons": None,
                      # 升级用的临时状态。都要走界面线程，所以放在这里而不是局部变量。
                      "pending": None, "dl_text": "",
                      # 账号列表里每行的「删除」按钮。render_accounts 每次重建列表都会换一批，
                      # 所以这里存的是**当前这一批**。set_busy 必须能拿到它们 —— 见下面的说明。
                      "row_btns": []}

    # ------------------------------------------------------------ 通用小控件

    def lbl(self, parent, text, size=None, bold=False, fg=FG, bg=CARD, **kw):
        return self.tk.Label(parent, text=text, bg=bg, fg=fg,
                             font=(self.fam, size or self.BASE,
                                   "bold" if bold else "normal"), **kw)

    def card(self, pady=(0, 9), padx=CARD_PAD_X, ipady=CARD_PAD_Y, radius=CARD_R):
        """一张圆角分区卡片，撑满宽度。返回往里塞控件的 Frame。

        所有分区都走这里 —— 间距就只有一个来源，以后调整体疏密只改默认值。

        ⚠️ 为什么从 `Frame + 1px 描边` 换成 Canvas（2026-10-04）：
            Tk 没有圆角容器，`highlightthickness` 画出来的是**四个直角 + 一圈硬边**。
            一屏之内五张方卡叠在一起，就是用户说的「太方正」。Canvas 能精确画
            圆角（`_draw_round_rect` 就是为这个写的），代价是内容要放进
            `create_window` 里、高度得自己跟着内容走 —— 这部分收在 `round_panel`。

        ⚠️ 竖向数值是**量出来的**，不是看着顺眼挑的：设置窗口的高度直接等于
        内容高度（`window_geometry` 的 EXTRA_H 已归零），所以这里每加 1px，
        窗口就高 1px。5 张卡 × 上下各多 2px 就是 20px —— 在 1707x1067 这种
        屏幕上，内容一旦越过 `sh * SCREEN_H_RATIO`（906）就会冒出滚动条。
        改完请跑 `content_h_probe.py` 重新量，并同步 `geometry_test.py`
        的 `CONTENT_H`。
        """
        cv, body = round_panel(self.inner, self.tk, padx, ipady, radius)
        cv.pack(fill="x", pady=pady)
        return body

    def sec(self, parent, text):
        """分区小标题：圆角彩色图标徽章 + 深色标题字。

        ⚠️ 高度和加图标之前**完全一致**（22px 徽章 vs 22px 的 Label 行高，
           外层都是 pady=(0,7)）—— 所以换上去之后内容高度一个像素都没变。
           这条是刻意的：改竖向间距就要重标定 CONTENT_H，能不碰就不碰。
        """
        style = self.SEC_STYLE.get(text)
        if style is None:
            raise ValueError(
                "sec(): 标题 %r 没在 SEC_STYLE 里登记配色/图标。"
                "新增分区请去 LoginApp.SEC_STYLE 加一行 —— "
                "这里不给默认值，就是为了不让新分区悄悄退化成没有图标的灰字。"
                % text)
        fg, soft, icon = style
        head = self.tk.Frame(parent, bg=CARD)
        head.pack(anchor="w", pady=(0, 7))
        icon_badge(head, self.tk, icon, fg, soft).pack(side="left")
        self.lbl(head, text, self.SMALL, True, SEC_FG).pack(side="left", padx=(8, 0))

    def entry_box(self, parent, textvariable=None):
        """带 1px 圆角描边、聚焦时描边变蓝的输入框。返回 (容器, Entry)。

        为什么要包一层：`tk.Entry` **没有内部留白**（不像 ttk 有 padding），
        文字会紧贴着左边框，看着很挤。标准做法是把 Entry 放进一个有底色和
        描边的容器、再给 Entry 一点 padx —— 顺便就得到了聚焦变色的能力
        （Entry 自己的 highlight 只能画在它自己那圈，而它现在被 padx 撑不满容器）。

        ⚠️ 换成 Canvas 圆角底之后**高度一像素没变**（算过的，不是估的）：
            旧：1px 描边 + `ipady=7`              → 文字上下各 8
            新：panel `pad_y=6` + Entry `ipady=2` → 文字上下各 8
           水平同理：旧 `1+10=11`，新 `8+3=11`。
            所以这里能上圆角而**不用动 CONTENT_H**。
        ⚠️ 半径只有 5，比卡片（14）小得多 —— 这是被几何锁死的：
            半径必须 <= pad_y，而 pad_y 一加大整块就变高。
            顺带也符合常规做法：输入框的圆角本来就该比卡片小。
        """
        cv, box = round_panel(parent, self.tk, 8, 6, 5, fill=FIELD)
        cv.pack(fill="x")
        e = self.tk.Entry(box, font=(self.fam, self.BASE), relief="flat", bd=0,
                          bg=FIELD, fg=FG, highlightthickness=0, insertbackground=FG,
                          textvariable=textvariable)
        e.pack(fill="x", padx=3, ipady=2)
        e.bind("<FocusIn>", lambda ev: panel_set_line(cv, BLUE))
        e.bind("<FocusOut>", lambda ev: panel_set_line(cv, LINE))
        return cv, e

    def mkbtn(self, parent, text, cmd, kind="ghost"):
        style = {
            "primary": (BLUE, "#ffffff", BLUE_DARK, "#1e40af"),
            "ghost":   ("#f1f5f9", "#334155", "#e7ecf3", "#dbe2ea"),
            "danger":  (DANGER_SOFT, DANGER, "#fbdada", "#f8caca"),
        }[kind]
        # 胶囊形：半径 = 高度的一半减 1（见 pill_radius 的说明）。
        # 原来是 9 —— 在 34px 高的按钮上只有一丝倒角，看着还是方的。
        h = self.BASE * 2 + 14
        b = round_button(parent, text, cmd, (self.fam, self.BASE),
                         bg=style[0], fg=style[1], hover=style[2], active=style[3],
                         parent_bg=CARD, height=h, radius=pill_radius(h))
        b.pack(fill="x", pady=(0, 7))
        return b

    # ------------------------------------------------------------ 状态与刷新

    def on_wheel(self, ev):
        # 内容比视口高才滚，否则别抢事件（短内容时滚轮该干嘛干嘛）
        if self.holder.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-1 if ev.delta > 0 else 1, "units")

    def draw_status_icon(self, st):
        """按状态重画徽章（浅底 + 同色系图标）。

        浅底和图标色**必须成对换** —— 只换图标色的话，红图标压在浅绿底上
        看着就是「画错了」，而这类错在截图里不一定显眼，只能靠这里锁死。
        """
        col = NET_COLOR.get(st, "#94a3b8")
        soft = NET_SOFT.get(st, "#eef2f7")
        self.st_icon.delete("all")
        _draw_round_rect(self.st_icon, 0, 0, 22, 22, 7, soft)
        _draw_icon(self.st_icon, "globe", col, 11, 11, size=13)

    def refresh_auto_warning(self):
        try:
            stale = autostart_stale()
            old_args = autostart_outdated()
        except Exception:
            stale = old_args = False
        # 注意：这里是普通 Label，**不认 Markdown** —— 用「」而不是 ** 来强调，
        # 否则用户看到的就是一串星号（老版本就是这么显示的）。
        if stale:
            self.warn_auto.configure(
                text="⚠ 开机自启项指向的是 exe 的「旧位置」，开机时实际不会生效。\n"
                     "   把下面的勾取消、再重新勾选一次即可修好。")
            self.warn_auto.pack(anchor="w", pady=(8, 0))
        elif old_args:
            # 0.2.0 装的 .lnk 参数是 `--auto`，没有 `--guard` —— 开机照常登录，
            # 但掉线后不会自动重连。不提示的话用户会以为新版本没修好。
            self.warn_auto.configure(
                text="⚠ 开机自启还是「旧设置」：开机照常登录，但掉线后不会自动重连。\n"
                     "   把下面的勾取消、再重新勾选一次即可升级。")
            self.warn_auto.pack(anchor="w", pady=(8, 0))
        else:
            self.warn_auto.pack_forget()

    def show(self, text, kind="info"):
        color = {"ok": (OK_SOFT, "#14532d"),
                 "err": (DANGER_SOFT, "#7f1d1d"),
                 "info": (BLUE_SOFT, "#1e3a8a")}[kind]
        self.msg.configure(text=text, bg=color[0], fg=color[1])
        self.msg.pack(fill="x", ipady=10, pady=(0, 10))

    def set_busy(self, b):
        self.state["busy"] = b
        st = "disabled" if b else "normal"
        for w in (self.btn_switch, self.btn_save, self.btn_out):
            w.configure(state=st)
        # 🔴 账号列表里每行的「删除」也必须跟着灰掉（2026-10-03 加）。
        #    原来这个元组里只有上面三个按钮，而「删除」是 render_accounts 每次
        #    现建的、不在其中 —— 于是「切换账号 / 退出账号」在后台跑的那十几秒里，
        #    用户仍然能点删除。那条路会走 drop() → mutate_accounts()，
        #    和后台线程的 update_account_record() 抢同一份账号库。
        #    （锁已经能兜住，但让入口根本不出现更好 —— 用户也不该在忙的时候删东西。）
        #
        #    这批按钮会被 render_accounts 整批 destroy 掉，所以先问「还在不在」再动它
        #    —— 对已销毁的控件 configure 会抛 TclError。不用 try/except 是为了
        #    和上面那三个按钮保持同一种写法，也免得又添一个「静默吞异常」。
        for w in self.state.get("row_btns") or ():
            if w.winfo_exists():
                w.configure(state=st)
        # 升级期间版本号小字也不能点，否则会并发起两个检查/两次替换。
        try:
            self.btn_ver.configure(cursor="arrow" if b else "hand2")
        except Exception:
            pass
        # 用「改文案」而不是「加控件」来表示忙碌：不加第四个按钮，
        # 也就不用去动 set_busy 那个控件元组（那是最容易被改漏的地方）。
        try:
            if b and self.state["dl_text"]:
                self.btn_ver.configure(text=self.state["dl_text"])
            elif not b:
                self.btn_ver.configure(text="v%s　检查更新" % VERSION,
                                       fg=BLUE_DARK, bg=BLUE_SOFT,
                                       font=(self.fam, self.SMALL, "underline"))
        except Exception:
            pass

    def refresh_status(self):
        def work():
            st = network_state()
            # 程序刚起来时第一个网络请求赶上 DNS 冷启动（实测首次 7.4 秒），
            # 有可能「测不出来」。隔几秒再试一次，免得状态栏一直停在未知上
            # ——用户打开程序第一眼看到的就是这行状态。
            if st == NET_UNKNOWN:
                time.sleep(3)
                st = network_state()
            self.state["queue"].put(("status", st))
        threading.Thread(target=work, daemon=True).start()

    def render_accounts(self):
        for w in list(self.list_inner.winfo_children()):
            w.destroy()
        # 上面 destroy 之后，旧的那批「删除」按钮已经失效 —— 重新收集。
        self.state["row_btns"] = []
        store = load_accounts()
        cfg = load_config()
        cur = cfg.get("userId") or ""
        ids = list(store["accounts"].keys())
        ids.sort(key=lambda k: (store["accounts"][k].get("lastSuccess") or "", k),
                 reverse=True)

        if not ids:
            self.lbl(self.list_inner, "暂无保存的账号", self.SMALL,
                     fg=SUB).pack(anchor="w", padx=14, pady=14)
            return
        for i, uid in enumerate(ids):
            a = store["accounts"][uid]
            row = self.tk.Frame(self.list_inner, bg=CARD)
            row.pack(fill="x")
            if i:
                self.tk.Frame(self.list_inner, bg=LINE, height=1).pack(fill="x")
            left = self.tk.Frame(row, bg=CARD)
            left.pack(side="left", fill="x", expand=True, padx=14, pady=9)
            line = self.tk.Frame(left, bg=CARD)
            line.pack(anchor="w")
            self.lbl(line, uid, self.BASE, True).pack(side="left")
            if uid == cur:
                round_chip(line, self.tk, "当前使用", (self.fam, self.SMALL - 1),
                           BLUE_DARK, BLUE_SOFT).pack(side="left", padx=(8, 0))
            if uid == store.get("lastOnline"):
                round_chip(line, self.tk, "上次登录", (self.fam, self.SMALL - 1),
                           "#15803d", OK_SOFT).pack(side="left", padx=(4, 0))
            if a.get("lastSuccess"):
                self.lbl(left, "上次成功：%s" % a["lastSuccess"], self.SMALL - 1,
                         fg=SUB).pack(anchor="w", pady=(3, 0))
            right = self.tk.Frame(row, bg=CARD)
            right.pack(side="right", padx=14)
            # ⚠️ 这里**故意不做整行 hover**（2026-10-04 试过又撤了）：
            #    圆角按钮是 Canvas 画的，它四个角露出的是自己的 bg（=CARD 白）。
            #    整行一变灰，那两个按钮的角上就顶着两块白方块。
            #    要把角一起染上就得给按钮也改 bg，可按钮的 <Enter>/<Leave>
            #    已经用来做自己的 hover 了 —— Tk 的 bind 是**覆盖**不是叠加，
            #    再绑一次会把按钮自身的悬停效果弄没。
            #    收益不大、坑很实在，所以只保留按钮自己的悬停反馈。
            rh = self.SMALL + 16
            round_button(right, "选用", lambda u=uid: self.pick(u),
                         (self.fam, self.SMALL),
                         bg="#f1f5f9", fg="#334155", hover="#e7ecf3", active="#dbe2ea",
                         parent_bg=CARD, height=rh, radius=pill_radius(rh), pad_x=26
                         ).pack(side="left", padx=(0, 6))
            # 「删除」原来的静止态是**白底**，在白卡片上完全看不出是个按钮
            # （截图放大后才发现的）。给一层很浅的红：既看得出能点，
            # 又不会像「退出当前账号」那样抢眼。
            b_del = round_button(right, "删除", lambda u=uid: self.drop(u),
                                 (self.fam, self.SMALL),
                                 bg="#fef6f6", fg=DANGER, hover=DANGER_SOFT,
                                 active="#f8caca", parent_bg=CARD,
                                 height=rh, radius=pill_radius(rh), pad_x=26)
            b_del.pack(side="left")
            # 新建的按钮必须**按当前忙碌状态**初始化：任务结束时 handle_msg 是先
            # load_all()（会重建列表）再 set_busy(False) 的，中间那一刻如果新按钮
            # 默认是 normal，忙碌期间就又能点了 —— 正好绕开刚加的那道禁用。
            if self.state["busy"]:
                b_del.configure(state="disabled")
            self.state["row_btns"].append(b_del)

    def pick(self, uid):
        store = load_accounts()
        self.var_uid.set(uid)
        self.pwd_field.set(store["accounts"].get(uid, {}).get("passwd", ""))

    def drop(self, uid):
        if not self.messagebox.askyesno(APP_TITLE, "从列表中删除账号 %s 吗？" % uid):
            return

        def _do(store):
            store["accounts"].pop(uid, None)
            if store.get("lastOnline") == uid:
                store["lastOnline"] = ""

        mutate_accounts(_do)
        self.render_accounts()

    def load_all(self):
        cfg = load_config()
        self.var_uid.set(cfg.get("userId") or "")
        self.pwd_field.set(cfg.get("passwd") or "")
        uid = cfg.get("userId")
        if uid:
            def _do(store):
                if uid not in store["accounts"]:
                    store["accounts"][uid] = {"passwd": cfg.get("passwd") or "",
                                              "lastSuccess": "", "lastAttempt": "",
                                              "lastResult": ""}
            mutate_accounts(_do)
        self.render_accounts()
        try:
            self.var_auto.set(is_autostart_on())
            self.chk.configure(state="normal")
            self.refresh_auto_warning()
        except Exception:
            self.var_auto.set(False)
            self.chk.configure(state="disabled")

    # ------------------------------------------------------------ 后台任务

    def start_job(self, running_text, fn):
        if self.state["busy"]:
            return
        self.show(running_text, "info")
        self.set_busy(True)

        def work():
            try:
                r = fn()
            except Exception:
                log("任务异常:\n%s" % traceback.format_exc())
                r = (-1, "操作异常，请查看日志")
            self.state["queue"].put(("job", r))

        threading.Thread(target=work, daemon=True).start()

    def handle_msg(self, msg):
        """处理一条队列消息。异常往上抛，由 drain_queue 记日志并继续下一条。"""
        kind, payload = msg
        if kind == "status":
            st = payload
            # 徽章的浅底 + 图标色一起换（见 draw_status_icon）。
            # 原来是「圆点 + 左侧竖条 + 文字」三处一起改；现在合成一个徽章，
            # 少了两处要同步的地方，也就少了两处能漏改的地方。
            self.draw_status_icon(st)
            self.status_text.configure(text=NET_TEXT.get(st, "网络状态未知"))
        elif kind == "job":
            code, text = payload
            try:
                self.load_all()
                self.show(text, "ok" if code == 0 else "err")
                self.refresh_status()
            finally:
                # **必须在 finally 里**：上面任何一步抛异常，界面就会永久卡在
                # "忙碌"——三个按钮全灰点不动，而用户看不到任何原因，
                # 看起来就是"窗口卡死了"。这正是最难查的那种症状。
                self.set_busy(False)
        elif kind == "upd_progress":
            # 下载进度。只改文案，不碰按钮状态 —— 进度消息会来很多次，
            # 每次都去动控件代价大而且容易闪。
            self.state["dl_text"] = payload
            try:
                self.btn_ver.configure(text=payload)
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
                    ok = self.messagebox.askyesno(
                        APP_TITLE,
                        "发现新版本 v%s。\n\n%s\n\n%s\n\n是否现在下载并升级？\n"
                        "（升级过程会关闭当前窗口，稍后自动重开）" % (
                            v, info.get("notes") or "无更新说明",
                            updater.source_hint()))
                    if not ok:
                        self.show("已跳过升级，当前仍是 v%s" % VERSION, "info")
                        self.set_busy(False)
                        return
                    self.state["pending"] = info
                    self.state["dl_text"] = "下载中 0%"
                    self.show("正在下载 v%s…" % v, "info")
                    self.start_upgrade(info)
                elif st == "done":
                    # 替换已经成功，现在重启到新版本。
                    self.show("升级完成，正在重新启动…", "ok")
                    self.root.update_idletasks()
                    self.restart_self()
                else:
                    # 结果里带上更新源：用户是自己点「检查更新」才看到这行的，
                    # 不存在打扰；而他有权知道这程序在跟哪个地址说话。
                    self.show(updater.describe(st, info) + "\n" + updater.source_hint(),
                              "ok" if st in (updater.STATE_CURRENT,) else "info")
                    self.set_busy(False)
            except Exception:
                # 任何意外都不能让界面卡在忙碌态。
                log("处理更新结果异常:\n%s" % traceback.format_exc())
                self.show("检查更新时出错，请查看日志", "err")
                self.set_busy(False)

    def start_upgrade(self, info):
        """下载 + 校验 + 替换自己。全在后台线程里做，界面只收进度。"""
        def work():
            exe = None
            dest = None
            try:
                exe = sys.executable if getattr(sys, "frozen", False) else None
                if exe is None:
                    self.state["queue"].put(("upd_result", (
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
                # 记下这次更新的落点。排查「更新完 .old 落在哪」「更新为什么失败」时，
                # 这是唯一能事后看到的事实 —— 这条路径以前**一行日志都没有**，
                # 用户报「更新没生效」时只能靠猜。
                log("开始下载更新 %s：%s（临时目录 %s）"
                    % (info.get("version"), dest, work_dir))
                self.state["queue"].put(("upd_progress", "下载中 0%"))

                def on_prog(got, total):
                    # 节流：每 3% 才推一次，不然队列会被进度消息淹没。
                    pct = 0 if not total else int(got * 100 / total)
                    if pct < 3 or pct == getattr(on_prog, "_last", -1):
                        return
                    if pct < getattr(on_prog, "_last", 0) + 3 and pct != 100:
                        return
                    on_prog._last = pct
                    self.state["queue"].put(("upd_progress", "下载中 %d%%" % pct))

                def on_retry(attempt, attempts, why):
                    # 重试必须说一句：否则进度会卡在某个百分比不动，
                    # 用户以为死机了就会再点一次 —— 那就并发下两份、还可能互相覆盖。
                    self.state["queue"].put(("upd_progress", "网络中断，正在重试 %d/%d…"
                                             % (attempt, attempts)))

                ok, reason = updater.download(
                    info["url"], dest, sha256=info.get("sha256"),
                    size=info.get("size"), on_progress=on_prog, on_retry=on_retry)
                if not ok:
                    # 下载失败时 download() 自己会清掉 `.part`，但 `.new` 可能
                    # 是上一轮留下的（下载成功、替换失败）。12 MB 的东西别留在盘上。
                    updater.discard(dest)
                    log("更新下载失败：%s" % reason)
                    self.state["queue"].put(("upd_result", (
                        updater.STATE_UNKNOWN, "下载失败：%s" % reason)))
                    return
                log("更新包下载完成并校验通过：%s" % dest)

                self.state["queue"].put(("upd_progress", "正在替换…"))
                # work_dir 必须一路传下去：`.old` 落在哪由它决定，
                # 漏传就退回 exe 同目录（桌面），等于这次修复没生效。
                ok, reason = updater.apply_update(exe, dest, work_dir)
                if not ok:
                    updater.discard(dest)
                    log("更新替换失败：%s" % reason)
                    self.state["queue"].put(("upd_result", (
                        updater.STATE_UNKNOWN, "升级失败：%s" % reason)))
                    return
                # 替换成功 → 重启新版本。**先放消息再退出**，让界面把提示显示出来。
                # ⚠️ 提示里要带版本号：只说「升级完成」的话，用户回头看桌面发现
                #    文件名没变（本来就是换成同名文件），会以为根本没更新 ——
                #    2026-09-20 那次「桌面没有出现新版本」就是这么误判的。
                #    顺带说清旧版本去哪了，免得他去桌面上找那个 .old。
                log("更新替换成功：旧版本备份在 %s（下次启动自动清理）"
                    % (os.path.dirname(updater.old_path_of(exe, work_dir) or "")
                       or "数据目录"))
                self.state["queue"].put(("upd_result", (
                    "done", "已升级到 v%s，正在重新启动…" % info.get("version"))))
            except Exception:
                log("升级异常:\n%s" % traceback.format_exc())
                # 异常路径同样别留残骸（`.part` 可能下到一半）。清不掉也无所谓，
                # 下次启动的 cleanup_stale() 还会再扫一遍。
                if dest:
                    updater.discard(dest + ".part")
                    updater.discard(dest)
                self.state["queue"].put(("upd_result", (
                    updater.STATE_UNKNOWN, "升级过程出错，请查看日志")))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            drain_queue(self.state["queue"], self.handle_msg)
        except Exception:
            log("界面轮询异常:\n%s" % traceback.format_exc())
        # 无论上面发生了什么，下一次轮询都要排上。否则轮询链条一断，
        # 界面就再也不刷新了（后台任务的结果永远回不来）。
        try:
            self.root.after(120, self.poll)
        except Exception:
            log("排下一次轮询失败:\n%s" % traceback.format_exc())

    # ------------------------------------------------------------ 按钮动作

    def restart_self(self):
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
            self.show("升级已完成，请手动重新打开程序", "ok")
            self.set_busy(False)
            return
        exit_now(0)

    def on_check_update(self):
        """点版本号触发。全流程在后台线程，界面只收队列消息。"""
        if self.state["busy"]:
            self.show("正在处理上一个操作，请稍候", "info")
            return
        self.state["dl_text"] = ""
        self.set_busy(True)
        self.show("正在检查更新…", "info")

        def work():
            try:
                st, info = updater.check(VERSION)
            except Exception:
                log("检查更新异常:\n%s" % traceback.format_exc())
                st, info = updater.STATE_UNKNOWN, "检查更新时出错"
            self.state["queue"].put(("upd_result", (st, info)))
        threading.Thread(target=work, daemon=True).start()

    def form(self):
        uid = self.var_uid.get().strip()
        pwd = self.pwd_field.get()
        if not uid:
            self.show("请填写账号", "err")
            return None
        if not pwd:
            self.show("请填写密码", "err")
            return None
        return uid, pwd

    def upsert(self, uid, pwd, overwrite):
        upsert_account(uid, pwd, overwrite)

    def on_save(self):
        f = self.form()
        if not f:
            return
        cfg = load_config()
        cfg["userId"], cfg["passwd"] = f
        save_config(cfg)
        self.upsert(f[0], f[1], True)
        self.load_all()
        self.show("已保存", "ok")

    def on_purge(self):
        """清除本机保存的账号密码（设置页最下面那个入口）。

        ⚠️ 顺序**不能反**：先 note_logout()、再 purge_local_data()。
           · note_logout() 写的是 accounts.json —— 而 purge 会把这个文件删掉。
             反过来的话，purge 刚删完又被 note_logout 重建出来，等于没清干净。
           · 它在这里的作用是**盖住那 30 秒窗口**：守护最多每 30 秒才醒一次，
             醒来的第一件事虽然是查凭据还在不在（guard_should_exit_for_purge），
             但万一它这一拍先走到了重连判定，「刚主动退出过」这个标记能拦住它
             拿着内存里那份密码再登一次 —— 用户刚点完清除，不该看到它又登回去。
        """
        if self.state["busy"]:
            self.show("正在处理上一个操作，请稍候", "info")
            return
        d = data_dir()
        try:
            running, _why = guard_running()
        except Exception:
            running = False
        # 守护在跑要单独说一句：它是**另一个进程**，删文件删不掉它内存里那份，
        # 得等它自己退（最多 30 秒）。不说明的话，用户以为点完就立刻干净了。
        extra = ("\n\n现在有守护进程正在运行：凭据会被删掉，它最多 30 秒后自己退出，"
                 "不会拿着内存里的密码再去登录。") if running else ""
        if not self.messagebox.askyesno(
                APP_TITLE,
                "确定要清除本机保存的账号密码吗？\n\n"
                "会删掉这个目录里的账号、密码、登录记录和日志：\n%s\n\n"
                "删掉之后要重新输入账号密码才能登录。\n"
                "开机自启如果还开着，开机时会跑一次但登不上（没账号了），"
                "建议把那个勾也取消掉。\n\n"
                "只影响这台电脑，不影响你的校园网账号本身。%s" % (d, extra)):
            return
        try:
            _d, removed, failed = purge_local_data_with_marker()
        except Exception:
            log("清除本机账号数据失败:\n%s" % traceback.format_exc())
            self.show("清除失败，请查看日志", "err")
            return
        # 界面上别留残影：输入框里的账号密码、列表里的账号都得跟着消失，
        # 否则用户看到「清除成功」但屏幕上还写着他的学号，会怀疑根本没清。
        self.var_uid.set("")
        self.pwd_field.set("")
        self.render_accounts()
        if failed:
            self.show("有文件没删掉：%s\n（可能被别的程序占着，过一会儿再试一次）"
                      % "、".join(failed), "err")
            return
        self.show("已清除本机保存的账号密码%s。\n"
                  "守护进程如果正在运行，它会在 30 秒内自己退出。"
                  % ("（删掉了 %s）" % "、".join(removed) if removed else "（本来就没有）"),
                  "ok")

    def action_runner(self, fn):
        """跑一个 do_* 动作，把 (退出码, 给用户看的一句话) 交回主线程"""
        def run():
            code = fn()
            r = read_json(RESULT_FILE) or {}
            return code, (r.get("message") or "操作完成")
        return run

    def on_switch(self):
        f = self.form()
        if not f:
            return
        uid, pwd = f
        prev = load_config().get("userId") or ""
        cfg = load_config()
        cfg["userId"], cfg["passwd"] = uid, pwd
        save_config(cfg)
        self.upsert(uid, pwd, False)            # 不覆盖已存密码，回滚要靠它
        self.start_job("切换账号中，可能需要等待十几秒…",
                       self.action_runner(lambda: do_switch(uid, pwd, prev)))

    def show_help(self):
        """使用说明弹窗。

        说明文字内置在 HELP_TEXT 里，所以程序不依赖外部 .txt —— 单独一个 exe
        拷到别的电脑上，照样点得开说明。
        """
        win = self.tk.Toplevel(self.root)
        win.title("使用说明 · %s v%s" % (APP_TITLE, VERSION))
        # 窗口底色 = **页面底色**，不是卡片底色 —— 正文区是一张圆角卡片，
        # 卡片和窗口同色的话圆角就看不出来了（这是刚把直角改成圆角时最
        # 容易漏的一步：改卡片忘了改窗口）。
        win.configure(bg=BG)
        win.transient(self.root)         # 跟随主窗口，不单独占一个任务栏项
        ico = icon_path()
        if ico:
            try:
                win.iconbitmap(default=ico)
            except Exception:
                pass

        # 正文 + 按钮栏都走模块级的 `text_panel` / `text_panel_bar` ——
        # 和 `show_text_window()`（命令行 `--help` 用）共用**同一份实现**。
        # 这两段以前是各写一份的：2026-10-04 圆角化时只改了一份，
        # 症状是**弹窗截图字节一字没变**，靠那个才发现还有第二份。
        text_panel(win, self.tk, self.fam, self.BASE, HELP_TEXT + SWITCH_HELP)
        text_panel_bar(win, self.tk, self.fam, self.BASE, win.destroy)

        if self.smoke:
            win.withdraw()               # 自检时不真的弹出来
        else:
            win.update_idletasks()
            w = min(720, max(520, int(self.root.winfo_width() * 0.92)))
            h = min(700, max(440, int(self.root.winfo_height() * 0.92)))
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
            win.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))
            win.minsize(420, 320)
            win.focus_set()
        return win

    def on_logout(self):
        if not self.messagebox.askyesno(
                APP_TITLE, "确定退出当前校园网账号吗？\n\n退出后本机将无法上网。"):
            return
        self.start_job("正在退出当前账号…", self.action_runner(do_logout))

    def on_toggle_autostart(self):
        if self.state["busy"]:
            # 有任务在跑（切换账号 / 退出 / 自启设置本身）。此刻不该改自启设置，
            # 把勾同步回**真实状态**，免得用户以为改了。
            self.var_auto.set(is_autostart_on())
            return
        want = self.var_auto.get()

        def job():
            ok, why = set_autostart(want)
            if not ok:
                return 1, "设置失败：%s" % why
            if not want:
                return 0, "已关闭开机自启"
            # 说清楚走的是哪条路。只有建不了计划任务时才会退回启动文件夹，
            # 而那条路要等到开机后约 85 秒（计划任务约 25 秒）—— 不告诉用户的话，
            # 他下次开机还是会觉得「怎么又这么慢」。
            if autostart_mode() == "task":
                return 0, "已开启开机自启"
            return 0, "已开启开机自启（本机退回用「启动文件夹」，开机会慢约 1 分钟）"

        # 🔴 为什么必须放后台（2026-10-03 改）：
        #    set_autostart() → task_create() → _ps()，而 _ps 的超时是 **60 秒**。
        #    正常约 1 秒，但组策略禁用 / 任务计划服务停了 / 杀软拦 PowerShell 时会
        #    走满 60 秒 —— 在主线程跑就是「界面冻 60 秒、而且一个字的提示都没有」，
        #    用户只会以为程序死了（很可能直接强杀进程）。
        #    ⚠️ 失败时**不用**手动把勾翻回去：handle_msg → load_all() 会用
        #       is_autostart_on() 读真实状态同步复选框，refresh_auto_warning() 也跟着
        #       更新 —— 这两件事 load_all() 都做了，原来那行 var_auto.set 是多余的手工活，
        #       而且一旦漏了某个分支就会「勾与实际对不上」。
        self.start_job("正在设置开机自启…", job)

    def _on_wm_close(self):
        exit_trace("收到 WM_DELETE_WINDOW（用户点了 X）")
        self.root.destroy()
        exit_trace("root.destroy() 返回")

    # ------------------------------------------------------------ 启动

    def run(self):
        """排好轮询、算好几何、进事件循环。返回 0。

        ⚠️ 这一段原来是 `run_gui()` 的收尾（def 之后的那几十行），
           原样搬过来 —— 顺序不能动：`load_all()` 必须在排 `poll` 之前，
           `update_idletasks()` 必须在量 `holder.winfo_reqheight()` 之前。
        """
        # queue 已在模块顶层导入（poll() 要按名字捕获 queue.Empty）
        self.state["queue"] = queue.Queue()
        self.root.option_add("*Font", (self.fam, self.BASE))

        self.load_all()
        self.root.after(120, self.poll)
        self.root.after(60, self.refresh_status)

        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 内容需要多高：holder 是滚动区里那层（含卡片内留白），再加上最外层留白。
        # **不要在这里加 EXTRA_H** —— 它由 window_geometry 统一处理（0.3.6 起已归零），
        # 这里再加一次就白高 40px。
        # 也别用 root.winfo_reqheight()：内容进了 canvas 之后它就不再反映内容高度了。
        reqh = self.holder.winfo_reqheight() + 2 * OUTER_PAD
        w, h, x, y = window_geometry(sw, sh, reqh)
        # 内容装不下才挂滚动条。一次性决定，不做动态显隐 —— 动态 pack/pack_forget
        # 会改变 canvas 宽度、又触发 <Configure>，容易自己把自己绕进去。
        if reqh > h:
            self.vsb.pack(side="right", fill="y", before=self.canvas)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.minsize(*minsize_for(sw, sh))
        if self.smoke:
            self.root.update_idletasks()
            # 密码框接线自检：确认界面里读到的是真密码、看到的是打码。
            # 只有真建了窗口、拿到 run_gui 里那个实例才测得到（见 pwd_wiring_check）。
            # 失败就抛出去 —— 让 --guitest 落成 GUI_FAIL，别静默放过。
            wiring = pwd_wiring_check(self.pwd_field,
                                      (load_config().get("passwd") or None))
            log(wiring)
            # 更新入口接线自检：真给版本号小字发一个点击，确认整条链通。
            # 同 pwd_wiring_check —— 光看代码证明不了「点下去真的会跑」。
            log(update_wiring_check(self.btn_ver))
            # 按钮配色自检：确认没有哪个按钮的底色和它坐的背景同色
            # （同色 = 按钮隐形。这类错不抛异常，只有放大截图才看得见）。
            log(ui_contrast_check(self.root))
            # 顺便把说明弹窗也建一次，确认它能起来（不显示，建完就销毁）
            try:
                hv = self.show_help()
                hv.update_idletasks()
                hv.destroy()
            except Exception:
                log("说明弹窗自检失败:\n%s" % traceback.format_exc())
            # 自检路径不走 mainloop，显式销毁，别把 Tk 留给解释器收尾
            try:
                self.root.destroy()
            except Exception:
                pass
            return 0
        # 点 X 时先记一笔。行为与 Tk 默认（无 handler 时直接 destroy）完全一致，
        # 只是为了知道「关闭请求确实到了主线程」，把卡点范围再收窄一格。
        self.root.protocol("WM_DELETE_WINDOW", self._on_wm_close)
        exit_trace("即将进入 mainloop")
        self.root.mainloop()
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


def run_gui(smoke=False):
    """打开设置窗口。**保留这个函数名**（`main()`、`ui_shot.py`、`layout_probe.py`、
    `content_h_probe.py`、`content_h_matrix.py`、`fresh_test.py` 都按这个名字调它）。

    实现搬到了 `LoginApp`（2026-10-05 拆类），这里只做转发 ——
    调用方一行都不用改。
    """
    return LoginApp(smoke).run()




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
        d, removed, failed = purge_local_data_with_marker()
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

    # 清理上一次自我更新留下的 .old / .new / .new.part。**必须放在这里**（每次启动都跑）：
    # 替换时旧 exe 正被当前进程占用，删不掉；只能等它退出后、下次启动时清。
    #
    # ⚠️ **不能只清一次**。`.old` 常被别的实例占着 —— 最典型的是守护进程：更新发生时
    #    它正跑着那个 exe，改名后它继续从 `.old` 跑完剩下的时间（最长 30 分钟），
    #    这期间 Windows 拒绝删除。清一次失败就**永远留着**，用户看到的就是
    #    「更新完 .old 还在」（2026-09-20 反馈，实测复现见 _repro_lock.py）。
    #    所以：清一次，还有剩的就转后台按 STALE_SWEEP_DELAYS 重试。
    # ⚠️ 结果**必须写日志**。这里以前静默吞掉一切：清干净了和清不掉在日志里长得一样，
    #    用户报问题时无从下手 —— 项目的老毛病，「搜不到 ≠ 干净」。
    # ⚠️ 传 data_dir() 不能漏：0.3.3 起临时文件落在数据目录，漏传就退回 exe 同目录
    #    （用户的桌面），新位置的 `.old` 永远没人清。cleanup_stale_all 两个位置都扫，
    #    所以老版本（≤0.3.2）留在桌面的历史残留也能一并收掉。
    try:
        swept, still = sweep_stale_once()
        if swept:
            log("已清理上次更新残留：%s" % "、".join(swept))
        if still:
            log("更新残留暂时删不掉（多半有实例正占着）：%s —— 转后台重试"
                % "、".join(still))
            threading.Thread(target=sweep_stale_retry, daemon=True).start()
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
            # 现在还剩哪些更新残留（.old / .new / .new.part）。
            # 用户报「更新完 .old 还在」时，这一行就是第一手答案：是清干净了，
            # 还是被某个实例占着删不掉。以前这个信息**完全没有出口**。
            "更新残留: %s" % stale_leftover_desc(),
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
        # 更新源也打出来 / 落盘：这个开关本来就是给排障和脚本用的，
        # 「到底在跟哪个地址说话」是排查「检查更新失败」时的第一个问题。
        hint = updater.source_hint()
        print(text)
        print(hint)
        try:
            with open(os.path.join(data_dir(), "checkupdate.txt"), "w",
                      encoding="utf-8") as f:
                f.write("%s\n%s\n状态: %s\n清单地址: %s\n" % (
                    text, hint, st, updater.MANIFEST_URL))
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
