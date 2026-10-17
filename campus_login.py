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
import base64
import copy
import queue
import threading
import subprocess
import tempfile
import traceback
import hashlib
import hmac
import http.cookiejar
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

# 检查更新模块。纯标准库，不引入新依赖（打包体积的三条手段一条都不破坏）。
# ⚠️ 顶层 import 而不是延迟 import：updater 只 import 标准库，开销可以忽略，
#    但**打包时必须让 PyInstaller 看见它** —— 藏在函数里的 import 容易被漏掉。
import updater

# 「账号中心」功能（郑航自助服务）的取数层。**和 updater 同理**：
# 顶层 import，打包时必须让 PyInstaller 看见它 —— 藏在函数里的 import 容易被漏掉。
# 它只 import 标准库（base64/datetime/hashlib/http.cookiejar/json/urllib），不引入新依赖。
import self_service

# 「账号中心」的**写操作**层（B 档：踢设备下线 / 解绑 MAC / 停机复机 / 改密码）。
# 🔴 它和 `self_service` 是**故意分开**的两个模块：
#    那边有测试扫源码钉着的「只读」契约（出现 kickonline 之类字面量就报失败），
#    而这边全是**不可逆**动作。两边风险等级不同，规矩也相反
#    （读可以重试，写**绝不能**重试）—— 混在一个文件里早晚有人记混。
# 顶层 import 的理由同 `self_service`：PyInstaller 要看得见。
import self_actions

# 「账号中心」的**本地流量历史**（存本机，下次不登录也能看）。
# ⚠️ 它和 self_service 是**故意分开**的两个模块：self_service 有一条被测试钉死的
#    性质 —— **它不碰磁盘**（不 import os / pathlib、没有 open）。那条性质保护的是
#    「取数层不会把响应偷偷落盘」，而响应里带身份证号和明文密码。
#    所以：取数归取数，存盘归存盘。
# 同样顶层 import（PyInstaller 才看得见）。
import self_history

# 界面配色。**和站点 CSS 共用一份**（2026-10-05 抽出，理由见 palette.py）。
# 以前这 19 个常量长在本文件里，而站点那边另有一份手工同步的副本 ——
# 实测两边重合 15 个，改配色要记得改两处，迟早会漏。
# 和 updater 同理：顶层 import，**打包时必须让 PyInstaller 看见它**。
from palette import (BG, CARD, FG, SUB, LINE, FIELD, SEC_FG,
                     BLUE, BLUE_DARK, BLUE_SOFT, BLUE_HOVER, BLUE_ACTIVE,
                     OK, OK_SOFT, DANGER, DANGER_SOFT, WARN, WARN_SOFT,
                     # 2026-10-17 换 Fluent 时新增：导航窗格一套 + 次要控件一套。
                     # 原来是散在本文件里的字面量（`#f1f5f9` / `#e7ecf3` /
                     # `#d3e0ff` / `#dbe1eb`…），换配色时得逐个 grep 才不会漏。
                     NAV_BG, NAV_HOVER, NAV_ACTIVE, NAV_BAR,
                     BTN_SOFT, BTN_SOFT_ON_BG, BTN_HOVER, BTN_ACTIVE, BTN_PRESS,
                     BTN_FG, DANGER_HOVER, DANGER_ACTIVE, CHECK_EDGE, NEUTRAL_SOFT,
                     MUTED, BLUE_PRESS, OK_FG, DANGER_FG, BLUE_FG,
                     CHART_AVG, CHART_ZERO)

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
VERSION = "0.6.0"

# ==================== 学校参数：换学校只改 config.json ====================
#
# 🔴 本节列出的键 + 下面那几个常量，是**所有「跟哪所学校有关」的默认值**。
#    换学校 / 换校区**不用改源码** —— 把要改的键写进本机 config.json 就行：
#        %ProgramData%\CampusLogin\config.json
#    （README 的「适配其它学校」一节列的正是这几个键，两边要一起改。）
#
#    可覆盖的键（config.json 键名 → 默认值 → 取值函数）：
#        school          用哪个内置档案    "zua"（郑航）                school_key()
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
#
# 🔴 `school` 是 2026-10-12 加进来的（界面上的学校下拉框）。
#    它跟另外 8 个**不是一类** —— 那 8 个是「参数」，它是「选择器」：
#    它自己不是参数值，而是决定去 `SCHOOLS` 里取哪一套参数。
#    ⚠️ 但迁移时它必须跟那 8 个一起被保住，否则用户换过学校、一升级
#       又变回郑航（静默的，界面上还显示着他选的学校名）。
SCHOOL_KEYS = ("school",
               "portalHost", "wlanAcIp", "wlanAcName", "timeoutSec",
               "campusSubnets", "triggerUrl", "checkTargets", "loginExtra")

# 学校 portal 默认参数（拷到同校其他电脑上可直接用）
DEFAULTS = {
    "portalHost": "http://202.196.169.166",
    "wlanAcIp": "10.0.1.10",
    "wlanAcName": "ZZHK-ZAX-BRAS",
    "timeoutSec": 8,
}

# ==================== 内置学校档案 ====================
# 用户只需要在 config.json 里写一行 `"school": "<key>"`，程序就把这套参数铺开，
# 不用手抄一长串。
#
# 优先级（后者覆盖前者）：
#     这里的档案  <  config.json 里的单项覆盖（如 `"portalHost"`）
# 也就是「选了学校之后，仍然可以单独覆盖某一项」—— 高级用户的路没被堵死。
#
# ⚠️ 加一所新学校 = 在这里加一项，**不用改别的代码**。
# 🔴 但每一项都要**实测过**再写进来 —— 尤其 `campusSubnets`：
#    填宽了会把宿舍/家庭的私网也判成校园网（2026-09-19 踩过，见 campus_subnets 的说明）。
# 🔴 `portalType` 决定走哪套认证实现：
#    · "webauth" —— 郑航那套（webauth.do 表单，见 portal_login）
#    · "srun"    —— 深澜 Srun（嘉兴大学，见「深澜 Srun 适配」一节）
SCHOOLS = {
    "zua": {
        "name": "郑州航空工业管理学院",
        "portalType": "webauth",
        "portalHost": "http://202.196.169.166",
        "wlanAcIp": "10.0.1.10",
        "wlanAcName": "ZZHK-ZAX-BRAS",
        "campusSubnets": ["10."],
        "triggerUrl": "http://1.1.1.1/",
    },
    # 嘉兴大学（2026-10-11/12 实网验证：认证打通 + 强制认证 + HTTP 拦截都在）
    # 详见工作区的 `嘉兴大学适配.md`
    "zjxu": {
        "name": "嘉兴大学",
        "portalType": "srun",
        "portalHost": "http://wlan.zjxu.edu.cn",
        # 🔴 必须是 `10.246.`，不能是 `10.` —— 实测网段是 10.246.0.0/16
        "campusSubnets": ["10.246."],
        "triggerUrl": "http://1.1.1.1/",
    },
}

# 不写 `school` 时用哪个。
# 🔴 **必须是 zua** —— 老用户的 config.json 里没有这个键，默认值一变他们的行为
#    就跟着变（红线：郑航那套一个字都不能动）。
DEFAULT_SCHOOL = "zua"

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
# 连通性检测目标：**并发**试，任一通过即算「已联网」。**取值走 check_targets()**。
#
# 为什么不是一个地址就够：只认一个域名时，该域名一旦被校园网拦截或临时不可达，
# 就会把「能上网」误判成「未连接」。实测首次 DNS 解析要 7.4 秒、之后只要 0.2 秒，
# 所以冷启动那一次尤其容易超时。
#   (url, 期望正文；None 表示只看状态码, 简短名字)
#
# 🔴 顺序 = **国内地址优先**（2026-10-10 实测后调）：微软那两个在国内实测
#    **3.4~7.6 秒**才回，而国内这两个只要 **0.05 秒**（差 27 倍，实测数据见
#    `_probe_race` 的说明）。并发之后顺序**不再是速度关键**（总耗时取决于最快
#    的那个），留着这个顺序是为了两层：
#      ① 代码即文档 —— 默认值该反映实测事实；
#      ② 兜底 —— 万一将来并发被回退成串行，这个顺序仍然是对的。
#    ⚠️ 改这个列表时**别依赖下标** —— 测试里已改成按特征挑目标（有正文校验的 /
#       只看状态码的），不再写死 [0] / [2]。
CHECK_TARGETS = (
    ("http://connectivitycheck.platform.hicloud.com/generate_204", None, "hicloud"),
    ("http://connect.rom.miui.com/generate_204", None, "miui"),
    ("http://www.msftconnecttest.com/connecttest.txt", "Microsoft Connect Test", "msftconnecttest"),
    ("http://www.msftncsi.com/ncsi.txt", "Microsoft NCSI", "msftncsi"),
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
                 "campus-login.log", "selftest.txt", "guitest.txt",
                 # 自助服务「上次的登录态」。🔴 **必须在清单里** ——
                 # 只清密码、留着一个还能用的会话，是最糟的那种「以为清干净了」。
                 "self-session.json")

# 「账号中心」的本地流量历史。
#
# 🔴 **故意不放进 `PRIVATE_FILES`** —— 也就是说「清除本机保存的账号密码」
#    **不会**删它。理由：用户要的就是「以后不登录也能看过去的流量」
#    （2026-10-12 明确提的需求），清凭据顺手把历史清掉就等于把这个功能废了。
#    ⚠️ 但**必须让用户知道**：清除确认框里会写明「流量历史会保留」，
#       面板里另有一个「清掉本机历史」的入口（`clear_flow_history`）。
#    ⚠️ 它里面**只有账号 + 每天几个流量数字**，没有密码、没有 cookie ——
#       见 `self_history.py` 的模块文档。
SELF_FLOW_FILE = os.path.join(data_dir(), "self-flow.json")

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


# ==================== 密码怎么存：DPAPI 加密 ====================
#
# 🔴 为什么改（2026-10-15，用户点名要求）：
#     原来密码是**明文**躺在 config.json / accounts.json 里，而数据目录是
#     `C:\ProgramData\CampusLogin` —— **这台机器上任何用户都能读**。
#     自己的电脑还好，但借给别人用一会儿、机房机器、同学的电脑，
#     就等于把校园网密码送出去了；而校园网密码往往和教务 / 邮箱同源，
#     泄露面比「一个校园网」大得多。
#
# 做法：用 **Windows 自带的 DPAPI**（`CryptProtectData` / `CryptUnprotectData`）。
#     加密密钥由 Windows 按**当前用户**保管 —— 我们既不生成也不保存密钥。
#     ⇒ 换个 Windows 用户登录、或把文件拷到另一台机器，**解不开**（这正是要的）。
#     ⇒ 用户**不用**再记一个「主密码」——那才是真会丢的东西。
#
# ⚠️ 三条必须说清楚的边界（别把它当保险箱）：
#   1. **同一个用户下的其它程序仍然解得开** —— DPAPI 是「按用户」不是「按程序」。
#      它挡的是「别的用户 / 文件被拷走」，挡不住「这台机器已经中了木马」。
#      准确说法：把「明文躺在那，记事本一开就看见」升级成「得跑一段代码才能拿到」。
#   2. 额外加了一串**固定熵**当胡椒（`_DPAPI_ENTROPY`）。它在 exe 里是公开的，
#      作用只是让「随手调一下 CryptUnprotectData」的通用工具拿不到东西 ——
#      **它不是密钥**，别指望它。
#   3. 🔴 **开机自启的计划任务是以当前用户身份跑的**（`TASK_XML` 里
#      `LogonType=InteractiveToken` + 同一个 `UserId`，见 `task_create()`），
#      所以守护进程解得开。**哪天要是把任务改成别的身份 / SYSTEM，
#      密码就解不开了** —— 那时必须改成 per-machine
#      （`CRYPTPROTECT_LOCAL_MACHINE`）或换存储方式。改任务 XML 时别忘了这条。
#
# 兼容：老文件里是**明文** ⇒ 读的时候原样可用（没前缀就不动它），
#      下一次写盘自动变成密文。用户什么都不用做。
#      解不开时返回**空串**（= 当没存过，让用户重新输一次），
#      **绝不把密文当密码发出去**。
SECRET_PREFIX = "enc:v1:"

# 固定熵（胡椒，不是密钥 —— 见上面第 2 条）
_DPAPI_ENTROPY = b"CampusLogin/secret/v1"

# 0x1 = CRYPTPROTECT_UI_FORBIDDEN：绝不弹任何界面。
# ⚠️ 这条不能省 —— 这个函数在**后台守护进程**里也会被调到，那里弹窗等于卡死。
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_plaintext_warned = [False]          # 「加密不可用」只警告一次，别刷屏


def is_encrypted(s):
    """这串是不是我们写出去的密文。"""
    return isinstance(s, str) and s.startswith(SECRET_PREFIX)


def dpapi_available():
    """DPAPI 能不能用（加密 + 解密各跑一次往返）。给 `--selftest` 用。"""
    probe = "campus-login-dpapi-probe"
    try:
        blob = _dpapi(probe.encode("utf-8"), True)
        if blob is None:
            return False
        back = _dpapi(blob, False)
        return back is not None and back.decode("utf-8") == probe
    except Exception:
        return False


def _dpapi(data, protect):
    """跑一次 DPAPI。成功返回 bytes，失败返回 None（**任何情况下都不抛**）。

    ⚠️ 为什么必须不抛：这个函数在**开机自启的守护进程**里也会被调到。
       在这里抛出去等于「密码读不出来 ⇒ 整个自动登录挂掉」，而且报错还看不见。
       读不出来就退回「当没存过」，让用户重新输一次 —— 那是可恢复的。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        def _mk(b):
            buf = ctypes.create_string_buffer(b, len(b))
            return (_BLOB(len(b),
                          ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf)

        if not data:
            return None
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # LocalFree 返回的是指针，不声明 restype 会被当成 c_int 截断
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        blob_in, _k1 = _mk(data)
        blob_ent, _k2 = _mk(_DPAPI_ENTROPY)
        blob_out = _BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
    except Exception:
        return None


def encrypt_secret(s):
    """明文 → `enc:v1:<base64>`。空串 / 非字符串原样返回。

    ⚠️ 加密不可用时**退回明文**并记一次日志 —— 宁可少一层保护，
       也不能让用户「密码存不进去、每次开机都要手输」。
       要确认它在不在工作，看 `--selftest` 里那行「密码加密」。
    """
    if not isinstance(s, str) or not s:
        return s
    if is_encrypted(s):
        return s                      # 已经是密文，别二次加密
    blob = _dpapi(s.encode("utf-8"), True)
    if blob is None:
        if not _plaintext_warned[0]:
            _plaintext_warned[0] = True
            log("⚠️ 密码加密不可用（DPAPI 调用失败），本次仍按明文保存")
        return s
    return SECRET_PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_secret(s):
    """`enc:v1:…` → 明文。**没前缀的原样返回**（老文件里的明文照样能用）。

    解不开时返回空串 —— 那表示「当没存过」，界面会提示重新输入一次。
    🔴 绝不把密文原样当密码返回：那串东西会被发到门户服务器上。
    """
    if not isinstance(s, str) or not s:
        return s
    if not is_encrypted(s):
        return s
    try:
        blob = base64.b64decode(s[len(SECRET_PREFIX):], validate=True)
    except Exception:
        log("密码读不出来：密文格式不对，按「没存过」处理")
        return ""
    raw = _dpapi(blob, False)
    if raw is None:
        log("密码读不出来：DPAPI 解不开（换了 Windows 用户 / 换了机器？）"
            "—— 按「没存过」处理，请重新输入一次")
        return ""
    try:
        return raw.decode("utf-8")
    except Exception:
        return ""


def _secrets_walk(raw, fn):
    """把 raw 里**所有**密码字段过一遍 fn。就地改，返回 raw。

    密码存在两处：
      · `config.json`    → 顶层 `passwd`
      · `accounts.json`  → `accounts[<学号>]["passwd"]`
    🔴 **两处都要过** —— 只做一处等于没做，另一处还是明文躺着。
    """
    if not isinstance(raw, dict):
        return raw
    if isinstance(raw.get("passwd"), str):
        raw["passwd"] = fn(raw["passwd"])
    accs = raw.get("accounts")
    if isinstance(accs, dict):
        for a in accs.values():
            if isinstance(a, dict) and isinstance(a.get("passwd"), str):
                a["passwd"] = fn(a["passwd"])
    return raw


def read_secret_json(path):
    """读 JSON 并把密码字段**解密**。文件不在 / 坏了 → None（和 read_json 一致）。"""
    raw = read_json(path)
    return None if raw is None else _secrets_walk(raw, decrypt_secret)


def write_secret_json(path, obj):
    """把密码字段**加密**后写。⚠️ **不改入参** —— 调用方手里那份必须还是明文。

    为什么必须深拷贝：`update_config()` 是「读 → 改 → 写 → **返回 raw**」。
    要是写的时候把 raw 里的密码就地换成密文，返回给调用方的那份就成了密文，
    后面拿它去登录就废了。这种 bug 不报错、测试也可能不红，只表现为
    「明明存对了却登录失败」——所以这里宁可多复制一次。
    """
    write_json(path, _secrets_walk(copy.deepcopy(obj), encrypt_secret))


def secret_storage_text():
    """一句话说清「磁盘上的密码现在是什么状态」。给 `--selftest` 用。

    🔴 只看**有没有 `enc:v1:` 前缀**，**绝不真解密** —— 自检的输出会写进
       `selftest.txt`，而那份文件是要发出去给人看的。把密码解到那里就闹笑话了。

    为什么值得专门报一行：排查「开机自动登录突然说密码是空的」时，
    第一手信息就是这一行 —— 多半是换了 Windows 用户 / 换了机器，DPAPI 解不开。
    """
    if not dpapi_available():
        return "🔴 DPAPI 不可用 —— 密码只能按明文存"
    n = enc = 0
    for raw in (read_json(CONFIG_FILE), read_json(ACCOUNTS_FILE)):
        if not isinstance(raw, dict):
            continue
        vals = [raw.get("passwd")]
        accs = raw.get("accounts")
        if isinstance(accs, dict):
            vals += [a.get("passwd") for a in accs.values()
                     if isinstance(a, dict)]
        for v in vals:
            if isinstance(v, str) and v:
                n += 1
                if is_encrypted(v):
                    enc += 1
    if n == 0:
        return "DPAPI 可用；本机还没存过密码"
    if enc == n:
        return "DPAPI 已加密（%d 条）" % n
    return "⚠️ 还有 %d/%d 条是明文（下次保存会自动加密）" % (n - enc, n)


def should_upgrade_secrets(args):
    """启动时要不要**主动**把明文密码升级成密文。`args` = main() 里那个小写参数列表。

    🔴 自检入口（`--selftest` / `--guitest`）**不动真实数据**：
       这两个恰恰是「**还没装新版**」时最常被跑的 —— `build.py` 的冒烟测试
        就是拿刚打出来的新 exe 跑一次 `--selftest`（见 `main()` 上方那条注释）。
        要是那次就把真实数据升级成密文，用户手上**还在用的旧版 exe**
        会立刻读不懂密码（它会把那串 `enc:v1:…` 当密码发去门户）。
        ⇒ 自检要写文件就写自己的 selftest.txt / guitest.txt，**别碰凭据**。

    ⚠️ 抽成纯函数是为了**能测** —— 写成 `if "--selftest" not in args and ...`
       内联在 main() 里的话，这条判断永远只有靠人读代码才能确认。
    """
    return "--selftest" not in args and "--guitest" not in args


def upgrade_secrets_to_encrypted():
    """把磁盘上残留的**明文**密码主动升级成密文。返回升级了几条（0 = 不用动）。

    🔴 为什么必须有这一步（2026-10-15）：
        光在「写盘时加密」是不够的 —— 老用户升级到 0.6.0 之后，
        只要他**不再点一次「仅保存」**，config.json 里那串明文就会**一直躺着**。
        而用户要的正是「别让密码明文躺在那」。所以得主动升一次。

    ⚠️ 为什么放在**启动时**而不是「写盘时顺手」：写盘只发生在用户主动改东西时，
       而明文残留恰恰是「用户什么都不改」的那种情况 —— 那正是最需要它的时候。

    幂等：全都已经是密文时**一个字节都不写**。否则每次开机都改一次文件，
          既没必要，也会让「文件被动过」这种排查信号失真。
    """
    changed = 0
    for path, kind in ((CONFIG_FILE, "config.json"), (ACCOUNTS_FILE, "accounts.json")):
        # ⚠️ 故意读**原始**的、不解密 —— 要判的是「磁盘上那串是不是明文」。
        raw = read_json(path)
        if not isinstance(raw, dict):
            continue
        plain = []
        if (isinstance(raw.get("passwd"), str) and raw["passwd"]
                and not is_encrypted(raw["passwd"])):
            plain.append("passwd")
        accs = raw.get("accounts")
        if isinstance(accs, dict):
            plain += [uid for uid, a in accs.items()
                      if isinstance(a, dict) and isinstance(a.get("passwd"), str)
                      and a["passwd"] and not is_encrypted(a["passwd"])]
        if not plain:
            continue
        out = _secrets_walk(copy.deepcopy(raw), encrypt_secret)
        if out == raw:
            # 加密没生效（DPAPI 不可用，encrypt_secret 会原样退回）
            # ⇒ **别写盘**，否则每次开机都白改一次文件。
            continue
        write_json(path, out)
        changed += len(plain)
        log("%s：把 %d 处明文密码升级成加密存储" % (kind, len(plain)))
    return changed


# ==================== 「上次的登录态」：存不存、存多久 ====================
#
# 🔴 为什么要有它（2026-10-17，用户点名要的）：
#     自助服务登录一次要读一张**图片验证码**，而登录成功只换来一条会话 cookie
#     （`JSESSIONID-BOSS-0`）。程序一关，cookie 跟着内存没了 ——
#     于是「关掉再打开」必须重新读一次验证码。
#     实测（`_lab/session_ttl.py`）：服务端是**滑动过期**，一直有访问就 45 分钟
#     都不掉。所以「上次那条 cookie 还能不能用」这件事可以**直接问服务端**：
#     能用就接着用，不能用就安静退回验证码流程。
#
# ⚠️ 这确实是「在磁盘上多存一份凭据」，和 `self_service.py` 里
#    「会话 cookie 不落盘」那条**故意**的设计相反。所以四条边界必须守住：
#      · 值一律过 DPAPI（`encrypt_secret`），和密码同一套，**不存明文**
#      · **必须记账号**，加载时比对 —— 串号会让界面显示上一个账号的数据，
#        而两边数字都「看着正常」，用户根本看不出来（这就是账号卡要写清账号的原因）
#      · 带 `savedAt` 上限（`SELF_SESSION_AGE`），超过就丢弃，别无限期留一份登录态
#      · 文件名进 `PRIVATE_FILES` ⇒ 「清除本机保存的账号密码」会连它一起删掉
#
# 🔴 **永远不要把「存过」当成「还能用」**：读出来之后必须真的探一次
#    （`has_session()`），探不通就走验证码。cookie 是会过期的，存档只会过期。

SELF_SESSION_FILE = os.path.join(data_dir(), "self-session.json")
SELF_SESSION_AGE = 24 * 3600          # 存档最多留一天
SELF_SESSION_VERSION = 1


def save_self_session(account, base, pairs, now=None):
    """把会话 cookie **加密**存盘。返回 (ok, 说明)。

    `pairs` 是 `[(名字, 明文值)]`，逐条过 `encrypt_secret()` 再写。
    没有账号 / 没有 cookie ⇒ 不写，返回 (False, 原因)。
    """
    account = (account or "").strip()
    pairs = [(n, v) for n, v in (pairs or []) if n and v]
    if not account or not pairs:
        return False, "没有可保存的登录态"
    stamp = int(time.time() if now is None else now)
    obj = {
        "v": SELF_SESSION_VERSION,
        "account": account,
        "base": (base or "").rstrip("/"),
        "savedAt": stamp,
        "cookies": [[n, encrypt_secret(v)] for n, v in pairs],
    }
    try:
        write_json(SELF_SESSION_FILE, obj)
    except Exception as e:
        return False, "写不进去（%s）" % e
    return True, ""


def read_self_session(account, base, now=None):
    """读出上次存的会话（值已解密）。返回 `(pairs, 说明)`。

    🔴 说明是**给人看的原因**，不是错误码；调用方只在「不是没存过」时记日志。
    🔴 任何一项对不上就**整份丢掉**，绝不半份复用 ——
       半个 cookie 集合等于一个坏会话，而它会在后面以「莫名其妙的 -1」出现。
    """
    account = (account or "").strip()
    base = (base or "").rstrip("/")
    raw = read_json(SELF_SESSION_FILE)
    if not isinstance(raw, dict):
        return [], "没存过"
    if raw.get("v") != SELF_SESSION_VERSION:
        return [], "存档格式不认（v=%r）" % (raw.get("v"),)
    if (raw.get("account") or "") != account:
        # 🔴 这一条是**防串号**的：cookie 是按账号的，拿错账号的会话去查，
        #    服务端会返回另一个人的数据，而界面照样「看着正常」。
        return [], "存档是另一个账号的（%s）" % (raw.get("account") or "?",)
    if (raw.get("base") or "") != base:
        return [], "门户地址变了"
    try:
        saved = int(raw.get("savedAt") or 0)
    except Exception:
        saved = 0
    stamp = time.time() if now is None else now
    if saved <= 0:
        return [], "存档里没有保存时间"
    if stamp - saved > SELF_SESSION_AGE:
        return [], "存档超过 %d 小时了" % (SELF_SESSION_AGE // 3600)
    if stamp - saved < -300:
        # 系统时间被往回调过。宁可不用，也别拿一个「未来」的存档当有效。
        return [], "存档时间是未来（系统时间被改过？）"
    pairs = []
    for item in (raw.get("cookies") or []):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return [], "存档格式不对"
        name, enc = item
        value = decrypt_secret(enc) if isinstance(enc, str) else ""
        if not name or not value:
            # 解不开（换了 Windows 用户 / 换了机器）⇒ 整份作废
            return [], "存档解不开（换了 Windows 用户或机器？）"
        pairs.append((name, value))
    if not pairs:
        return [], "存档里没有 cookie"
    return pairs, ""


def clear_self_session(account=None):
    """删掉磁盘上的登录态。返回是否真的删掉了一个文件。

    `account` 给了就**只删这个账号的**：存档是「上一次谁登录过就是谁」，
    当前界面看的是另一个账号时，凭什么把别人的登录态顺手删掉 ——
    （严格说删了只是让对方下次多读一次验证码，但「不该动的东西被动了」
      正是这类地方积累出来的。）

    ⚠️ 文件不在也算「没什么可删」（幂等）。
    """
    if not os.path.isfile(SELF_SESSION_FILE):
        return False
    raw = read_json(SELF_SESSION_FILE)
    if account:
        mine = (account or "").strip()
        # 读不出结构 / 读到的是别人的 ⇒ 不动它。
        # ⚠️ 读不出来也**不删**：那可能是暂时的（文件正被写），
        #    下次真要用它的时候 `read_self_session()` 会自己判定作废。
        if not isinstance(raw, dict) or (raw.get("account") or "") != mine:
            return False
    try:
        os.remove(SELF_SESSION_FILE)
        return True
    except Exception as e:
        log("清掉登录态失败：%s" % e)
    return False


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
def school_profile(key):
    """按 key 取内置学校档案。认不出来就返回空 dict（于是走 DEFAULTS）。

    ⚠️ 认不出来时**故意**退回空 dict 而不是报错 —— 用户把 key 写错了，
       行为应当回到「郑航默认」，而不是让程序起不来。
    """
    if not isinstance(key, str) or not key.strip():
        key = DEFAULT_SCHOOL
    return dict(SCHOOLS.get(key.strip().lower(), {}))


def _fossil_keys(raw):
    """config.json 里那些「长得像老版本自动写进去的」键。

    🔴 为什么需要它（2026-10-12 实测复现，见 `_repro_school_switch.py`）：
        老版本的 `on_save` / `on_switch` / `set_active_account` 都是
        `save_config(load_config())`，而 `load_config()` 返回的是
        「DEFAULTS + 用户写的那份」**合并后的整份** —— 于是 DEFAULTS 里那 4 个键
        （portalHost / wlanAcIp / wlanAcName / timeoutSec）被**固化**进了文件。
        用户从没写过它们，但文件里从此有了。（本机真实 config.json 实测确认。）

        而学校档案的优先级**低于**「config.json 里的键」，于是这 4 个化石
        永远盖住档案 —— 表现是「界面选了嘉兴，登录还打到郑航」，一声不吭。

    判据：**键在 DEFAULTS 里，且值和 DEFAULTS 一模一样**。
        ⚠️ 这个判据是「不会误伤」的：值与默认值相同的键删掉之后，
           `load_config()` 回落到的还是同一个值 —— 对**当前**这 4 个键而言
           删与不删行为完全一致（`SCHOOLS["zua"]` 的同名值和 DEFAULTS 相同）。
           而用户真改过的（比如把 timeoutSec 写成 20）值不等于 DEFAULTS，
           不会被误删 —— 这条由 `_repro_school_switch.py` 第 3 节钉着。
    """
    return [k for k, v in DEFAULTS.items() if k in raw and raw[k] == v]


def load_config():
    cfg = read_secret_json(CONFIG_FILE)
    if not isinstance(cfg, dict):
        cfg = {}
    out = dict(DEFAULTS)
    # 学校档案铺在 DEFAULTS **之上**：选了嘉兴时它会覆盖 portalHost / campusSubnets 等。
    # ⚠️ 顺序不能反 —— 反了 DEFAULTS 里的郑航地址会把嘉兴的盖掉。
    out.update(school_profile(cfg.get("school")))
    out["userId"] = ""
    out["passwd"] = ""
    # 化石键**不参与覆盖**：它们不是用户的选择，只是老版本顺手写下的痕迹，
    # 留着就会把学校档案盖住（见 _fossil_keys）。
    skip = set(_fossil_keys(cfg))
    out.update({k: v for k, v in cfg.items() if v is not None and k not in skip})
    return out


def save_config(cfg):
    """整份覆盖。⚠️ 除了迁移和测试，**业务代码不要用这个** —— 用 `update_config()`。

    原因见 `update_config` 的说明：整份写回会把内置默认值固化成用户配置。
    """
    write_secret_json(CONFIG_FILE, cfg)


def update_config(patch):
    """**只改 `patch` 里点名的键**，文件里其它键原样保留。

    🔴 为什么不能写成 `cfg = load_config(); cfg[k] = v; save_config(cfg)`
       （2026-10-12 实测复现）：`load_config()` 返回的是合并后的**整份**，
       把它写回去等于把**内置默认值固化成了用户配置** —— 从此换学校换不过去。
       本机真实 config.json 里就躺着老版本写下的 4 个这样的键。

    `patch` 里值为 None 的键 = **删除**该键。
    ⚠️ 只写「文件里本来就有的键 + patch 里点名的键」，绝不注入档案值。
    """
    raw = read_secret_json(CONFIG_FILE)
    if not isinstance(raw, dict):
        raw = {}
    for k, v in patch.items():
        if v is None:
            raw.pop(k, None)
        else:
            raw[k] = v
    write_secret_json(CONFIG_FILE, raw)
    return raw


def set_school(key):
    """切换学校：写 `school` 键，并清掉老版本留下的化石键。

    返回 (生效的学校 key, 被清掉的化石键列表)。

    ⚠️ 为什么「清化石」这一步必须做、而且必须在这里做：
        `load_config()` 里那道过滤（`_fossil_keys`）已经能让化石键不生效了，
        但**文件里那份谎话还在** —— 用户拿记事本打开 config.json 会看到
        一个他从没写过的 `portalHost`，而它跟界面上选的学校是矛盾的。
        所以这里把它物理删掉，让文件本身也是诚实的。
        （两道防线：读的时候不认它，写的时候顺手清掉。测试两条都钉了。）
    """
    k = key.strip().lower() if isinstance(key, str) else ""
    if k not in SCHOOLS:
        k = DEFAULT_SCHOOL
    raw = read_secret_json(CONFIG_FILE)
    if not isinstance(raw, dict):
        raw = {}
    dropped = _fossil_keys(raw)
    patch = {f: None for f in dropped}
    patch["school"] = k
    update_config(patch)
    if dropped:
        log("切换学校 → %s：清掉了老版本固化在 config.json 里的 %d 个键（%s）"
            % (k, len(dropped), "、".join(dropped)))
    return k, dropped


# ---- 学校参数的取值函数（默认值在文件顶部「学校参数」一节，可被 config.json 覆盖）----
# 都收一个可选的已读好的 cfg，免得在一次登录里把同一个文件读三遍。
def _cfg_or_load(cfg):
    return cfg if isinstance(cfg, dict) else load_config()


def school_key(cfg=None):
    """当前生效的学校 key。

    ⚠️ 认不出来时**故意**退回 `DEFAULT_SCHOOL` 而不是报错 —— 用户把 key 拼错了，
       行为应当回到「郑航默认」，而不是让程序起不来。
       （和 `school_profile()` 的兜底是同一条原则。）
    """
    v = _cfg_or_load(cfg).get("school")
    if not isinstance(v, str) or not v.strip():
        return DEFAULT_SCHOOL
    v = v.strip().lower()
    return v if v in SCHOOLS else DEFAULT_SCHOOL


def portal_type(cfg=None):
    """当前该走哪套认证实现：'webauth'（郑航） / 'srun'（深澜，嘉兴大学）。

    🔴 抽出来是因为它原来在 `portal_login` / `check_password` / `portal_logout`
       里**各抄了一遍** —— 三份判断迟早会漂移，而漂移的表现是
       「登录走 srun、登出却走 webauth」这种只在特定学校才炸的错。
    """
    v = _cfg_or_load(cfg).get("portalType")
    if isinstance(v, str) and v.strip():
        return v.strip().lower()
    return "webauth"


def school_choices():
    """界面上学校下拉框的选项：`[(key, 中文名), …]`，顺序固定（按 SCHOOLS 的定义序）。

    ⚠️ 顺序**不能**用 `sorted()`：字典序是 zjxu / zua，
       而用户最可能选的是列表第一个（郑航）—— 排序会把它挤到后面。
    """
    return [(k, (p.get("name") or k)) for k, p in SCHOOLS.items()]


def school_name(key=None):
    """学校的中文名（界面显示用）。`key=None` = 当前生效的那所。"""
    k = school_key() if key is None else key
    return (SCHOOLS.get(k) or {}).get("name") or (k or "未知学校")


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
        s = read_secret_json(ACCOUNTS_FILE)
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
        write_secret_json(ACCOUNTS_FILE, store)
    finally:
        if got:
            _ACCOUNTS_LOCK.release()


def upsert_account(uid, pwd, overwrite, school=None):
    """记录一个账号。

    overwrite=False 时**不覆盖**已存的密码 —— 切换账号时必须这样，
    否则回滚目标的密码就被新密码冲掉了，保险会失效。

    🔴 `school`（2026-10-12 加）：这个账号是在**哪所学校**下保存的。
       为什么必须记（不是锦上添花）：
           账号库是**两所学校共用**的（同一个 accounts.json）。
           用户在郑航存了自己的账号、又切到嘉兴帮同学测，
           之后只要在嘉兴这所学校下点一次「切换到此账号」，
           **他的郑航密码就会被发到嘉兴大学的门户服务器上** ——
           那是把密码交给第三方。所以每个账号都要带上它属于哪所学校，
           界面上看得见、切换前会问一句（见 render_accounts / on_switch）。

       `school=None` = **不改动**已有的标记（老版本存下的账号没有这个键，
       这时按「跟当前学校一致」处理，不打扰用户）。
    """
    def _do(store):
        a = store["accounts"].get(uid)
        if a is None:
            a = {"passwd": pwd, "lastSuccess": "", "lastAttempt": "",
                 "lastResult": ""}
            store["accounts"][uid] = a
        elif overwrite or not a.get("passwd"):
            a["passwd"] = pwd
        # ⚠️ 只在明确传了学校时才写 —— 别用 school_key() 兜底，
        #    否则守护/自动登录那条路会把老账号的标记悄悄改成当前学校。
        if school:
            a["school"] = school

    mutate_accounts(_do)


def account_school(uid):
    """这个账号是在哪所学校下保存的。没标记（老数据）返回空串。"""
    a = (load_accounts().get("accounts") or {}).get(uid) or {}
    v = a.get("school")
    return v if isinstance(v, str) else ""


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
    """把「当前账号」记进 config.json。

    ⚠️ 走 `update_config` 而不是 `save_config(load_config())` ——
       后者会把内置默认值固化成用户配置，见 `update_config` 的说明。
    """
    update_config({"userId": uid, "passwd": pwd})


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
        old_cfg = read_secret_json(os.path.join(src, "config.json"))
        if not isinstance(old_cfg, dict):
            continue
        # ⚠️ 这里必须遍历 **SCHOOL_KEYS**，不能只遍历 DEFAULTS。
        #    DEFAULTS 只有 4 个键（portalHost / wlanAcIp / wlanAcName / timeoutSec），
        #    而换学校的用户会在 config.json 里额外写 campusSubnets / triggerUrl /
        #    checkTargets / loginExtra —— 只遍历 DEFAULTS 的话这 4 个键**静默丢失**，
        #    用户看到的是「配置明明写了却不生效」（2026-10-05 用
        #    _repro_migrate_school_keys.py 实测复现，退出码 1）。
        #    回归钉在 school_profile_test.py 第 6 节。
        #
        # 🔴 2026-10-12：**不再拿 DEFAULTS 打底**（原来是 `cfg = dict(DEFAULTS)`）。
        #    打底等于把内置默认值写成用户配置 —— 迁移过来的 config.json 会凭空多出
        #    4 个化石键，从此换学校换不过去（见 `_fossil_keys` / `set_school`）。
        #    只搬「旧文件里真的写了的东西」，缺的键 `load_config()` 自己会兜底。
        cfg = {}
        cfg["userId"] = old_cfg.get("userId", "") or ""
        cfg["passwd"] = old_cfg.get("passwd", "") or ""
        for k in SCHOOL_KEYS:
            if old_cfg.get(k):
                cfg[k] = old_cfg[k]
        save_config(cfg)
        old_acc = read_secret_json(os.path.join(src, "accounts.json"))
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


def _probe_race(targets, per):
    """**并发**探所有目标，谁先给出结论就用谁。返回 (verdict, name)。

    verdict 的三态与 `_probe` 一致：True / False / None（**False 优先于 None**）。
    name 是给出结论的那个目标名，只用于排障，不参与判定。

    🔴 为什么改成并发（2026-10-10，用户报「开机后自动登录慢」）：
        原来是一个个**串着**探、第一个成功的就返回 —— 于是**总耗时取决于「最靠前
        的那个能通的目标有多快」**。而 `CHECK_TARGETS` 原先前两个是微软的地址，
        在国内实测 3.4~7.6 秒才回（国内地址只要 0.05 秒）。
        实测对比（同一分钟、同一台机器）：
            微软优先  3.47 秒   ← 改之前
            国内优先  0.13 秒   ← 改之后
        并发之后**总耗时取决于「最快的那个目标有多快」**，与顺序无关；
        最坏情况也从「N × per」（4 个目标 = 10 秒）收成「per」（约 2.5 秒）。
        ⚠️ 顺带解决了可扩展性：以后往列表里加目标**不会让它变慢**。

    ⚠️ 和 `updater.probe_sources()` 的区别（同一个理由，但**不能照抄**）：
        那边要的是**每个源各自的速度**，所以必须 `join` 等全部跑完；
        这边只要**一个「能通」的证据**，所以拿到第一个 True 就立刻返回 ——
        等剩下几个跑完，等于把慢目标的时间又加回来了，白并发。
        线程都是 daemon + 各自带超时，不会把进程吊住。

    ⚠️ 用 `queue` 而不是 `concurrent.futures`：后者会引入额外模块，而打包体积是
        项目的红线（见 build.py 的 EXCLUDES）；`threading` / `queue` 本来就 import 了。
    """
    q = queue.Queue()

    def one(url, expect, name):
        q.put((name, _probe(url, expect, per)))

    for t in targets:
        threading.Thread(target=one, args=t, daemon=True).start()

    # 总期限：最坏情况 = 单个目标的超时 + 一点余量。
    # 不这么兜底的话，DNS 解析一旦卡住（它**不受 socket timeout 约束**）就会在这里
    # 无限等下去 —— 那正是「开机后慢」的另一个可能来源，别再引入。
    deadline = time.time() + per + 0.5
    blocked = None
    for _ in targets:
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            name, r = q.get(timeout=remain)
        except queue.Empty:
            break
        if r is True:
            return True, name
        if r is False and blocked is None:
            blocked = name
    return (False, blocked) if blocked is not None else (None, "")


def test_internet(timeout=None, rounds=1):
    """检测「当前是否已能上网」，返回三态：

        True  —— 已联网
        False —— 被门户拦截（拿到响应但内容不对，即确实未认证）
        None  —— 判定不了（请求异常 / 超时 / 状态码异常）

    为什么要三态：调用方对「确定没认证」和「没测出来」的处理必须不同。把后者
    当成「没认证」，轻则状态显示错误，重则误触发登录、或在切换账号时跳过下线
    步骤（见 do_switch），所以宁可不给结论也不要给错结论。

    ⚠️ 目标之间是**并发**探的（2026-10-10 起，见 `_probe_race`）—— 顺序不再影响
       耗时，往列表里加目标也不会变慢。三态语义与函数签名都没变，调用方无需改动。
    """
    cfg = load_config()
    t = timeout or int(cfg.get("timeoutSec") or 8)
    # 每个目标的超时上限。并发下**总耗时 ≈ per**（不再是「N × per」），
    # 所以这个值现在只决定「最坏情况等多久」，不决定「加几个目标就慢几倍」。
    # 下限 2.5 秒是为了扛住首次 DNS 解析慢（实测冷启动 7.4 秒、之后 0.2 秒）。
    per = min(5.0, max(2.5, t * 0.5))
    saw_blocked = False
    for i in range(max(1, rounds)):
        if i:
            time.sleep(0.4)
        verdict, _name = _probe_race(check_targets(cfg), per)
        if verdict is True:
            return True
        if verdict is False:
            saw_blocked = True
    # 「明确被拦截」的证据优先于「测不出来」：门户劫持整片流量时，有正文校验的
    # 目标会返回 200 + 认证页（判 False），而 generate_204 这类目标拿到的状态码
    # 不是 204（判 None）。要是让 None 盖过 False，就会把「确实没认证」误报成
    # 「测不出来」，进而让 do_auto 不去登录、用户上不了网。
    return False if saw_blocked else None


# ------------------------------------------------------------------ 测速
#
# 「测一下速度」量的是**用户到发布站**的下载速度 —— 也就是「我这台机器能不能
# 顺畅下到这个软件」。不是「宽带标称带宽」，也不打算去调中科大测速网：
# 它的「隐私和服务说明」原文写着「异常程序访问」的 IP 会被临时封禁，
# 线上页面还加了 PoW（浏览器先算 0.5～2 秒 SHA-256 才让测速，中科大内网免检）
# —— 那是明确针对脚本的拒绝。完整取证见 docs/DEVELOPMENT.md「测速」一节。

SPEEDTEST_FILE = "speedtest.bin"

# ⚠️ 地址**从 updater 派生**，不写字面量。
#    `domain_drift_test.py` 会扫 campus_login.py 里出现的域名并要求和别处一致，
#    而「三处必须同源」那条红线的前提是**域名只在一个地方写**。这里再写一遍，
#    就等于又造了一处「换域名时会漏改」的地方 —— 而它漏改的症状是测速一直失败，
#    和「网络不好」长得一模一样，极难查。
SPEEDTEST_URL = updater.BASE_URL_HINT + "/" + SPEEDTEST_FILE

SPEED_MIN_SECONDS = 2.5     # 至少量够这么久才给结论（少了噪声大）
SPEED_MAX_ROUNDS = 4        # 最多下几轮。1 MiB × 4 = 4 MiB 上限 ——
                            # 用户点一次「测一下速度」的流量天花板就靠这个兜住
SPEED_TIMEOUT = 20          # 单轮超时。校园网慢起来 1 MiB 要 6 秒多，
                            # 20 秒够扛住抖动，又不至于让用户干等


def _speed_err(ex):
    """把测速路上可能抛的异常翻成一句给用户看的中文。

    ⚠️ 这里**故意不 import ssl**：本文件从头到尾不 import 它，理由见上面
       「网络」一节的说明 —— PyInstaller 一旦看到 ssl，就会把
       libcrypto-3.dll + libssl-3.dll（解压后约 5.8MB）一起打进来。
       所以证书错误靠 `URLError.reason` 的文字来认：urllib 会把
       `ssl.SSLError`（它是 OSError 的子类）包成 `URLError` 抛出来。
    """
    if isinstance(ex, urllib.error.HTTPError):
        return "服务器返回 HTTP %s" % ex.code
    if isinstance(ex, urllib.error.URLError):
        reason = str(ex.reason)
        up = reason.upper()
        if "SSL" in up or "CERTIFICATE" in up:
            return "证书校验失败：%s" % reason
        return "连不上发布站：%s" % reason
    if isinstance(ex, socket.timeout):
        return "超时（%s 秒内没下完一轮）" % SPEED_TIMEOUT
    return "%s: %s" % (type(ex).__name__, ex)


def speed_probe(url=None, min_seconds=SPEED_MIN_SECONDS, max_rounds=SPEED_MAX_ROUNDS,
                timeout=SPEED_TIMEOUT, opener=None, clock=None):
    """下载发布站上的测速文件，量出下载速度。返回 (ok, info)。

    ok=True 时 info 是 dict：
        mbps         兆比特每秒（用户最认这个单位）
        mbytes_per_s 兆字节每秒
        bytes        计速用的总字节数（**不含**第 1 轮）
        seconds      计速用的总耗时
        rounds       计速的轮数
        connect      第 1 轮的连接耗时（DNS + TCP + TLS + 响应头到达）
    ok=False 时 info 是一句给用户看的中文原因。

    🔴 为什么第 1 轮**不计入速度**：
       第 1 轮里包含 DNS 解析 + TCP + TLS 握手，而这些**只发生一次**。
       把它摊进平均值，量出来的数会同时受「握手多慢」和「带宽多大」影响，
       而且偏差方向不固定（慢网被拉低得多、快网被拉低得少）——
       换句话说，那个数**没法解释**。所以第 1 轮单独用来报「连接耗时」，
       速度只从第 2 轮起算。
       ⚠️ 代价是**至少要下两轮**。只下到一轮时宁可报「测不出来」，
          也不给一个混着握手时间的假速度。

    🔴 每轮都带一个**唯一的时间戳参数**：
       测速文件在站点上配的是 `Cache-Control: no-store`，但那是我们这边的承诺；
       客户端自己也带一个变化的 query，等于给「某一层偷偷缓存了」上了第二道保险。
       缓存对测速是**致命**的 —— 命中一次缓存，那一轮走的是内存，
       速度凭空翻几倍，而客户端完全看不出来（它只看到「很快」）。

    `opener` / `clock` 只为测试而留（回环服务 + 假时钟），正常调用不传。
    """
    url = url or SPEEDTEST_URL
    clock = clock or time.monotonic
    if opener is None:
        opener = urllib.request.build_opener(*_direct_handlers())

    def one_round(idx):
        """下一轮。返回 (字节数, 连接耗时, 总耗时)。异常原样往上抛。"""
        sep = "&" if "?" in url else "?"
        u = "%s%st=%d&r=%d" % (url, sep, int(time.time() * 1000), idx)
        req = urllib.request.Request(u, headers={
            "User-Agent": UA,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })
        t0 = clock()
        fp = opener.open(req, timeout=timeout)
        try:
            connect = clock() - t0          # 到这一步响应头已经回来了
            got = 0
            while True:
                chunk = fp.read(64 * 1024)
                if not chunk:
                    break
                got += len(chunk)
            return got, connect, clock() - t0
        finally:
            try:
                fp.close()
            except Exception:
                pass

    # ---- 第 1 轮：只用来量连接耗时 ----
    try:
        b0, connect, _e0 = one_round(0)
    except Exception as ex:
        return False, _speed_err(ex)
    # 200 但一个字节都没有：多半是中间层塞了个空响应（或者 URL 被改写成了别的
    # 东西）。第 1 轮就发现的事没必要再下第 2 轮 —— 而且这里不拦的话，
    # 要等到第 2 轮才由下面那个 `got <= 0` 兜住，白跑一次请求。
    if b0 <= 0:
        return False, "服务器返回了空内容"

    # ---- 第 2 轮起：计速 ----
    total_bytes = 0
    total_secs = 0.0
    counted = 0
    while counted < max_rounds - 1 and total_secs < min_seconds:
        try:
            got, _c, elapsed = one_round(counted + 1)
        except Exception as ex:
            # 后面几轮出错：已经量到数据的就用已量到的（结果照给，不假装失败）；
            # 一轮都没量到才报错 —— 否则一次网络抖动就让整个功能看起来是坏的。
            if counted:
                break
            return False, _speed_err(ex)
        if got <= 0:
            # 200 但一个字节都没读到：多半是中间层塞了个空响应。
            if counted:
                break
            return False, "服务器返回了空内容"
        total_bytes += got
        total_secs += elapsed
        counted += 1

    if not counted or total_bytes <= 0 or total_secs <= 0:
        return False, "没量到有效数据"

    bps = total_bytes / total_secs
    return True, {
        "mbps": bps * 8.0 / 1e6,
        "mbytes_per_s": bps / (1024.0 * 1024.0),
        "bytes": total_bytes,
        "seconds": total_secs,
        "rounds": counted,
        "connect": connect,
    }


def speed_describe(info):
    """把 speed_probe 的成功结果拼成给用户看的多行文案。"""
    mbps = info["mbps"]
    if mbps >= 100:
        shown = "%.0f Mbps" % mbps
    elif mbps >= 10:
        shown = "%.1f Mbps" % mbps
    else:
        shown = "%.2f Mbps" % mbps
    lines = [
        "下载速度 约 %s（%.2f MB/s）" % (shown, info["mbytes_per_s"]),
        "连接耗时 %.2f 秒 · 量了 %.1f MB / %d 轮"
        % (info["connect"], info["bytes"] / 1048576.0, info["rounds"]),
    ]
    # 量得太快说明这次采样点太少，如实说 —— 比给一个看着很精确的假数字好。
    if info["seconds"] < 1.0:
        lines.append("（这次量得太快，数字仅供参考）")
    return "\n".join(lines)


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
    NET_OFF_CAMPUS: MUTED,
    NET_OK: OK,
    NET_NEED_LOGIN: DANGER,
    NET_UNKNOWN: MUTED,
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
    """借 UDP connect 拿到本机用于访问 portal 的出口 IP（不发包）。

    🔴 为什么要有**字面量 IP 兜底**（2026-10-12，嘉兴实机测出来的）：
        这里只借 `connect()` 让内核**选一次路由**，并**不真的发包** ——
        所以目标地址是不是 portal 根本不重要，重要的是它**必须能被解析出来**。
        而 `portalHost` 是**域名**时（郑航是纯 IP `202.196.169.166`，嘉兴是
        `wlan.zjxu.edu.cn`），这一句就变成了一个**隐式的 DNS 依赖**。
        实测后果：嘉兴换 MAC 进未认证状态后，守护**连续 4 分钟什么都没做**
        —— 因为 `on_campus_network()` 判不出来（它依赖本函数**和**
        `portal_reachable()`，两个都要解析域名），`guard_decide()` 于是返回
        「什么都不做」，而且**不写任何日志**。
        ⇒ 加一组**字面量 IP** 兜底：解析不了域名时，用固定 IP 照样能选到路由、
          拿到出口 IP。这**不改变**正常情况下的行为（域名永远先试）。
    """
    host = urlparse(load_config().get("portalHost") or DEFAULTS["portalHost"]).hostname
    # 域名先试（保持原行为：拿到的是「朝 portal 那个方向的」出口 IP）；
    # 解析不了就退到字面量 IP —— 它们唯一的用途就是让内核挑一条路由。
    targets = ([host] if host else []) + ["223.5.5.5", "1.1.1.1"]
    for t in targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((t, 80))
            return s.getsockname()[0]
        except Exception:
            continue
        finally:
            s.close()
    return None


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
    # 🔴 超时了：把「为什么判成不在校园网」写进日志（2026-10-12，嘉兴实机测出来的）。
    #    以前这里只 `return False`，调用方打一句「未检测到校园网，请确认已连接校园网
    #    Wi-Fi」—— 而这句话在「Wi-Fi 明明连着、只是没认证」时是**误导**，
    #    日志里也没有任何线索能查，只能靠猜。
    ip = local_ipv4()
    log("等校园网超时（%d 秒）：本机IP=%s，配置的校园网段=%s。"
        "判据是「IP 落在网段」或「门户主机可达」，两条都没满足 ⇒ 判成不在校园网。"
        % (max_seconds, ip or "取不到", ",".join(campus_subnets())))
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


# ==================== 深澜 Srun 适配（嘉兴大学）====================
# 算法**逐行对照门户自己的 JS** 实现（`Portal.js` + `all.min.js` 里的
# jquery-base64 v1.0 / md5 v2.10.0 / js-sha1 v0.6.0），原始 JS 见工作区的
# `嘉兴大学-Srun算法采集.md`；并做过**逐字节对拍**（`_lab/srun_algo_check.py`：
# 4 组用例的 JSON 载荷 / xEncode / base64 / info 全部一致）。
#
# 流程（2026-10-11 实网验证成功，`suc_msg = login_ok`）：
#   1. GET /cgi-bin/get_challenge  → 一次性 token（只校验 IP，不校验账号）
#   2. hmd5 = HMAC-MD5(原始密码, key=token)
#   3. info = '{SRBX1}' + srun_b64(srun_xencode(JSON, token))
#   4. chksum = sha1(token+uid+token+hmd5+token+acid+token+ip+token+n+token+type+token+info)
#   5. GET /cgi-bin/srun_portal?action=login&…
#
# ⚠️ 判结果看 `suc_msg`，**不是 `ecode`** —— 「已在线」时 ecode 也是 0（实网踩过）。
# ⚠️ 该校开了 **macauth**：设备被 AC 按 MAC 自动放行时 `domain == "macauth"`，
#    那属于**正常在线**，别误判成「需要登录」（见 srun_status）。
# ⚠️ 纯标准库；请求走项目的 `Session`（它已经禁用系统代理 —— 校园网内网正需要这个）。
SRUN_ALPHA = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
_SRUN_MASK = 0xFFFFFFFF
SRUN_AC_ID = 1
SRUN_N = 200
SRUN_TYPE = 1
SRUN_ENC = "srun_bx1"
SRUN_LOGIN_OK = "login_ok"
SRUN_ALREADY_ONLINE = "ip_already_online_error"


def _srun_str2long(a, w):
    """门户里的 `s(a, b)`。按 UTF-16 码元取码（等价于 JS 的 charCodeAt）。"""
    raw = a.encode("utf-16-le")
    units = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
    v = []
    for i in range(0, len(units), 4):
        val = 0
        for j, u in enumerate(units[i:i + 4]):
            val |= u << (8 * j)
        v.append(val & _SRUN_MASK)
    if w:
        v.append(len(units))
    return v


def _srun_long2str(a, w):
    """门户里的 `l(a, b)`。本流程只用 `w=False`。"""
    d = len(a)
    out = []
    for x in a:
        out.append(chr(x & 0xFF))
        out.append(chr((x >> 8) & 0xFF))
        out.append(chr((x >> 16) & 0xFF))
        out.append(chr((x >> 24) & 0xFF))
    s = "".join(out)
    if not w:
        return s
    c = (d - 1) << 2
    m = a[d - 1]
    if m < c - 3 or m > c:
        return None
    return s[:m]


def srun_xencode(msg, key):
    """门户里的 `encode(str, key)` —— XXTEA 变体。

    ⚠️ JS 的 `>>>` / `<<` / `^` 都把操作数转成 32 位，Python 不会 ⇒
       每处位运算后补 `& 0xFFFFFFFF`（低 32 位相同，是**等价**不是近似）。
       但中间变量 `m` **反而不能截断** —— JS 那里是浮点加法不溢出，
       而 `(v[p] + m) & MASK` 只取决于低 32 位。
    """
    if msg == "":
        return ""
    v = _srun_str2long(msg, True)
    k = _srun_str2long(key, False)
    if len(k) < 4:
        k = k + [0] * (4 - len(k))
    n = len(v) - 1
    z = v[n]
    y = v[0]
    c = 0x86014019 | 0x183639A0          # = 0x9E3779B9（XXTEA 的 delta）
    d = 0
    q = int(6 + 52 / (n + 1))
    while q > 0:
        q -= 1
        d = (d + c) & _SRUN_MASK
        e = (d >> 2) & 3
        p = 0
        while p < n:
            y = v[p + 1]
            m = (z >> 5) ^ (y << 2)
            m += (y >> 3) ^ (z << 4) ^ (d ^ y)
            m += k[(p & 3) ^ e] ^ z
            v[p] = (v[p] + m) & _SRUN_MASK
            z = v[p]
            p += 1
        y = v[0]
        m = (z >> 5) ^ (y << 2)
        m += (y >> 3) ^ (z << 4) ^ (d ^ y)
        m += k[(p & 3) ^ e] ^ z
        v[n] = (v[n] + m) & _SRUN_MASK
        z = v[n]
    return _srun_long2str(v, False)


def srun_b64(s):
    """深澜自定义 base64（字符表与标准不同）。输入每字符码必须 ≤ 255。"""
    data = [ord(ch) for ch in s]
    n = len(data)
    if n == 0:
        return ""
    imax = n - n % 3
    out = []
    for i in range(0, imax, 3):
        b10 = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
        out.append(SRUN_ALPHA[(b10 >> 18) & 63])
        out.append(SRUN_ALPHA[(b10 >> 12) & 63])
        out.append(SRUN_ALPHA[(b10 >> 6) & 63])
        out.append(SRUN_ALPHA[b10 & 63])
    rem = n - imax
    if rem == 1:
        b10 = data[imax] << 16
        out.append(SRUN_ALPHA[(b10 >> 18) & 63])
        out.append(SRUN_ALPHA[(b10 >> 12) & 63])
        out.append("=")
        out.append("=")
    elif rem == 2:
        b10 = (data[imax] << 16) | (data[imax + 1] << 8)
        out.append(SRUN_ALPHA[(b10 >> 18) & 63])
        out.append(SRUN_ALPHA[(b10 >> 12) & 63])
        out.append(SRUN_ALPHA[(b10 >> 6) & 63])
        out.append("=")
    return "".join(out)


def _srun_jsonp(text):
    """接口是 JSONP（`x({...})`），剥壳。"""
    i = text.find("(")
    j = text.rfind(")")
    if i >= 0 and j > i:
        return json.loads(text[i + 1:j])
    return json.loads(text)


def _srun_base(cfg=None):
    return (_cfg_or_load(cfg).get("portalHost") or "").rstrip("/")


def srun_status(cfg=None):
    """查在线状态。不在线时 `res == "not_online_error"`。

    ⚠️ `domain == "macauth"` 表示设备已被 AC 按 MAC **自动放行** ——
       那属于正常在线，**别当成「需要登录」**（2026-10-12 实测）。
    """
    url = "%s/cgi-bin/rad_user_info?%s" % (
        _srun_base(cfg),
        urlencode({"callback": "x", "ip": local_ipv4() or ""}))
    return _srun_jsonp(http_get(url, timeout=10, headers={"User-Agent": UA}).text)


def srun_login(uid, pwd, cfg=None):
    """深澜 Srun 认证。成功返回 True。

    与 webauth 那套最大的差别：**不需要**先 GET 触发地址、也不用从重定向里
    解析参数 —— 直接问门户要 token 再提交。
    """
    cfg = _cfg_or_load(cfg)
    base = _srun_base(cfg)
    if not base:
        log("Srun: config 里没有 portalHost，没法认证")
        return False

    ip = local_ipv4()
    if not ip:
        log("Srun: 拿不到本机出口 IP（不在校园网？）")
        return False

    sess = Session({"User-Agent": UA})

    # ---- 1) 拿 challenge ----
    try:
        r = sess.get("%s/cgi-bin/get_challenge?%s" % (
            base, urlencode({"callback": "x", "username": uid, "ip": ip})),
            timeout=10)
        ch = _srun_jsonp(r.text)
    except Exception as e:
        log("Srun: get_challenge 失败：%s" % e)
        return False

    token = ch.get("challenge")
    if not token:
        log("Srun: 没拿到 challenge，响应=%r" % (r.text or "")[:200])
        return False
    log("Srun: 拿到 challenge（%d 字符），ecode=%s" % (len(token), ch.get("ecode")))

    # ---- 2) 派生 hmd5 / info / chksum ----
    hmd5 = hmac.new(token.encode("utf-8"), pwd.encode("utf-8"),
                    hashlib.md5).hexdigest()
    payload = json.dumps({
        "username": uid, "password": pwd, "ip": ip,
        "acid": SRUN_AC_ID, "enc_ver": SRUN_ENC,
    }, separators=(",", ":"), ensure_ascii=False)
    info = "{SRBX1}" + srun_b64(srun_xencode(payload, token))

    chkstr = token + uid
    chkstr += token + hmd5
    chkstr += token + str(SRUN_AC_ID)
    chkstr += token + ip
    chkstr += token + str(SRUN_N)
    chkstr += token + str(SRUN_TYPE)
    chkstr += token + info
    chksum = hashlib.sha1(chkstr.encode("utf-8")).hexdigest()

    # ---- 3) 提交 ----
    params = {
        "callback": "x", "action": "login", "username": uid,
        "password": "{MD5}" + hmd5,
        # ⚠️ 门户实际值是 os="Windows 10" / name="Windows"，但实测服务端**不校验**，
        #    用 "Windows" 也能登录成功（而且省掉了空格的编码问题）。
        "os": "Windows", "name": "Windows",
        "double_stack": "0", "chksum": chksum, "info": info,
        "ac_id": str(SRUN_AC_ID), "ip": ip,
        "n": str(SRUN_N), "type": str(SRUN_TYPE),
    }
    try:
        r = sess.get("%s/cgi-bin/srun_portal?%s" % (base, urlencode(params)),
                     timeout=15)
        res = _srun_jsonp(r.text)
    except Exception as e:
        log("Srun: srun_portal 失败：%s" % e)
        return False

    suc = res.get("suc_msg") or ""
    log("Srun: suc_msg=%r ecode=%s error=%r"
        % (suc, res.get("ecode"), res.get("error")))

    # 🔴 判成功看 `suc_msg`，**不是 `ecode`** —— 已在线时 ecode 也是 0（实网踩过）。
    if suc in (SRUN_LOGIN_OK, SRUN_ALREADY_ONLINE):
        if suc == SRUN_ALREADY_ONLINE:
            log("Srun: 本来就在线（幂等，算成功）")
        # 门户建会话要几秒 —— 与 webauth 那边同理，多探几次再下结论。
        for i, delay in enumerate((2, 3, 3), 1):
            time.sleep(delay)
            net = test_internet(timeout=5)
            if net is True:
                log("Srun: 认证后第 %d 次探测：已连通" % i)
                return True
            log("Srun: 认证后第 %d 次探测：%s"
                % (i, "仍未连通" if net is False else "无法判定"))
        return False

    log("Srun: 认证被拒：%s"
        % (res.get("error_msg") or res.get("error") or "（没给原因）"))
    return False


def srun_logout(cfg=None):
    """Srun 下线。三态约定与 `portal_logout` 相同（True / False / None）。

    ⚠️ **对 macauth 会话无效**（2026-10-12 实网实测）—— 门户会回
       `login_error` / "You are not online."。

       那不是「失败」，而是「**本机已经没有计费会话**」
       （AC 可能仍在按 MAC 放行，见 `嘉兴大学适配.md` §5.1）。

       ⇒ 按 **True** 返回（确实没有会话可退），但日志会写明；
         **绝不能当 False 去重试** —— 重试多少次都是同一个结果，
         只会让用户白等三轮（`do_logout` 里那个重试循环）。
    """
    cfg = _cfg_or_load(cfg)
    base = _srun_base(cfg)
    uid = cfg.get("userId") or ""
    ip = local_ipv4()
    if not base or not ip:
        log("Srun: 缺 portalHost 或本机 IP，没法下线")
        return None
    try:
        r = http_get("%s/cgi-bin/srun_portal?%s" % (
            base, urlencode({"callback": "x", "action": "logout",
                             "username": uid, "ip": ip,
                             "ac_id": str(SRUN_AC_ID)})),
            timeout=15, headers={"User-Agent": UA})
        res = _srun_jsonp(r.text)
    except Exception as e:
        log("Srun: 下线请求失败：%s" % e)
        return None

    suc = res.get("suc_msg") or ""
    err = res.get("error") or ""
    msg = res.get("error_msg") or ""
    log("Srun: 下线响应 suc_msg=%r error=%r error_msg=%r ecode=%s"
        % (suc, err, msg, res.get("ecode")))

    if err == "ok" or suc:
        return True
    low = msg.lower()
    if "not_online" in low or "not online" in low:
        log("Srun: 门户说「本机不在线」—— 没有会话可退，算已下线")
        log("      ⚠️ 若本机开了 macauth，AC 可能仍按 MAC 放行（这不是错误）")
        return True
    return None


def portal_login(uid, pwd):
    """认证入口 —— 按 `portalType` **分派**。

    ⚠️ 这里只做分派，不碰两套实现里的任何逻辑：
       · `webauth`（默认）= 郑航那套，原 `portal_login` 的代码**一字未改**地
         搬进了 `webauth_login`；
       · `srun` = 深澜 Srun（嘉兴大学）。
    """
    pt = portal_type()
    if pt == "srun":
        return srun_login(uid, pwd)
    return webauth_login(uid, pwd)


def webauth_login(uid, pwd):
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
    """只读校验：True 正确 / False 密码错 / None 无法判断

    ⚠️ 2026-10-12 起按 `portalType` **分派**：**Srun 没有这个接口**
       （郑航那套是 `checkUserPwdGeneral.do`），直接返回 `None`（「无法判断」），
       调用方会按「跳过校验」处理。
       Srun 的密码对不对，只能在真正登录时从 `suc_msg` 看出来 ——
       这也够用：登录失败时会明确说「认证被拒」。
    """
    if portal_type() == "srun":
        log("Srun: 没有独立的密码校验接口，跳过（真正登录时才知道）")
        return None
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

    ⚠️ 2026-10-12 起按 `portalType` **分派**：`srun` 走 `srun_logout`，
       其余（默认）走下面这段 webauth 的实现 —— **逻辑一字未改**。
       ⚠️ 分派点放在**这里**（最里层）而不是 `do_logout()` ——
          后者那套「锁 + 三态 + 重试 + 写结果」是通用的，两套协议都能用。
    """
    if portal_type() == "srun":
        return srun_logout()
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


def _pid_alive(pid):
    """pid 对应的进程还活着吗（Windows）。True / False；判不出来返回 None。

    为什么不用 psutil：它**不在打包清单里**（只有 bench.py 这个测量端工具用它），
    exe 里 `import psutil` 必然 ImportError —— 于是那个「进程存活检查」
    在生产环境从来没跑过，guard.json 一有新鲜残留就一律「保守当作在跑」。
    ctypes 是标准库、跟着程序走，exe 和源码里行为一致。（2026-10-10 修。）

    判定口径：
      * OpenProcess 成功 → GetExitCodeProcess：STILL_ACTIVE(259) = 还在跑。
        （退出码恰好等于 259 的进程概率忽略不计，psutil 同款口径。）
      * 打不开 + ERROR_ACCESS_DENIED → 当作活着（不让看 ≈ 还在）。
      * 打不开 + ERROR_INVALID_PARAMETER → 系统里没这个 pid = 已退出。
      * 其它 → None，调用方按「判不出来」保守处理。

    🔴 句柄必须声明 restype（64 位下不声明按 c_int 会截断 —— 项目的老坑），
       句柄在 finally 里 CloseHandle，别漏。
    """
    try:
        import ctypes
        pid = int(pid)
        if pid <= 0:
            return None
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.GetExitCodeProcess.restype = ctypes.c_int
    k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_uint32))
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    try:
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                            pid & 0xFFFFFFFF)
    except Exception:
        return None
    if not h:
        err = ctypes.get_last_error()
        if err == 5:          # ERROR_ACCESS_DENIED：进程在，只是不让看
            return True
        if err == 87:         # ERROR_INVALID_PARAMETER：没有这个进程
            return False
        return None
    try:
        code = ctypes.c_uint32(0)
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return None
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


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
    alive = _pid_alive(pid)
    if alive is None:
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
    last_state = None  # 上一拍的判定结果；只在**变化**时记日志（见下面的说明）
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
            # 🔴 每一拍都把**判定结果**记下来（2026-10-12，嘉兴实机测出来的）：
            #    以前 GUARD_IDLE 是**完全静默**的，于是「守护 4 分钟一次登录都没尝试」
            #    在日志里只表现为「只有心跳」—— 事后完全分不清是
            #    「不在校园网」「已经在线」还是「测不出来」，只能靠猜。
            #    ⚠️ 只在**变化**时记：30 秒一条会把日志刷满（那正是当初保持静默的理由）。
            #       首拍一定记，所以任何异常状态都不会被漏掉。
            state = (on, net, act)
            if state != last_state:
                last_state = state
                log("守护判定：在校园网=%s 联网=%s（本机IP=%s）→ %s"
                    % ({True: "是", False: "否", None: "判不出"}[on],
                       {True: "已联网", False: "被门户拦", None: "判不出"}[net],
                       local_ipv4() or "取不到", act))
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

  1. 选学校：「账号信息」那行右边的下拉框。默认是郑航，本校用户不用动。
  2. 填上你的校园网账号和密码
  3. 点「仅保存」
  4. 勾选最下面的「开机时自动登录校园网」

  以后开机自动联网，不用再管。


【整个程序只有一个窗口】

  左边竖着一排入口：设置 / 账号中心 / 使用说明。点一下就换页，不用为每个功能
  各开一个窗口 —— 想同时看好几样，来回点也不会满屏都是窗口。当前在哪一页，
  那一行就是白底 + 左边一道蓝色竖条。

  · 设置      就是这一页：账号、开机自启、隐私、网络状态。
  · 账号中心  「你有账号的那所学校（郑航）」的账号情况，见下一节。
               学校没有自助服务时这一项不出现。
  · 使用说明  你现在看的这份。


【账号中心（郑航）】

  查你自己账号的情况：账号状态、是不是停机了、套餐、余额、在线设备、每天用了
  多少流量（柱状图），以及「上网详单」（每次上网的起止时间、时长、上行、下行、
  断开原因）。

  点右上角「同步数据」才需要登录学校自助服务 —— 要照着图输一次 4 个字符的
  验证码。登进去之后会顺手把「每天流量」攒在本机，以后不登录也能翻历史。

  ⚠️ 三件事说在前头：
    · 数据是**只读**的。本程序不发任何改动的请求（不踢设备下线、不解绑、
      不缴费、不改密码）。充值、办套餐这类涉及付款的事会**跳到你学校自己的
      网页**去办，本程序不代办。
    · 「上网详单」和「停机状态」每次都从学校现取，不留在本机；只有每天流量
      那几个数字会攒在本机（按账号分开）。想清掉点「清掉本机历史」。
    · 窗口够宽时卡片会自动分成两列，不用一路往下滚。


【换学校】

  点「账号信息」那行右边的「学校」下拉框就能换。目前内置两所：

    · 郑州航空工业管理学院（默认）
    · 嘉兴大学

  换完之后，登录、退出、开机自启都会走新学校的认证门户，不用重启程序。

  一个提醒：已保存的账号列表是几所学校**共用**的。如果某个账号是在另一所学校
  下保存的，它那一行会多出一个橙色的学校名。真去点「切换到此账号」时程序会
  再问你一次 —— 因为继续下去会把那个账号的密码发到当前学校的门户服务器上。
  确实要用它，点一次「仅保存」就能把标记改成当前学校。


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

  右边那个「测一下速度」量的是「从本程序的发布站下载」的速度，也就是这台
  电脑能不能顺畅地下载和更新本程序。点一下，几秒后出结果。
  它测的不是你家宽带的带宽，只是到发布站这一路。


【账号密码存在哪】

  存在这台电脑的  C:\ProgramData\CampusLogin  里，不在这个程序里。所以把
  「校园网自动登录.exe」这一个文件拷给别人，不会带出你的账号、密码和登录记录。

  🔒 密码是**加密**存的 —— 用 Windows 自带的 DPAPI，密钥由 Windows 按你这个
  登录用户保管，本程序既不生成也不保存密钥。效果是：换一个 Windows 用户登录、
  或者把这两个文件拷到别的电脑上，**解不开**（打开只会看到 enc:v1: 开头的乱码）。
  老版本存下的明文密码会自动升级，你不用做任何事。

  ⚠️ 三条得说清楚，别把它当保险箱：
    · 同一个 Windows 用户下运行的**其它程序仍然解得开** —— DPAPI 是「按用户」
      不是「按程序」。它挡的是「别的用户 / 文件被拷走」，挡不住「这台电脑
      已经中了木马」。
    · 如果你哪天把「开机自动登录」的运行身份改成了别的用户（默认就是你自己），
      密码会解不开 —— 那时程序会当成「没存过」，需要重新输入一次。
    · 校园网密码往往和教务、邮箱同源，所以这个功能的意义比「一个校园网」大。

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

  相邻版本之间真正变动的部分通常只有百分之几，所以升级时多数情况只下载
  变动的那一小块（几百 KB），不用每次重下整个程序。万一增量这条路走不通，
  它会自动改成下载完整包，你不需要做任何选择。

  更新包有多个来源：本程序的发布站 hobson2233.dpdns.org、GitHub，
  以及发布站上另外列出的线路。
  下载前会各测一下速度，从快的那个下；一个下不动会自动换另一个。

  如果下载失败，程序会问你要不要用浏览器打开下载页。浏览器下载支持
  断点续传（网络断了能接着下），比程序内下载更稳，觉得慢也可以用它。

  如果显示「检查更新失败」，多半是当时网络不通，稍后再点一次就行。
  失败时会明确说「失败」，不会含糊地显示「已是最新版本」。


【注意】

  · 只适用于内置档案里的那几所学校（见上面「换学校」）。部分杀毒软件可能误报，
    加白名单即可。
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
        self.box, _body = round_panel(parent, tk, dp(8), dp(5), R_CTRL)
        self.entry = tk.Entry(_body, font=font_main, relief="flat", bd=0,
                              bg=CARD, fg=fg, highlightthickness=0,
                              insertbackground=fg)
        self.entry.pack(side="left", fill="x", expand=True, ipady=dp(2), padx=(dp(3), dp(0)))
        self.button = tk.Button(_body, text="显示", font=font_small,
                                relief="flat", bd=0, bg=CARD, fg=BLUE_DARK,
                                activebackground=BLUE_SOFT, activeforeground=BLUE_DARK,
                                cursor="hand2", padx=dp(10), command=self.toggle_reveal)
        self.button.pack(side="right", padx=(dp(4), dp(1)), ipady=dp(2))

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


# ==================== 高 DPI（界面清晰度） ====================
# 🔴 2026-10-16 加。用户反馈「软件里面的文字不太清晰」——根因不在字体，
#    在**这个开关没开**。详见 `_init_dpi()` 的说明。
def _init_dpi():
    """开高 DPI 感知，返回缩放因子（96 DPI = 1.0）。

    🔴 为什么必须开：
       本机屏幕是 2560x1600、Windows 缩放 150%。**不开**这个开关时，
       Tk 只知道「屏幕 1707x1067」（2560÷1.5，Windows 报给不知情进程的
       虚拟值），于是按 1707 把界面画好、再由 Windows 把**整张位图**
       放大 1.5 倍贴到屏幕上。位图放大 = 一堆放大后的像素块 = 字发虚。
       这跟字体选得对不对**没关系** —— 界面上用的本来就是微软雅黑。
       开了之后 Tk 直接按物理像素渲染，字就锐利了。

    🔴 为什么还要返回一个缩放因子：
       开了之后 `winfo_screenwidth()` 从「1707」变成「2560」，而界面里
       所有尺寸常量都是**按 96 DPI 设计**的（`OUTER_PAD=14`、`NAV_W=168`、
       `geometry_test.CONTENT_H=780`…）。不补偿的话整个界面会缩水到 2/3、
       字挤成一团。乘上这个因子之后「设计像素 → 物理像素」的换算就补回来了：
       界面物理尺寸和以前**一模一样**，只是渲染变清晰。

    ⚠️ 必须在**建任何 Tk 窗口之前**调用（Tk 只在创建时读一次 DPI）。
       所以这段直接放在模块顶层 —— import 时就会跑，比任何调用点都早。
    ⚠️ 拿不到 DPI 时返回 1.0（退回旧样子）：清晰度没改善，但界面不会错乱。
       宁可少一个改进，也不要出一个排版崩掉的版本。
    """
    if os.name != "nt":
        return 1.0
    try:
        import ctypes
        try:
            # 2 = PER_MONITOR_AWARE：多显示器各自缩放不同时也各自正确
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            # Win7/8 没有 shcore.dll，退回系统级感知（老 API，够用）
            ctypes.windll.user32.SetProcessDPIAware()
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(1.0, dpi / 96.0) if dpi else 1.0
    except Exception:
        return 1.0


UI_SCALE = _init_dpi()


def dp(v):
    """「设计像素」→ 当前屏幕的物理像素。

    界面里所有尺寸都写设计值：`dp(14)` 就是「按 96 DPI 设计的 14px」。
    100% 缩放的屏上原样返回，150% 的屏上自动 ×1.5 —— 两种屏看起来一样大。
    ⚠️ 只用在**尺寸**上，别用在秒数、次数、倍率（那些跟屏幕无关）。
    """
    return int(round(v * UI_SCALE))


# ==================== 窗口几何 ====================
# 抽成纯函数是为了能测：这段算错在受控会话里很难复现（要改屏幕分辨率），
# 而它恰恰是「小屏幕上底部控件够不到」这类问题的根源。
#
# ⚠️ `window_geometry()` 的入参和返回值**都是设计像素**（不是物理像素）——
#    这样上面那些标定值（1000/1320/560/80）和 `geometry_test.py` 的断言
#    全都不用动。调用点负责换算（见 `run_gui`）。

OUTER_PAD = dp(14)      # 最外层留白
INNER_PAD = dp(14)      # 卡片内留白

# ---------------------------------------------------------------- 圆角规范（2026-10-17）
# 🔴 换 Win11 Fluent 时立的规矩：**圆角只有四档**，每一档有明确的用途。
#    在这之前是「每个控件各写一个数」（卡片 14 / 状态卡 11 / 导航项 9 /
#    导航面板 12 / 输入框 5 / 按钮是胶囊），五个数各不一样 ——
#    那本身就是「没有规则」的观感来源，也正是「看着不专业」的一部分。
#
#    Win11 的实际取值就是「大容器 8、小控件 4~5」这么简单，照抄即可：
#      · `R_CARD`   —— 卡片 / 面板 / 导航窗格这类**大容器**
#      · `NAV_R`    —— 导航项（比卡片小一档）
#      · `R_CTRL`   —— 按钮 / 输入框这类**控件**
#      · `R_CHIP`   —— 徽章 / 标签（胶囊，跟着高度走，见 `pill_radius`）
#
#    ⚠️ 这四个都必须是**物理像素**（`dp()` 过）—— 它们最终喂给
#       `_draw_round_rect`，和 `winfo_width()` 量出来的物理尺寸直接比。
#    ⚠️ 还有一条硬约束：`round_panel` 要求 `pad_x >= radius` 且 `pad_y >= radius`。
#       改大 `R_CARD` 时要连带检查卡片的 `padx` / `ipady`。
R_CARD = dp(8)
NAV_R = dp(6)
R_CTRL = dp(5)
# 导航项选中时左侧那条强调竖条的宽度（Win11 用 3px）。
NAV_BAR_W = dp(3)

# ---- 「一个窗口、多个页面」的骨架（2026-10-16） ----
# 🔴 用户原话：「如果咱们软件每加一个新功能就要加一个窗口，那用户同时看好几个
#    功能不就全屏都是窗口了」—— 所以主窗口从「一屏设置」改成
#    **左侧导航 + 右侧内容**，功能都在同一个窗口里进出。
#
# 为什么导航放**左边**而不是做成顶部标签：
#    竖向空间是本项目最紧的资源（内容 827px / 上限 906px，`content_h_probe.py`
#    量过），横向却很富裕（窗口在 1707 宽的屏上只占 1050）。标签栏要吃掉
#    ~40px 高，侧边栏只吃横向 —— 花在横向上才是划算的那一边。
NAV_W = dp(168)     # 导航栏宽度（含它自己那张圆角面板）
NAV_GAP = dp(16)    # 导航栏与内容区之间的缝
# 0.3.6 起改成 0。它原本记的是「给标题栏/边框留的余量」，但 Tk 的
# `geometry("WxH")` 里的 H **就是客户区高度、不含标题栏**（实测：
# 传 851 → winfo_height()=851、holder 实际也拿到 851-28=823）。
# 也就是说这 40px 从来不是被标题栏吃掉的，而是一路变成卡片底部的空白 ——
# 实测 holder 需要 783、实际给到 783，客户区 851 里有 68px（28 外层留白
# + 40 这个 EXTRA_H）全是富余。窗口不吃紧时无所谓，在 16:10 上就是白高 40px。
# 留着这个常量是为了不改调用点的签名，值归零即「不再额外加高」。
EXTRA_H = 0
# ⚠️ 下面这几个是**设计像素**，不是物理像素 —— 它们只被 `window_geometry()` /
#    `minsize_for()` 用，而那两个函数**整段都在设计像素里运算**
#    （入参设计、返回也设计，见它们各自的 docstring）。**不要**给它们套 `dp()`，
#    套了就变成「设计 × 缩放」叠了一层，窗口会大得离谱。
SCREEN_MARGIN_H = 80    # 给任务栏 + 标题栏留的余量
# 窗口高度最多占屏幕的比例。0.85 —— 留出上方标题栏和下方任务栏，也让窗口
# 四周有点桌面透出来，不至于「贴满上下沿」。**这个比例就是「16:10 屏上显得
# 太长」的正解**：16:10 的高本来就只有宽的 62.5%，如果高度再按内容硬撑，
# 就会一路顶到屏幕边。按下限和上限同时收口，窗口才是「跟着屏幕走」的。
SCREEN_H_RATIO = 0.85
MIN_H = 560         # 再矮也不小于这个（低于它就一定挂滚动条了）。设计像素。
# 最小宽度 = 内容区最少 700px + 导航栏(168) + 缝(16)。**设计像素**。
# ⚠️ 700 是**内容区**的最小值（设置页里有 wraplength 680/700 的说明文字，
#    再窄就会被静默裁掉），加上导航栏和缝才是窗口的最小宽度。
# ⚠️ 这里写 168 / 16 字面量，**不要**换成 `NAV_W` / `NAV_GAP` —— 那两个是
#    **物理像素**（`NAV_W = dp(168)`），混进来这个算式就成了「设计 + 物理」
#    的四不像，在 150% 的屏上会算出偏大的最小宽度。
MIN_W = 700 + 168 + 16


def window_geometry(sw, sh, reqh):
    """按屏幕尺寸和内容需要的高度算窗口几何，返回 (w, h, x, y)。

    **宽度只按屏幕算**，跟内容想要多宽无关（内容宽度由下面 1000 的下限兜住）。
    原来签名里还有个 `reqw` 参数，但全仓没有任何调用点传它、函数体里也从不使用
    —— 一个"看起来会让宽度自适应内容、实际完全不影响"的死参数，只会误导人，
    已经删掉。真要按内容调宽度，得先有调用点传 `winfo_reqwidth()` 再说。

    🔴 **0.4.0 起宽度明显变宽**（820 → 1000..1320）。原因：主窗口改成了
       「左侧导航 + 右侧内容」，导航要占掉 `NAV_W`(168) + `NAV_GAP`(16) ——
       不把窗口加宽的话，右侧内容会从原来的 ~792 掉到 ~608，
       设置页里 `wraplength=680` 那几行说明文字会全部提前折行、卡片高度跟着变，
       `geometry_test.CONTENT_H` 那套标定全部作废。
       加宽之后右侧内容 ≈ 1050-168-16-28 ≈ 838，比原来还宽一点，
       所以竖向高度**一个像素都没变**（这条有 `content_h_probe.py` 复核）。
       比例取 0.62 而不是 0.44：0.44 在 1707 宽的屏上算出 751，被 820 的下限
       兜住 —— 那说明这个比例早就没在起作用了，一直是下限在定宽度。

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

    w = max(1000, min(1320, int(sw * 0.68)))
    h = max(MIN_H, min(ratio_h, max_h, reqh + EXTRA_H))
    w = min(w, max_w)
    # 🔴 也不能太「贴边」：1024 宽的屏上 0.68 只有 696，会被 1000 的下限拉到 1000，
    #    那就是屏幕的 98%，左右几乎没余量、看着像全屏。所以再按屏幕比例收一道。
    #    ⚠️ 收下来之后右侧内容可能不够宽 —— 那没关系，正文会**自动退回单列**
    #    （判据见 `SelfServiceWindow.MIN_2COL_W`），不是裁掉。
    w = min(w, int(sw * 0.86))
    h = min(h, max_h)          # 屏幕装不下就按屏幕来，宁可小也不要跑出屏幕

    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)
    return w, h, x, y


def minsize_for(sw, sh):
    """最小尺寸同样要按屏幕收口，否则小屏幕上用户连缩都缩不动。

    🔴 2026-10-16：**最小宽度从 700 涨到 700 + 导航栏**。加了左侧导航之后，
       700 宽的窗口只剩约 500 给内容区 —— 而设置页里有几行说明文字的
       `wraplength` 是 680/700（`warn_auto`、`self.msg`）。Label 的
       `create_window` 是按**自己要求**的尺寸摆的，比容器宽就直接被裁掉，
       **不报任何错**。所以最小宽度得把导航栏那 184px 一起算进去。

    ⚠️ 入参和返回**都是设计像素**（和 `window_geometry` 同一套约定）——
       调用点自己过 `dp()`。理由见 `window_geometry` 上面那段说明。
    """
    return (min(MIN_W, max(360, sw - 40), int(sw * 0.86)),
            min(520, max(320, sh - SCREEN_MARGIN_H)))


# ==================== 窗口外框圆角（2026-10-17） ====================
# 🔴 先说实话：**在 Win11 上这个调用基本是「显式化」，视觉上不会有变化。**
#    实测（`_lab/corner_check.py`，抓屏幕做对照实验）：本机 Win11 上
#    Tk 窗口**本来就是圆角**的 —— 把偏好设成 `DONOTROUND(1)` 也变不回直角。
#    所以别指望靠它「把方窗口变圆」，它做不到那件事，因为本来就已经是圆的。
#
#    那为什么还留着：这是一条**兜底**。圆角是 Win11 的默认行为，但它是
#    系统级的、可以被别的因素改掉（组策略、第三方「窗口美化」工具、
#    某些远程桌面环境）。显式声明一次，成本为零，能挡住「哪天突然变方」。
#
# ⚠️ 只加圆角，**不碰标题栏颜色**（`DWMWA_CAPTION_COLOR` = 35）：
#    标题栏的底色和文字色是**两条互相独立的属性**（文字是
#    `DWMWA_TEXT_COLOR` = 36）。只改底色不改文字，在**深色模式的 Windows**
#    上就变成「浅底 + 白字」，标题直接读不出来 —— 那比直角严重得多。
#    所以这里保持零风险：只做圆角，别的一概不动。
#
# ⚠️ `DwmSetWindowAttribute` 在 Win10 及更早版本上不认识 33 这个属性，
#    会返回 `E_INVALIDARG`。返回值**必须检查**、失败就安静跳过 ——
#    在 Win10 上跑得好好的程序，不该因为一个纯装饰特性起不来。
DWM_CORNER_ROUND = 2            # DWMWCP_ROUND（另有 0=DEFAULT / 1=DONOTROUND / 3=ROUNDSMALL）
DWMWA_CORNER_PREFERENCE = 33


def enable_round_corners(win):
    """把窗口外框的圆角偏好显式声明成 Win11 的圆角。返回是否调用成功。

    🔴 返回值只代表「这个 API 调用成功了」，**不代表「窗口因此变圆了」** ——
       在 Win11 上它本来就是圆的（见上面那段实测）。别拿这个返回值当
       「圆角已生效」的证据。

    纯装饰：返回 False 只表示「这个系统上没做成」，**调用方不需要做任何补救**。
    """
    try:
        if not sys.platform.startswith("win"):
            return False
        import ctypes
        from ctypes import wintypes

        win.update_idletasks()
        user32 = ctypes.windll.user32
        # 🔴 HWND 的取法：Tk 顶层窗口的 `winfo_id()` 给的是 Tk 自己那个 HWND，
        #    而真正被 DWM 合成的**往往是它的 parent**（Tk 会套一层 wrapper）。
        #    实测两种都出现过，所以先试 parent、拿不到再退回自身。
        #    ⚠️ 必须显式声明 argtypes：HWND 在 64 位上是**指针宽度**，
        #       不声明的话 ctypes 按 C int 传，高 32 位会被截断。
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        if not hwnd:
            return False

        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        pref = ctypes.c_int(DWM_CORNER_ROUND)
        hr = dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref))
        return hr == 0
    except Exception:
        return False


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


# 下载进度每涨这么多百分比才推一次（不然队列会被进度消息淹没）。
DL_STEP = 3


def dl_progress_reset(state, now=None):
    """把进度状态归零。**每次重试之前必须调**，理由见 dl_progress_text。"""
    state["last"] = -1
    # 用 perf_counter：单调、高精度。time.time() 会被系统对时改动，而且
    # 在 Windows 上粒度约 15ms —— 测速那处已经因为它量到过 dt=0。
    state["t0"] = time.perf_counter() if now is None else now


def dl_progress_text(state, got, total, now=None):
    """下载进度的节流与文案。该推就返回字符串，不该推返回 None。

    `state` 是调用方持有的可变 dict：`{"last": 上次报的百分比, "t0": 本轮开始时刻}`。

    🔴 **进度回退 = 新的一轮，这个函数自己会认出来**（2026-10-08 补）。
       「从头下」的地方有三处：重试、换源、以及 0.5.0 加的「补丁不行改下完整包」。
       三处都会让 got 从 0 重新数，而 `last` 还停在上一轮的最高百分比上 ——
       于是新一轮在 0% ~ 上次那个百分比之间**一条进度都不报**，界面卡住不动，
       看着像死机。实测复现过：第 1 轮报 3,6,…,60，第 2 轮只从 63 开始报。

       原来的修法是「每个重启点都记得调 dl_progress_reset()」——
       而那正是这个 bug 的成因（漏一处就复发，换源那处就一直漏着）。
       现在改成函数自己判断：**pct 比上次小**就说明换了一轮，就地归零 + 重置计时。
       dl_progress_reset() 仍然留着，给「还没收到任何进度就想先重置」的调用方用。

    为什么抽成纯函数：闭包里的逻辑**脚本点不到**，只能靠点界面验证 ——
    而这条 bug 恰恰只在「下载中途出岔子」时才出现，最难手动撞上。

    速度取的是**本轮平均**（从 state["t0"] 算），不是瞬时值 —— 瞬时值一抖就跳，
    反而让人以为出问题了。dt 太小时不给速度（除法会被抖动放大）。
    """
    if now is None:
        now = time.perf_counter()
    pct = 0 if not total else int(got * 100 / total)
    if pct < state["last"]:
        # 新的一轮。计时也一起重置 —— 否则速度会拿「本轮的字节 ÷ 跨轮的时长」
        # 去算，算出来偏小，而偏小的速度用户是会当真的。
        state["last"] = -1
        state["t0"] = now
    if pct < DL_STEP or pct == state["last"]:
        return None
    if pct < state["last"] + DL_STEP and pct != 100:
        return None
    state["last"] = pct
    dt = now - state["t0"]
    spd = ""
    if dt > 0.3 and got:
        spd = "  %.1f MB/s" % (got / dt / 1048576.0)
    return "下载中 %d%%%s" % (pct, spd)


def browser_fallback_url(info, used_url=""):
    """下载失败时该用浏览器打开哪个地址。

    优先用**这次实际试过的那个源**（`used_url`）—— 它刚才至少还连得上
    （失败往往发生在下到一半），比让用户从主站从头再试一次更可能成功。
    拿不到就退回清单里的主源；再拿不到就退回发布站首页。

    抽成纯函数是为了能在 --selftest 里断言 —— 「真开一个浏览器」那一步
    没法自动验，能验的是「该开哪个地址」这条判断。
    """
    used_url = (used_url or "").strip()
    if used_url:
        return used_url
    if isinstance(info, dict):
        u = (info.get("url") or "").strip()
        if u:
            return u
    return updater.BASE_URL_HINT


def open_in_browser(url):
    """用系统默认浏览器打开一个网址。返回 True / False。

    为什么要有这个出口（2026-10-08，用户反馈「室友在软件里更新下载超级慢」）：
        程序内的下载**没有断点续传** —— 发布站是 Cloudflare Worker 的静态资源，
        实测不支持 Range（带 Range 的请求返回的是 200 + 完整文件，不是 206），
        所以网络一抖就得从 0 重来。而浏览器下载**支持续传**，断了能接着下，
        还能看到真实进度。下载失败时给用户这条退路，比让他干瞪眼强。

    ⚠️ `webbrowser` 用**函数内导入**：它只在「用户真的选了用浏览器打开」时
       才需要。放函数里，模块导入阶段就不碰它 —— 少一个能把整个程序拖下水的
       导入点。（PyInstaller 照样会把它打进包里，不影响 exe。）
    """
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        log("打开浏览器失败:\n%s" % traceback.format_exc())
        return False


def download_source_desc():
    """`--selftest` 用：多源清单能不能被正确解析出两个下载源。

    为什么**真的跑一遍**而不是打印一句写死的说明：0.4.4 的多源选路完全依赖
    清单里的 `urls` 字段。字段名写错、或 evaluate() 忘了透传，症状是
    **悄悄退回单源** —— 不报错、不失败，只是又变慢了。写死的说明在那时候
    照样打印「主站 + GitHub」，等于没有这个检查。
    """
    try:
        root = updater.BASE_URL_HINT
        demo = {
            "version": VERSION,
            "url": "%s/dl/campus-login-%s.exe" % (root, VERSION),
            "urls": ["%s/dl/campus-login-%s.exe" % (root, VERSION),
                     "https://github.com/%s/releases/download/v%s/%s"
                     % (updater.GITHUB_REPO, VERSION, updater.GITHUB_ASSET)],
            "sha256": "a" * 64,
            "size": 1,
        }
        _st, info = updater.evaluate(demo, "0.0.0")
        urls = updater.candidate_urls(info)
        hosts = [urlparse(u).hostname or u for u in urls]
        return "%d 个（%s）" % (len(urls), "、".join(hosts))
    except Exception:
        log("下载源解析自检失败:\n%s" % traceback.format_exc())
        return "解析失败（见日志）"


def mirror_merge_desc():
    """`--selftest` 用：远程附加源能不能被正确合并，且**主源没被顶掉**。

    为什么真跑一遍而不是打印写死的说明：附加源是**从网上取回来的一个文件**，
    它唯一能造成的伤害就是「改变首选下载地址」。这里断言两件事：
      ① 附加源确实被追加上了（`{version}` 也替换了）；
      ② 第一项仍然是我们自己的站。
    写死的说明在那两条失效时照样打印「附加源可合并」，等于没有这个检查。
    """
    try:
        root = updater.BASE_URL_HINT
        primary = "%s/dl/campus-login-%s.exe" % (root, VERSION)
        entries = [{"name": "自检假源",
                    "url": "https://example.invalid/v{version}/x.exe"}]
        merged = updater.merge_sources([primary], entries, VERSION)
        if not merged or merged[0] != primary:
            return "主源被顶掉了（异常）"
        if len(merged) != 2 or "{version}" in merged[1]:
            return "附加源没合并进来（异常）"
        # 顺手确认「关掉的源会被跳过」—— 这是我们下线一条线路的唯一手段。
        if updater.mirror_urls([{"url": "https://example.invalid/x",
                                 "enabled": False}], VERSION):
            return "enabled=false 没被跳过（异常）"
        return "可合并，主源仍在第一位"
    except Exception:
        log("附加源合并自检失败:\n%s" % traceback.format_exc())
        return "解析失败（见日志）"


def patch_tool_desc():
    """`--selftest` 用：内嵌的补丁工具在不在、在哪、多大。

    为什么要报这个：`--add-binary` 漏了或路径写错时，**打包不报错、程序照常跑**，
    只是每次更新都静默退回「下 10 MB 完整包」—— 用户看不出任何异常，
    只是觉得更新一直很慢。这种「功能悄悄退化」只有靠自检报出来 + gate 档
    对真 exe 断言才拦得住（见 build.py 的 _check_tools，那道管的是「打进包里没有」，
    这道管的是「运行期找得到没有」，缺一不可）。
    """
    p = updater.hpatchz_path()
    if not p:
        return "没找到（增量更新会退化成完整下载）"
    try:
        n = os.path.getsize(p)
    except OSError as e:
        return "找到了但读不到：%s（%s）" % (p, e)
    return "%s（%d 字节）" % (os.path.basename(p), n)


def patch_plan_desc():
    """`--selftest` 用：拿一份**假的**清单，真跑一遍「挑补丁」这段逻辑。

    和 download_source_desc / mirror_merge_desc 同一个理由：打印写死的说明
    在那段逻辑被改坏之后照样会打印，等于没有这个检查。这里真跑一遍，
    断言「版本对得上就挑得到、对不上就挑不到」—— 后者尤其重要，
    挑错了会拿一个给别的旧版本的补丁去还原，结果是白下 500 KB 再回退。
    """
    try:
        full = 10 * 1024 * 1024
        entry = {"from": VERSION,
                 "url": "https://example.invalid/patch/%s-to-9.9.9.hpatch" % VERSION,
                 "sha256": "0" * 64, "size": 500 * 1024}
        info = {"version": "9.9.9", "size": full, "patches": [entry]}
        if not updater.pick_patch(info, VERSION):
            return "版本对得上却挑不到（异常）"
        if updater.pick_patch(info, "0.0.1"):
            return "版本对不上却挑到了（异常）"
        # 补丁比完整包一半还大时客户端要拒绝 —— 服务端已经筛过，这里再筛一次。
        big = dict(entry, size=full // 2 + 1)
        if updater.pick_patch({"version": "9.9.9", "size": full, "patches": [big]}, VERSION):
            return "过大的补丁没被拒绝（异常）"
        return "可挑选（版本对得上才用）"
    except Exception:
        log("补丁挑选自检失败:\n%s" % traceback.format_exc())
        return "解析失败（见日志）"


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


class _SilentBox:
    """自检用的**假弹窗**：所有方法立刻返回，不真的弹出来。

    为什么需要（2026-10-11）：`--guitest` 是无人值守的，而模态弹窗会**一直等用户点**。
    以前自检路径不会弹框 —— `update_wiring_check` 特意挑 CURRENT 这条「不弹确认框」的
    最短路径走。给 CURRENT 加了 info 框之后这条前提就失效了：不换假框的话，自检会
    卡在弹窗上直到超时，而报出来的是「自检没通过」，指不到真实原因。

    ⚠️ `askyesno` 返回 **False**：自检里遇到「要不要做某个有副作用的动作」时，
       安全的默认是「不做」。返回 True 会让自检去执行真实操作（下载 / 替换自己）。
    """

    def showinfo(self, *a, **k):
        return "ok"

    def showwarning(self, *a, **k):
        return "ok"

    def showerror(self, *a, **k):
        return "ok"

    def askyesno(self, *a, **k):
        return False


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
    NET_OFF_CAMPUS: NEUTRAL_SOFT,
    NET_OK: OK_SOFT,
    NET_NEED_LOGIN: DANGER_SOFT,
    NET_UNKNOWN: NEUTRAL_SOFT,
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
# 参照现有取值定：次要按钮 BTN_SOFT #f5f5f5 落在白卡 CARD #ffffff 上，差 10 ——
# 那是肉眼一眼就能分辨的程度。取 8 留一点余量，但足够拦住「几乎同色」。
_CONTRAST_MIN = 8

# 默认「禁用灰」在浅底上撞了背景时改用的颜色。
# 比 BG(#f3f3f3) 深 29 级、比 CARD(#ffffff) 深 41 级，两个底上都看得出来。
_DISABLED_FALLBACK = "#d6d6d6"


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
        「使用说明」按钮的静止底色一开始写成了页面底色 BG 的那个值。
        结果按钮和背景完全融成一片，截图里就剩四个灰字，跟普通说明文字没区别，
        用户根本不会去点。这类错**不抛任何异常**：逻辑自检全绿、窗口照常显示、
        `--guitest` 也是 GUI_OK，只有把截图放大若干倍才看得出来。
        静态读源码同样靠不住 —— 那个字面色和几百行外的 `BG` 常量隔着半屏，
        肉眼对不上（而且换配色时它们会**一起变**，更看不出问题）。
        ⚠️ 2026-10-17 换 Fluent 时同一个坑又露头了一次（新的 BG 是浅灰，
           「落在页面灰底上的按钮」很容易选到和它只差 2 级的颜色）——
           是这个自检在构建时拦下的，不是靠眼睛。

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


def round_chip(parent, tk, text, font, fg, soft, page=CARD, pad_x=dp(9),
               height=dp(18), radius=None, command=None, cursor="hand2",
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
    # 🔴 2026-10-17（换 Fluent）：**能点的用 `R_CTRL`，纯标签才用胶囊**。
    #    Win11 里「可以点的东西」是 4~5px 的小圆角矩形，只有徽章 / 计数那种
    #    **纯展示**的才是胶囊。原来这里一律胶囊，于是「检查更新」「测一下速度」
    #    「选用」「删除」全都长得像徽章 —— 分不出哪个能点。
    #    ⚠️ 判据是 `command` 传没传，**不是**「看着像不像按钮」：
    #       传了 command 就是按钮，没传就是标签，这在调用点是一目了然的。
    if radius is None:
        r = R_CTRL if command else pill_radius(height)
    else:
        r = radius

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

        def invoke():
            """按一下。**和 `round_button.invoke()` 对称** —— 测试脚本两种控件
            都能用同一种方式「点」，不用去记「这个要用 event_generate、
            那个要用 invoke」。2026-10-16 加：C 档的「打开」是圆角片，
            而 `event_generate` 在没映射的控件上不一定触发，容易写出
            「看着点了其实没点」的测试。
            """
            _click()

        cv.invoke = invoke

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


def nav_button(parent, tk, text, icon, font, command, width, height=dp(38),
               fg=SUB, active_fg=BLUE, active_fill=NAV_ACTIVE, page=NAV_BG,
               hover=NAV_HOVER):
    """左侧导航栏里的一项：**左边图标 + 右边一行字**，整条可点。

    🔴 2026-10-17 改成 Win11 的画法（换 Fluent 时）：
        选中项 = **白色圆角底 + 左侧一条强调竖条**，未选中项完全透明。
        原来是「浅蓝底 + 深蓝字」—— 那是网页后台侧栏的观感。
        ⚠️ 底色也跟着从 `BG` 换成了 `NAV_BG`（比内容区深一档）——
           Win11 的导航窗格不是一张白卡片，见 palette.NAV_BG 的说明。
           这一条由 `ui_contrast_test` 的「导航项底色必须和 NAV_BG 分得开」
           那段钉着（悬停态差 11 级）。

    🔴 为什么不直接用 `round_chip`（导航项和它长得像）：
        `round_chip` 的宽度是**按文字量出来的**、文字还是**居中**的。
        导航项要的恰好是相反的三件事 —— 宽度固定、文字左对齐、图标在最左。
        拿 chip 拼出来的导航会是一排宽窄不一、字都居中的小标签，
        看着就是「没做完」。所以这里单独写一个。

    🔴 **故意不挂 `_rb_bg`**（`round_chip` / `round_button` 都挂了）：
        未选中的导航项**本来就是透明的**（露出底下的导航窗格底色），
        这是标签页的通用画法，不是「按钮和背景同色」那种缺陷。
        挂上去会被 `ui_contrast_check` 判成「按钮隐形」而误报 ——
        而那个自检是**硬拦构建**的（UI_FAIL_PHRASE）。宁可少查一项，
        也不能让一条会误报的自检天天挡发布。
        选中态靠 `set_active(True)` 的白底 + 强调条区分，这一条由
        `selfwin_test.py` 的导航段单独钉着。

    ⚠️ 图标名不认识时**当场抛异常**，不静默画空白：
        `_draw_icon` 对不认识的名字返回 0（什么都不画），
        那种失败在截图上就是「这个导航项没有图标」，很容易被当成设计如此。

    返回的 Canvas 上挂了（和 round_chip / round_button 对齐，测试能一视同仁）：
        `invoke()`          —— 直接「点」一下
        `set_active(bool)`  —— 切选中态
        `_rb_text`          —— 文字（Canvas 的 create_text 内容查不出来）
    """
    if icon not in _ICON_SHAPES:
        raise ValueError(
            "nav_button: 图标 %r 不在 _ICON_SHAPES 里（界面上会变成一块空白，"
            "而且不报任何错）。现有图标：%s"
            % (icon, ", ".join(sorted(_ICON_SHAPES))))
    st = {"text": text, "active": False, "hovering": False, "state": "normal"}

    cv = tk.Canvas(parent, width=width, height=height, bg=page,
                   highlightthickness=0, bd=0, cursor="hand2")

    def redraw(_e=None):
        try:
            cv.delete("all")
        except Exception:
            return                              # 控件正在被销毁
        on = st["active"]
        col = active_fg if on else fg
        if on:
            bg = active_fill
        else:
            bg = hover if st["hovering"] else None
        if bg:
            _draw_round_rect(cv, 0, 0, width, height, NAV_R, bg)
        if on:
            # 选中项的左侧强调竖条（Win11 导航窗格的标志性画法）。
            # ⚠️ 画在白色高亮**里面**、贴着左边缘。画在高亮外面就得给每一项
            #    都留出这一条的空白，`NAV_W` 会跟着变 —— 不值当。
            # ⚠️ 半径取 `bw//2 - 1` 而不是 `bw//2`：`_draw_round_rect` 在
            #    `2r >= 短边` 时会**静默退回画直角矩形**（那是给 r<=0 准备的兜底），
            #    `bw//2` 正好踩在边界上。和 `pill_radius` 同一个坑。
            bw = NAV_BAR_W
            bh = min(height - dp(12), dp(18))
            _draw_round_rect(cv, dp(6), (height - bh) / 2.0,
                             dp(6) + bw, (height + bh) / 2.0,
                             max(1, bw // 2 - 1), NAV_BAR)
        # 图标尺寸走 dp（这条本来就是对的）。2026-10-17 从 15 加到 17 ——
        # 徽章那一批改完之后，导航栏这几个裸线图标显得比卡片里的还小，反了。
        _draw_icon(cv, icon, col, dp(20), height / 2.0, dp(17))
        cv.create_text(dp(38), height / 2.0, text=st["text"], font=font,
                       fill=col, anchor="w")
        cv._rb_text = st["text"]

    def _click(_e=None):
        if st["state"] != "disabled":
            command()

    cv.bind("<Button-1>", _click)
    cv.bind("<Enter>", lambda e: (st.update(hovering=True), redraw()))
    cv.bind("<Leave>", lambda e: (st.update(hovering=False), redraw()))
    # 和 round_chip / round_button 同名同义：测试点导航项不用另记一套写法。
    cv.invoke = _click

    def set_active(on):
        st["active"] = bool(on)
        redraw()

    cv.set_active = set_active
    cv.is_active = lambda: st["active"]
    redraw()
    return cv


def round_check(parent, tk, text, var, command, font, page=CARD, fg=FG,
                box=dp(18), radius=dp(5), gap=dp(9), pad_y=dp(3), disabled_fg=MUTED):
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
            edge = BLUE if on else CHECK_EDGE
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
                           box * 0.75, box * 0.31, fill="#ffffff", width=2.1 * UI_SCALE,
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

    用法：`cv, body = round_panel(parent, tk, dp(19), dp(15), R_CARD)`，把内容塞进 body，
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


def icon_badge(parent, tk, name, fg, soft, box=dp(22), radius=dp(7), icon=dp(14),
               page=CARD):
    """圆角彩色小徽章：浅色圆角底 + 同色线系图标。返回那个 Canvas。

    🔴 `icon` **必须走 `dp()`**（2026-10-17 修的）。原来写的是裸的 `13.0`，
       而 `box` 是 `dp(22)` —— 150% 的屏上盒子长到 33px、里面的图形还是 13px，
       于是图标只占盒子的 39%（设计意图是 59%），看着**又小又空**。
       这类「一半跟着 DPI 变、一半不变」的错在 100% 屏上完全看不出来。

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
                 height=dp(38), radius=dp(9), width=None, pad_x=dp(30),
                 disabled_bg=BG, disabled_fg=MUTED):
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

    # ⚠️ 默认的「禁用灰」就是**页面底色本身**（`disabled_bg=BG`）——
    #    那样一禁用按钮就会跟背景融成一片，用户只会觉得界面坏了。这里只在它
    #    真的和背景分不开时才换一档更深的灰，调用方传了能用的颜色就不动它。
    #    🔴 默认值写成常量 `BG` 而不是抄一遍它的字面量：抄一遍就是一处漂移源
    #       （换配色时必漏），而且 `ui_contrast_test` 里那条「两个默认色确实是
    #       同一个值」的断言会静默变成测空气。2026-10-17 换 Fluent 时踩到过。
    disabled_bg = _pick_disabled_bg(disabled_bg, parent_bg)

    # 不给宽度就按文字量出来。Canvas 的默认宽度是 378px —— 拿它当按钮宽度
    # 会得到一个横贯整行的怪物，所以这里必须自己算。
    if width is None:
        try:
            import tkinter.font as tkfont
            width = tkfont.Font(font=font).measure(text) + pad_x
        except Exception:
            width = dp(120)

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
    """按屏幕高度挑字号 + 挑一个装得上的中文字体。返回 (字体名, 字号)。

    🔴 2026-10-16 改了三轮，把结论和过程都留在这儿（改这个数字前先读完）：

      ① **现在拿到的是物理高度**。高 DPI 感知打开之后（见 `_init_dpi`），
         本机 2560x1600 的屏不再被报成「1067」—— 以前那 1067 小于 1400，
         于是永远走小字号档。光是这一条就把 base 从 10 提到了 12。
      ② 第一版上调到「4K 16 / 2K 14 / 1200p 13 / 1080p 12」，用户反馈
         「**有点大过头了，稍微减小一些**」⇒ **整体回一档**（就是下面这版）。
      ③ 于是最终 = **老档位（`12 if sh >= 1400 else 10`）+ 1**：
         老逻辑在这台机器上给 10，现在给 13。

    ⚠️ 「稍微」的量化依据：老逻辑在能装下的屏上给 **12**，第一版给 **14**
       （+2）。用户嫌 14 大、嫌 10 小 ⇒ 取 **13**（老值 +1）。
       🔴 别再凭感觉加减 —— 要动就一次动一档，并重跑 `content_h_probe.py`。

    ⚠️ 字号是**点**（pt），Tk 会自己按 DPI 换算成像素，所以这里**不要**
       再乘 `dp()` —— 乘了就变成「DPI 缩放叠 DPI 缩放」，字会大得离谱。
       这跟 `OUTER_PAD` 那些像素常量是两套机制，别混。
    """
    try:
        sh = root.winfo_screenheight()
    except Exception:
        sh = 1080
    if sh >= 2000:          # 4K 级别
        base = 15
    elif sh >= 1440:        # 2K / 16:10 高分屏（本机 1600 走这档）
        base = 13
    elif sh >= 1200:
        base = 12
    else:                   # 1080p 及以下
        base = 11
    fam = "Microsoft YaHei UI"
    try:
        if fam not in tkfont.families():
            fam = "Microsoft YaHei"
    except Exception:
        fam = "Microsoft YaHei"
    return fam, base


def text_panel(parent, tk, fam, base, text, pad=OUTER_PAD):
    """在 `parent` 里搭一块「可滚动、可复制」的只读长文本区。返回 (外框, Text)。

    🔴 这是**唯一实现**（2026-10-05 收敛）。
       以前 `show_text_window()`（命令行 `--help` 用）和 `run_gui` 里那个
       「使用说明」按钮各写一份，两段代码几乎逐行相同。代价已经付过一次：
       2026-10-04 界面圆角化时只改了其中一份，症状是**弹窗截图字节一字没变**
       （ui_shots 的第 5 张），查了半天才发现还有第二份实现。
       以后改「看长文本的窗口」只改这里，别再抄第二份。

    `parent` 必须是**已经建好的窗口或 Toplevel** —— 本函数不建窗口，
    也不进 mainloop（调用方自己决定怎么跑）。

    ⚠️ `pad` 是四周留白。独立的说明窗口用默认值（`OUTER_PAD`）；
       而嵌进主窗口内容区当**一页**用时传 `pad=0` ——
       外层已经给过留白了，再加一层会和其他页差 14px，切页时内容会跳。
    """
    # 正文区：圆角卡片（`stretch=True` —— 它要跟着窗口长大，不是跟着内容）。
    # ⚠️ 滚动条**放在卡片外面**（兄弟节点，坐在灰底上）：
    #    Tk 的 Scrollbar 是系统原生控件，只有直角；塞在圆角卡片里，
    #    它那条直边会一直顶到卡片圆角上，看着就是「圆角破了」。
    #    挪出去之后它坐在页面底色上，谁也不碍着谁。
    wrap = tk.Frame(parent, bg=BG)
    wrap.pack(fill="both", expand=True, padx=pad, pady=(pad, 0))
    sb = tk.Scrollbar(wrap)
    sb.pack(side="right", fill="y", padx=(dp(8), dp(0)))
    body_cv, body = round_panel(wrap, tk, dp(15), dp(13), R_CARD, stretch=True)
    body_cv.pack(side="left", fill="both", expand=True)
    txt = tk.Text(body, wrap="word", font=(fam, base), bg=CARD, fg=FG,
                  relief="flat", bd=0, padx=dp(14), pady=dp(12), cursor="arrow",
                  selectbackground=BLUE_SOFT, selectforeground=FG,
                  spacing1=2, spacing3=2)
    sb.configure(command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(fill="both", expand=True)
    txt.insert("1.0", text)
    txt.configure(state="disabled")      # 只读；仍可选中、可 Ctrl+C 复制
    return wrap, txt


def text_panel_bar(parent, tk, fam, base, command, label="知道了",
                   pad=OUTER_PAD):
    """长文本窗口底部那条按钮栏（和 `text_panel` 配套）。

    ⚠️ 当**一页**用时按钮文字要改成「返回设置」而不是「知道了」——
       「知道了」是弹窗的说法，嵌在页面里会让人以为要关掉什么。
    """
    bar = tk.Frame(parent, bg=BG)
    bar.pack(fill="x", padx=pad, pady=pad)
    h = base * 2 + 16
    round_button(bar, label, command, (fam, base),
                 bg=BLUE, fg="#ffffff", hover=BLUE_DARK, active=BLUE_ACTIVE,
                 parent_bg=BG, height=h, radius=R_CTRL
                 ).pack(side="right")
    return bar


def show_text_window(title, text, width=None, height=None):
    """命令行 `--help` 用的独立说明窗口。尺寸走设计像素（见 `dp`）。"""
    width = dp(780) if width is None else width
    height = dp(640) if height is None else height
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


# ---------------------------------------------------------------------------
# 「账号中心」面板（郑航自助服务）
# ---------------------------------------------------------------------------

# 🔴 登录请求的次数上限。密码连错可能被学校风控锁号，所以**必须**有这道闸门。
#    ⚠️ 验证码错**也算一次** —— 宁可让用户关掉重开，也不拿账号去赌。
#    （服务端返回的 errmsg 能区分「验证码错」和「密码错」，见 self_service.SelfError）
SELF_LOGIN_MAX = 3

# 🔴 「保活」间隔（2026-10-17 加）。为什么需要它：
#    自助服务的会话靠一条 `JSESSIONID-BOSS-0` cookie 撑着，而它是
#    **session cookie**（没有 Max-Age / Expires）—— 客户端存多久都不算数，
#    真正的上限由**服务端**的空闲超时决定。
#    ⇒ 程序开着、但一直不去点「同步数据」的时候，会话会静默过期；
#      等用户隔了半小时再点，就又得读一次验证码 —— 这正是用户反馈的
#      「每次同步都要登录一次」。
#    ⇒ 只要**定期轻轻访问一下**，服务端的空闲计时就会被重置
#      （Tomcat 默认就是这种「有访问就续命」的滑动过期）。
#
# ⚠️ 为什么是 10 分钟：Tomcat 默认 `session-timeout` 是 30 分钟，
#    10 分钟留了 3 倍余量；再密就是白花请求（一天 144 次已经很少了）。
# ⚠️ 这个探测**只读**：打的就是 `getindexinfo.do`，和「同步数据」开头
#    那次探活是同一个调用（`SelfService.has_session()`）。
# 🔴 万一服务端是**固定寿命**（到点就死，访问也不续），保活就无效 ——
#    那时它会探到 False 然后自己停下来，不会无限空转。
SELF_KEEPALIVE_MS = 10 * 60 * 1000

# 验证码图的原始尺寸（学校那边实测 130x40）和显示倍数。
# 🔴 这几个数和**窗口宽度**绑在一起：面板宽度就是按「放下放大后的验证码」定的，
#    改放大倍数必须同时改 `PANEL_W`（下面几行就是按这个算出来的）。
# 🔴 倍数走 `dp()`：4 是「96 DPI 下的设计倍数」，150% 的屏上要放大 6 倍，
#    图才和面板一样大。不乘的话在 150% 屏上验证码会缩成一小块（更好笑的是
#    它还能读，只是费劲 —— 这类「能跑但变差」的问题最难被发现）。
CAPTCHA_W, CAPTCHA_H = 130, 40
CAPTCHA_ZOOM = dp(4)

# 卡片的内边距（和 `round_panel(pad_x=14, pad_y=12, radius=R_CARD)` 对应）。
# 放在这里是为了让下面的宽度算式**只有一处** —— 面板宽度、画布宽度、
# 验证码放不放得下，全从这几个数推出来。
_PANEL_PAD = dp(14)
_CARD_PAD_X = dp(14)

# 面板宽度：**至少**放得下放大后的验证码。
# ⚠️ 为什么要给下限：正文卡片是 `round_panel` 返回的 **Canvas**，而
#    Canvas 的 `reqwidth` 只等于它自己的 `width` 选项、**不跟内容长**
#    （和那边文档里讲的「高度得自己补」是同一个毛病）。不给下限的话，
#    窗口会停在标题行撑出来的 ~406px，验证码图右边 1/3 被**静默裁掉** ——
#    用户读不全 4 个字符，功能等于不可用，而 Tk 一声不吭。
#    （2026-10-12 靠 `selfwin_test.py` 量 `winfo_width` 才发现的。）
# ⚠️ 这个常量只给**独立窗口模式**兜底（`host=None`，测试和 `--help` 那条路）；
#    嵌进主窗口之后宽度是**现场量**的（`_body_w()`），跟它无关。
PANEL_W = CAPTCHA_W * CAPTCHA_ZOOM + 2 * OUTER_PAD + 2 * _CARD_PAD_X

# 右侧给滚动条留的**固定**列宽。
#
# 🔴 为什么是「固定预留」而不是「用到了才显示」：滚动条一出现就会从画布里
#    挤掉约 17px，而画布的宽度又决定 `holder` 的宽度 —— 于是「内容长得够不够
#    滚动」会反过来改变「图表放不放得下」。那是个**循环依赖**：内容多一点 ⇒
#    出滚动条 ⇒ 画布变窄 ⇒ 图表被裁 ⇒ 更难看。预留一个固定列就把这个环切断了：
#    画布宽度**恒定**，图表永远放得下，代价只是没滚动条时右边多一点留白。
_SCROLL_COL = dp(18)

# 图表画布的宽度 = 面板宽 - 两侧外层留白 - 滚动条列 - body 的右边距 - 卡片两侧内边距。
# （从 PANEL_W 推出来，别写死一个数 —— 写死的话改了面板宽度就对不齐。）
CHART_W = PANEL_W - 2 * OUTER_PAD - _SCROLL_COL - dp(2) - 2 * _CARD_PAD_X
CHART_H = dp(190)                 # 柱状图区高度（不含底下的日期刻度）


def self_service_available(cfg=None):
    """当前学校有没有「自助服务」（`/self/`）。

    实测确认**只有郑航**有：`202.196.169.166/self/`，和郑航的 `portalHost`
    是**同一台机器**（所以不算新增外部依赖）。
    嘉兴那套是深澜 Srun（`wlan.zju.edu.cn`），没有这个入口。
    """
    return school_key(cfg) == "zua"


class SelfServiceWindow(object):
    """「账号中心」面板：状态 / 停机 / 套餐 / 流量图表 / 上网详单 / 在线设备。

    🔴 **离线优先**（2026-10-12 用户提的改法）。打开面板**不联网、不要验证码**，
    先把本机攒下来的历史画出来；只有点「同步数据」才会取验证码 → 登录 → 拉数 →
    合并写盘 → 重画。理由：验证码得人肉读，每次想看历史都要读一遍码太蠢了。

    ⚠️ 2026-10-16 扩成「账号中心」：同步时多带回 A 档三样（上网详单 /
    停机状态 / 套餐概览，见 `self_service.account_center`）。**这三样没有本机
    缓存** —— 没同步过就看不到，因为它们每次都在变、而且体积大。
    面板因此从三块变成四块（账号 / 图表 / 详单 / 设备），
    其中「停机状态」并进账号卡（它就是一个状态字段，不值得单独一张卡）。

    ⚠️ 2026-10-16 又加了 A 档第二批三张卡（账号资料 / 费用记录 / 可购套餐，
    见 `_card_profile` / `_card_fee` / `_card_buspkg`），以及 **B 档写操作**
    （踢设备下线 / 解绑 MAC / 停机复机，见「写操作」那一节）。
    现在一共七张卡，靠正文滚动 + 高度封顶（`_autosize`）兜住。

    🔴 **写操作有一条前置条件**：它复用的是「同步过一次」之后的内存会话
    （cookie 只在内存里）。所以没同步过时，界面**一个写操作按钮都不画**
    （`_can_act`）—— 画出来只会骗人。每个动作都要二次确认，且**绝不重试**
    （规矩在 `self_actions.py` 那边，有测试钉着）。

    🔴 三条硬要求（从上一版继承）：

      1. **自带 worker 线程和自己的队列** —— 不走
         `LoginApp.state["queue"]`：那条队列的消息类型是给主窗口用的，
         塞新类型进去要改 `handle_msg`，改动面和风险都大得多。
      2. **网络请求绝不跑在 Tk 主线程**（会卡界面）。tkinter 也不是线程安全的，
         所以子线程**只往队列里放结果**，界面一律由 `after()` 轮询着改。
      3. 🔴 **cookie 只在内存**（`self_service.py` 里就是这么写的）——
         面板一销毁会话就结束。这是**故意**的：会话 cookie 等价于一次登录态。

    🔴 **2026-10-16：从「独立窗口」改成「主窗口里的一页」（`host=`）。**
       用户原话：「每加一个新功能就要加一个窗口，那用户同时看好几个功能
       不就全屏都是窗口了」。所以**没有 Toplevel 了** ——
       `host` 是主窗口内容区里的一块 Frame，本类把界面搭进去。
       ⚠️ 改成嵌入之后有三处跟着变，每一处漏掉都是静默故障：
         · `_autosize()` 不再设几何 —— 尺寸由主窗口说了算，
           再 `geometry()` 只会把主窗口拽变形；
         · 正文宽度**要现场量**（`_body_w()`），不能再拿常量 `PANEL_W` 算，
           因为主窗口可以被用户拖动改宽；
         · 「关闭」按钮不画了 —— 退出这一页靠左边的导航栏，
           画一个「关闭」只会让人以为会关掉整个程序。

    ⚠️ 一处**行为变化**：`_draw_data` 以前是「把整屏换成数据」，现在数据只是
       面板的一部分 —— 没有数据时（没同步过）只画图表卡（可能有本地历史）。
    """

    def __init__(self, root, tk, fam, base, account, password, base_url,
                 host=None):
        self.root = root
        self.tk = tk
        self.fam = fam
        self.base = base
        self.account = account
        # 🔴 `self.password` 这一行**不能少**（2026-10-12 被 selfwin_test 抓出来的）：
        #    原来只把 `password` 收进参数、没存成属性，而 `_job_login` 里写的是
        #    `self.svc.login(self.account, self.password, code)` —— 于是用户
        #    一提交验证码，子线程立刻 `AttributeError` 崩掉。表现是：
        #    界面永远停在「正在登录…」，日志里一行异常，**界面上一句提示都没有**。
        #    而且这条路是**必走**的 ⇒ 这个功能当时是「一用就废」。
        #    ⚠️ 这类「少存一个属性」的错误 py_compile 和 AST 扫描都看不见。
        self.password = password
        self.svc = self_service.SelfService(base=base_url)
        self.q = queue.Queue()
        # 队列泵的心跳（只给测试判断「泵还在不在转」，见 `_pump`）
        self.pump_beats = 0
        # 每种消息各处理了几条（只给测试用：区分「消息没送到」和「送到了但没画」）
        self.handled = {}
        # 每种消息各**入队**了几条（同上；和 `handled` 一比就知道消息丢在哪一段）
        self.puts = {}
        self.puts_dropped = 0     # 面板已关、结果被丢掉的消息条数
        self.crashes = 0          # 后台任务抛非预期异常的次数（同上，只给测试）
        self.tries = 0            # 已发出的登录请求次数（防锁号闸门）
        self.closed = False
        # 嵌入模式：`host` 非空 ⇒ 界面搭进那块 Frame，不建 Toplevel。
        # 🔴 这个标志决定了好几处分支（几何 / 关闭按钮 / 留白），别在别处
        #    用「`self.win` 是不是 Toplevel」去猜 —— 以后加一种容器就全错。
        self.embedded = host is not None
        # 正文宽度（列宽 / wraplength 的唯一来源）。先给单列时代的默认值兜底，
        # `_render()` 第一句就按实际宽度重算。
        self.cw = CHART_W
        # 分列状态：`cols` 非空 = 正文分成了几列，`col_i` = 下一张卡放哪一列。
        self.cols = []
        self.col_i = 0
        self._laid_w = -1         # 上次排版时量到的正文宽度（防重复重排）
        self._relayout_job = None
        # 🔴 屏上是不是**临时视图**（忙碌提示 / 验证码 / 失败提示）。
        #    这些视图不是从 `_render()` 画的，所以「因为窗口宽度变了而重排」
        #    会把它们冲掉 —— 用户正低头看验证码图，界面自己变回数据页，
        #    而且**一个字都不提示**。见 `_relayout`。
        self.view_locked = False
        # 🔴 验证码图片**必须留引用**（存成属性）—— 只放在局部变量里会被 GC 掉，
        #    症状是 Label 一片空白，而且**不报任何错**。这是 tkinter 的经典坑。
        self._cap_small = None
        self._cap_big = None
        self._entry = None

        # --- 本地历史（只读盘、不联网） ---
        self.store = {}
        self.store_why = ""       # 读库没读成时的原因（记日志用）
        # --- 联网同步到的数据；没同步过就是 None ---
        self.data = None
        self.syncing = False
        # --- 图表视图状态 ---
        self.view = "day"         # "day"（按天）/ "month"（按月）
        self.ym = ""              # 按天视图当前看的是哪个月 "YYYY-MM"
        # 确认框。测试里替换成 `lambda *a: True`，免得模态框把自动化挂住。
        self.ask = None
        # 要一行文字的输入框（停机原因）。测试里同样替换掉。
        self.ask_text = None
        # --- 写操作（B 档，2026-10-16 加）---
        # 🔴 `actions` **懒创建**：它复用的就是 `self.svc` 里那个内存会话
        #    （cookie 只活在内存里，窗口一关就没）。所以**没同步过就没有会话**，
        #    界面也不该出现任何写操作按钮（见 `_can_act`）。
        self.actions = None
        self.acting = ""          # 正在进行的写操作名（空串 = 空闲；防连点）
        # --- 会话保活（2026-10-17 加，见 SELF_KEEPALIVE_MS 上面的说明） ---
        # `keepalive_job` = `after()` 的句柄（None = 没在跑）
        # `session_alive` = None 未知 / True 有效 / False 已过期
        #    ⚠️ 三态是**故意**的：把「没探过」当成「有效」或「过期」都是在
        #       把「不知道」说成事实（红线 50）。
        self.keepalive_job = None
        self.session_alive = None
        # 上次存下来的登录态试过没有（**一次就够**，见 `_try_restore_session`）
        self.session_restored = False
        # 🔴 写操作成功后会**自动刷新**一次。刷新走的是 `_on_synced`，
        #    那里默认会把 `self.note` 覆盖成「已同步：…」—— 那样用户刚看到的
        #    「已下线」就被盖掉了，等于不知道刚才那一下成没成。
        #    这个标志让它**追加**而不是覆盖。
        self.keep_note = False
        # 上一次操作的结果提示（同步完了 / 清完了），画在正文末尾
        self.note = ""

        # 嵌入模式下外层页面已经给过留白了，这里不再叠加 ——
        # 否则从「设置」页切过来时内容会整体跳 14px。
        pad = 0 if self.embedded else OUTER_PAD

        if self.embedded:
            # 一页，不是一窗。**没有 title/transient/iconbitmap/protocol** ——
            # 那些都是 Toplevel 才有的东西，Frame 上调会 AttributeError。
            # ⚠️ Frame 必须**自己 pack**：Toplevel 建出来就自动显示，
            #    Frame 不 pack 的话宽度永远是 1 —— 而 `_body_w()` 量到的就是它，
            #    于是排版永远走「量不到、用兜底值」那条路（两列失效、换行宽度错），
            #    并且**不报任何错**。
            self.win = tk.Frame(host, bg=BG)
            self.win.pack(fill="both", expand=True)
        else:
            self.win = tk.Toplevel(root)
            self.win.title("账号中心 · %s · %s" % (account, APP_TITLE))
            # 窗口底色 = **页面底色**（不是卡片底色）—— 正文是圆角卡片，
            # 同色的话圆角就看不出来了。
            self.win.transient(root)          # 跟随主窗口，不单独占任务栏项
            self.win.resizable(False, False)  # 尺寸由内容算（见 _autosize）
            ico = icon_path()
            if ico:
                try:
                    self.win.iconbitmap(default=ico)
                except Exception:
                    pass
            self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.configure(bg=BG)

        # --- 顶部：标题 + 账号 + 「同步数据」 ---
        self.head = tk.Frame(self.win, bg=BG)
        self.head.pack(fill="x", padx=pad, pady=(2 if self.embedded else OUTER_PAD, 6))
        tk.Label(self.head, text="账号中心", bg=BG, fg=FG,
                 font=(fam, base + 4, "bold")).pack(side="left")
        # 🔴 账号**必须显示出来**：这个面板看的是「界面上那个账号」的数据，
        #    而本机历史是按账号存的 —— 不写清楚是哪个账号，用户会以为看的是
        #    另一个（而且两边数据都「看着正常」，看不出错）。
        round_chip(self.head, tk, account, (fam, base - 2), SUB, NEUTRAL_SOFT,
                   page=BG).pack(side="left", padx=(dp(10), dp(0)))
        round_chip(self.head, tk, "学校自助服务 · 只读", (fam, base - 2), SUB,
                   NEUTRAL_SOFT, page=BG).pack(side="left", padx=(dp(6), dp(0)))
        h = base * 2 + 14
        self.btn_sync = round_button(
            self.head, "同步数据", self.start_sync, (fam, base),
            bg=BLUE, fg="#ffffff", hover=BLUE_DARK, active=BLUE_ACTIVE,
            parent_bg=BG, height=h, radius=R_CTRL)
        self.btn_sync.pack(side="right")

        # --- 中间：可滚动正文 ---
        # ⚠️ 面板现在有三块（账号 / 图表 / 设备），矮屏上一定放不下，
        #    所以必须能滚 —— 主窗口也是这么做的。
        outer = tk.Frame(self.win, bg=BG)
        outer.pack(fill="both", expand=True, padx=pad)
        # 滚动条坐在一个**宽度固定**的列里（`_SCROLL_COL`），不用时只是空着。
        # 这样画布宽度恒定，图表不会被「滚动条出现/消失」挤到裁掉 —— 见常量那边的说明。
        self.scroll_col = tk.Frame(outer, bg=BG, width=_SCROLL_COL)
        self.scroll_col.pack(side="right", fill="y")
        self.scroll_col.pack_propagate(False)
        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        self.vsb = tk.Scrollbar(self.scroll_col, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.holder = tk.Frame(self.canvas, bg=BG)
        self.body = tk.Frame(self.holder, bg=BG)
        self.body.pack(fill="both", expand=True, padx=(dp(0), dp(2)))
        self._holder_id = self.canvas.create_window((0, 0), window=self.holder,
                                                    anchor="nw")
        self.holder.bind("<Configure>", self._on_holder)
        # 🔴 让 holder 始终和 canvas 一样宽（主窗口也有一句一模一样的）。
        #    **漏了这句的后果是内容被静默裁掉**：图表画布是 520px，而 holder
        #    会停在卡片 Canvas 的默认宽度（378px）—— 2026-10-12 实测，
        #    一个月 6 天的柱子**只画出来 4 根**，右边两根直接没了，
        #    而且不报任何错。（和「Canvas 的 reqwidth 不跟内容长」是同一个家族。）
        #
        # ⚠️ 2026-10-16 起这件事还多了一层：**正文宽度同时是列宽和 wraplength
        #    的来源**。主窗口能被用户拖动改宽，所以宽度一变，换行位置就过时了 ——
        #    留在原地的后果是文字被静默裁掉（Label 的 reqwidth 大于列宽时，
        #    列是固定宽的，多出来的部分直接看不见）。所以这里顺手安排一次重排。
        self.canvas.bind("<Configure>", self._on_canvas)

        # --- 底部 ---
        self.foot = tk.Frame(self.win, bg=BG)
        self.foot.pack(fill="x", padx=pad, pady=(dp(6), dp(0)) if self.embedded
                       else OUTER_PAD)
        h = base * 2 + 16
        # 🔴 嵌入模式下**不画「关闭」**：退出这一页靠左边的导航栏。
        #    画一个「关闭」会让人以为「关掉整个程序」（主窗口那个 X 才是）。
        if not self.embedded:
            round_button(self.foot, "关闭", self.close, (fam, base),
                         bg=BTN_SOFT_ON_BG, fg=BTN_FG, hover=BTN_ACTIVE,
                         active=BTN_PRESS, parent_bg=BG, height=h,
                         radius=R_CTRL).pack(side="right")
        # 「清掉本机历史」：**故意和主界面的「清除本机保存的账号密码」分开** ——
        # 那个不删流量历史（用户要的就是「以后不登录也能看」），所以这里给一个
        # 单独的入口，让想删的人删得掉。
        round_chip(self.foot, tk, "清掉本机历史", (fam, base - 1), SUB,
                   NEUTRAL_SOFT, page=BG, height=h, hover=BTN_ACTIVE,
                   command=self.clear_history).pack(side="right", padx=(dp(0), dp(10)))

        self.win.after(80, self._pump)
        self._load_local()
        self._render()
        # ⚠️ 定位只做**这一次**（在第一次渲染之后）—— 放在 `_render()` 里的话，
        #    每次同步完窗口都会跳一下，很烦。嵌入模式没有「位置」可摆。
        if not self.embedded:
            self._place()

    # ------------------------------------------------------------ 窗口

    def _on_canvas(self, e):
        """画布宽度变了：撑开 holder，必要时**重排一次**。

        ⚠️ 拖动主窗口时 `<Configure>` 会连发几十次，每次都重画会把界面拖卡，
           所以①宽度差得不够多就跳过；②真的要重排也延后 200ms，
           等手停下来再做（`_relayout` 里会再量一次当前宽度）。
        """
        try:
            self.canvas.itemconfigure(self._holder_id, width=e.width)
        except Exception:
            pass
        # 🔴 第一次 `<Configure>` 只是**建立基线**，不是「宽度真的变过」。
        #    拿它去和初始的 -1 比会算出一个巨大的差值，白白安排一次重排 ——
        #    而那一次重排会把正在显示的**验证码界面整个冲掉**
        #    （2026-10-16 实测：点「确定」报「验证码不对」之后约 200ms，
        #      界面自己变回了数据视图，用户以为那个按钮没反应）。
        if self._laid_w < 0:
            self._laid_w = e.width
            return
        if abs(e.width - self._laid_w) < dp(24):
            return
        if self._relayout_job is not None:
            try:
                self.win.after_cancel(self._relayout_job)
            except Exception:
                pass
        self._relayout_job = self.win.after(200, self._relayout)

    def _relayout(self):
        """按新宽度重画一遍（换行宽度变了，旧的 wraplength 会裁字）。"""
        self._relayout_job = None
        if self.closed:
            return
        if self.view_locked:
            # 🔴 现在屏上是**验证码 / 失败提示**这类临时视图。它们不是从
            #    `_render()` 画出来的，重排会把用户刚敲进去的验证码冲掉。
            #    把基线复位成 -1：等 `_render()` 下次跑的时候，宽度自然生效。
            self._laid_w = -1
            return
        self._render()

    def _on_holder(self, _e=None):
        """正文大小变了 ⇒ 更新滚动范围。

        ⚠️ 滚动条的**显示/隐藏交给 `_on_scroll()`**，这里只管范围 ——
           两处都管的话会互相打架（这边刚藏起来、那边又显示出来），
           表现是滚动条一闪一闪。
        """
        try:
            self.canvas.configure(scrollregion=(0, 0, 0,
                                                self.holder.winfo_reqheight()))
        except Exception:
            pass

    def _on_scroll(self, first, last):
        """滚动条回调。**内容装得下时把滚动条藏起来** —— 一直挂着一根空滚动条
        看着像界面没做完（主窗口那边也是按需显示的）。"""
        try:
            if float(first) <= 0.0 and float(last) >= 1.0:
                if self.vsb.winfo_ismapped():
                    self.vsb.pack_forget()
            elif not self.vsb.winfo_ismapped():
                self.vsb.pack(fill="both", expand=True)
            self.vsb.set(first, last)
        except Exception:
            pass

    def _autosize(self):
        """按内容算窗口高度（宽度固定 = `PANEL_W`）。

        ⚠️ 高度**必须封顶**：内容比屏幕还高时窗口会伸出屏幕外，
          底下的「关闭」按钮就点不到了（而那正是唯一的退出方式）。
          封顶之后正文自己会滚。

        🔴 嵌入模式下**只更新滚动范围，绝不碰几何** ——
           尺寸是主窗口的，这里再 `geometry()` 会把主窗口拽变形
           （而且 Frame 根本没有 geometry 方法，会直接抛异常）。
        """
        try:
            self.win.update_idletasks()
            if not self.embedded:
                h = (self.head.winfo_reqheight() + self.foot.winfo_reqheight()
                     + self.holder.winfo_reqheight() + 3 * OUTER_PAD + dp(12))
                limit = int(self.win.winfo_screenheight() * 0.86)
                h = max(dp(320), min(h, limit))
                self.win.geometry("%dx%d" % (PANEL_W, h))
            self._on_holder()
        except Exception:
            log("账号中心面板定尺寸失败:\n%s" % traceback.format_exc())

    def _place(self):
        """把窗口摆在主窗口附近偏中间的位置（**只在独立窗口模式下有意义**）。"""
        if self.embedded:
            return
        try:
            self.win.update_idletasks()
            x = self.root.winfo_rootx() + 40
            y = self.root.winfo_rooty() + 30
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            w = self.win.winfo_width() or PANEL_W
            hh = self.win.winfo_height() or 400
            # 别让它跑出屏幕（右上角那块尤其容易）
            x = max(0, min(x, sw - w - 8))
            y = max(0, min(y, sh - hh - 40))
            self.win.geometry("+%d+%d" % (x, y))
            self.win.lift()
        except Exception:
            # 定位失败不是致命问题（窗口会用系统默认位置），但要留痕 ——
            # 万一定位算错把窗口甩到屏幕外，这条日志就是唯一线索。
            log("账号中心面板定位失败:\n%s" % traceback.format_exc())

    def close(self):
        """收摊。置 `closed` 之后子线程再往队列里放东西也没人处理了。

        ⚠️ **不主动清空 `self.svc`** —— 它持有内存里的会话 cookie，
        对象没人引用时跟着被回收，正是我们想要的（不落盘、不显式持久化）。

        ⚠️ 嵌入模式下 `self.win` 是主窗口里的一块 Frame，
        `destroy()` 只会拆掉这一页的控件，**不会关掉主窗口** —— 这正是要的。
        """
        if self.closed:
            return
        self.closed = True
        # 先停保活：它挂着 `after()`，窗口一销毁那个回调就是野指针
        self._keepalive_stop()
        if self._relayout_job is not None:
            try:
                self.win.after_cancel(self._relayout_job)
            except Exception:
                pass
            self._relayout_job = None
        try:
            self.win.destroy()
        except Exception:
            log("关闭账号中心面板异常:\n%s" % traceback.format_exc())

    # ------------------------------------------------------------ 线程 / 队列

    def _run(self, fn):
        """起一个后台线程跑 `fn`。

        🔴 **兜底 catch 是必须的**（2026-10-12 加）：`_job_*` 只接 `SelfError`，
           别的异常（比如少存了一个属性 → `AttributeError`）会直接从线程里漏出去。
           那种情况下**界面永远停在「正在登录…」**，用户看到的是「点了没反应」，
           日志里才有线索 —— 这是最糟的一种失败：既不报错也不结束。
           现在一律变成界面上的提示，并且明说「已记日志」。
        """
        def _wrapped():
            try:
                fn()
            except Exception as e:
                # 计数只给测试用：区分「压根没进兜底」和「进了兜底但卡在 log()」
                self.crashes += 1
                # 🔴 **先把结果交给界面，再写日志**（2026-10-17 调过来的）。
                #    原来的顺序是 log() 在前，而 `log()` 每次都会
                #    `open/write/close` 一次文件 —— 单线程实测中位 14 ms、
                #    20 个线程抢的时候单次能到 1 秒以上（Windows 上开关文件
                #    加上杀毒软件扫描就是这个量级）。
                #    ⇒ 写日志是**给排查用的**，凭什么让用户多等；
                #      而且它一慢，界面就停在「正在登录…」不动。
                #    ⚠️ 这条不是猜的：`_lab/probe_log_slow.py` 量出来的。
                self._put("fail", "内部错误（已记日志）：%s: %s"
                          % (type(e).__name__, e))
                log("账号中心面板后台任务异常:\n%s" % traceback.format_exc())

        threading.Thread(target=_wrapped, daemon=True).start()

    def _put(self, kind, payload=None):
        if self.closed:
            # 面板已经关了，结果没人要 —— 但**要留个数**：
            # 「消息被丢掉了」和「消息压根没发出来」在界面上长得一样。
            self.puts_dropped += 1
            return
        # 计数只给测试用（和 `handled` 一比就知道消息丢在入队还是出队那一段）
        self.puts[kind] = self.puts.get(kind, 0) + 1
        self.q.put((kind, payload))

    def _pump(self):
        """主线程里轮询队列。**所有界面改动都只在这里发生。**"""
        # 心跳计数：只给测试用（`selfwin_test` 靠它判断「泵到底还在不在转」）。
        # 🔴 没有这个数就只能看到「界面上没有某段文字」，而泵停掉和
        #    消息发丢在界面上**长得一模一样**，根本分不开。
        self.pump_beats += 1
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self.handled[kind] = self.handled.get(kind, 0) + 1
                try:
                    self._handle(kind, payload)
                except Exception:
                    # 单条消息处理失败不能中断整批，也不能让窗口卡在"忙碌"
                    log("账号中心面板处理消息异常:\n%s" % traceback.format_exc())
        except queue.Empty:
            pass
        if not self.closed:
            try:
                self.win.after(80, self._pump)
            except Exception:
                # ⚠️ 续排失败 = **泵彻底停了**，界面从此再也不响应任何结果。
                #    以前这里没有 try，异常直接从 Tk 回调里漏出去，
                #    表现只是「界面永远停在正在登录…」，一行线索都没有。
                log("账号中心面板 _pump 续排失败:\n%s" % traceback.format_exc())

    def _handle(self, kind, payload):
        if kind == "load":
            self._busy_note("正在读取数据…")
            self._run(self._job_load)
        elif kind == "captcha":
            self._draw_captcha(payload)
        elif kind == "captcha_err":
            # 🔴 `note=True` 是**显式**的，不能靠 `payload` 顺手当标志位。
            #    2026-10-12 读代码时发现的 bug：原来写的是 `note=payload`，
            #    而「验证码错了、但新图没取到」时 payload 就是 `None` ——
            #    于是提示**整个消失**，用户只看到一张新验证码（或者一片空白），
            #    完全不知道刚才那次为什么失败，还以为是自己点错了。
            self._draw_captcha(payload, note=True)
        elif kind == "login_err":
            self._draw_login_err(payload)
        elif kind == "data":
            self._on_synced(payload)
        elif kind == "fail":
            self._draw_fail(payload)
        # --- 会话保活（后台探测的回话）---
        elif kind == "keepalive":
            self._on_keepalive(payload)
        # --- 写操作（B 档）---
        elif kind == "act_ok":
            self._on_action_ok(payload)
        elif kind == "act_err":
            self._on_action_err(payload)

    # ------------------------------------------------------------ 后台任务

    def _job_session(self):
        """先看会话还在不在。在就直接取数，不在就取验证码。"""
        try:
            self._try_restore_session()
            if self.svc.has_session(self.account):
                self._put("load")
            else:
                # 🔴 服务端明确说「没会话了」⇒ 磁盘上那份也一定是废的，删掉。
                #    不删的话下次还会白载入一次、白探一次。
                clear_self_session(self.account)
                self._job_captcha()
        except self_service.SelfError as e:
            self._put("fail", e.msg)

    def _try_restore_session(self):
        """把上次存的登录态载回来。**每个面板只做一次。**

        🔴 载回来**不等于**能用 —— 载完必须照常往下走 `has_session()`，
           探不通就退回验证码。存档是会过期的，我们只掌握「多久以前存的」，
           不掌握「服务端还认不认」。
        """
        if self.session_restored:
            return
        self.session_restored = True
        pairs, why = read_self_session(self.account, self.svc.base)
        if not pairs:
            # 「没存过」是正常情况，不值得记日志；其余原因要留痕，
            # 否则「明明存过却没生效」时完全没有线索。
            if why and why != "没存过":
                log("账号中心：没有复用上次的登录态（%s）" % why)
            return
        n = self.svc.import_cookies(pairs)
        log("账号中心：载入了上次的登录态（%d 条 cookie），先探一次再决定" % n)

    def _save_session(self):
        """把当前会话加密存盘，下次打开就不用再读验证码。

        ⚠️ 只在**服务端刚证明过这个会话有效**之后调（见 `_on_synced`）。
           别在 `login()` 刚返回时就存 —— 那时还没验过。
        """
        try:
            ok, why = save_self_session(self.account, self.svc.base,
                                        self.svc.export_cookies())
            if not ok and why:
                log("账号中心：登录态没存下来（%s）" % why)
        except Exception:
            log("账号中心：保存登录态异常:\n%s" % traceback.format_exc())

    def _job_captcha(self):
        try:
            png, logged = self.svc.fetch_captcha()
        except self_service.SelfError as e:
            self._put("fail", e.msg)
            return
        if logged:
            self._put("load")
            return
        self._put("captcha", png)

    def _job_load(self):
        try:
            # ⚠️ `today=` 显式传进去：让「今天算哪一天」和写盘用的是同一个日期，
            #    也免得 `parse_days` 去读时钟（那样测试就不是确定性的了）。
            #
            # 🔴 用 `full()` 而不是 `load()`（2026-10-16）：多带回 A 档的
            #    上网详单 / 停机状态 / 套餐概览。`full()` 内部**对 A 档容错** ——
            #    详单接口抽风时主数据照常返回，原因落在 `center_error` 里
            #    （界面会显式写出来，不会假装「这个账号没有上网记录」）。
            self._put("data", self.svc.full(self.account, today=self._today()))
        except self_service.SelfError as e:
            if e.kind == "session":
                # 会话在取数途中过期了 —— 退回验证码流程，不算失败
                clear_self_session(self.account)
                self._job_captcha()
            else:
                self._put("fail", e.msg)

    def _job_login(self, code):
        self.tries += 1
        try:
            self.svc.login(self.account, self.password, code)
        except self_service.SelfError as e:
            if e.kind == "captcha":
                # 验证码错 ⇒ **安全的重试**：换一张就行
                try:
                    png, _ = self.svc.fetch_captcha()
                except self_service.SelfError:
                    png = None
                self._put("captcha_err", png)
            elif e.kind == "login":
                # 🔴 **不许重试**（可能是密码错 / 账号不存在 / 被锁）
                self._put("login_err", e.msg)
            else:
                self._put("fail", e.msg)
            return
        self._job_load()

    def _today(self):
        """今天算哪一天（`YYYY-MM-DD`）。抽出来是为了测试能替换掉。"""
        return datetime.now().strftime("%Y-%m-%d")

    # ------------------------------------------------------------ 会话保活

    def _keepalive_start(self):
        """开始定期轻探会话。已经有会话（刚同步成功）才调这个。

        🔴 为什么不能只靠「用户下次点同步时再说」：那时候会话已经死了，
           用户就必须重新读验证码 —— 保活的意义就是**让它别死**。
        """
        if self.closed or self.keepalive_job is not None:
            return
        self.session_alive = True
        self._keepalive_tick()

    def _keepalive_tick(self):
        """安排下一次探测。"""
        if self.closed:
            return
        try:
            self.keepalive_job = self.win.after(SELF_KEEPALIVE_MS,
                                                self._keepalive_run)
        except Exception:
            # 窗口已经没了（嵌入模式下主窗口被关）⇒ 静默收摊，别刷日志
            self.keepalive_job = None

    def _keepalive_run(self):
        """到点了：丢给后台线程去探（`has_session()` 是阻塞的网络请求）。"""
        self.keepalive_job = None
        if self.closed:
            return
        if self.syncing or self.acting:
            # 用户正在同步 / 正在写操作 ⇒ 这一轮跳过。
            # ⚠️ 不是「失败」，所以**不能**停掉保活，也不能记成会话过期。
            self._keepalive_tick()
            return
        threading.Thread(target=self._keepalive_work, daemon=True).start()

    def _keepalive_work(self):
        """后台：探一下会话还在不在。结果一律回主线程处理。"""
        try:
            alive = self.svc.has_session(self.account)
        except Exception:
            # 网络不通 / 服务器抽风 —— 那是「不知道」，不是「会话没了」。
            # 🔴 这里绝不能返回 False：那会让界面去弹验证码，
            #    而用户可能只是在电梯里没信号。
            alive = None
        self._put("keepalive", alive)

    def _on_keepalive(self, alive):
        """主线程：根据探测结果决定「继续探」还是「收摊」。

        🔴 **不改界面、不重画** —— 用户可能正在看图表，被一次后台探测
           把界面刷一遍是最讨厌的那种「自己会动」。
           会话真的过期了也不弹验证码：那是下次点「同步数据」时才该发生的事。
        """
        if alive is True:
            self.session_alive = True
            self._keepalive_tick()
            return
        if alive is False:
            self.session_alive = False
            self.keepalive_job = None
            # 服务端不认了 ⇒ 磁盘上那份也没用了
            clear_self_session(self.account)
            log("账号中心：会话已过期（保活没拦住），下次同步需要重新验证码")
            return
        # alive is None ⇒ 不知道 ⇒ 下一轮再试，别下结论
        self._keepalive_tick()

    def _keepalive_stop(self):
        """停掉保活（关面板时调）。"""
        if self.keepalive_job is not None:
            try:
                self.win.after_cancel(self.keepalive_job)
            except Exception:
                pass
            self.keepalive_job = None

    # ------------------------------------------------------------ 同步流程

    def start_sync(self):
        """点「同步数据」：这时候才去联网、才要验证码。"""
        if self.syncing:
            return
        self.syncing = True
        self._set_sync_button()
        self._busy_note("正在连接学校自助服务…")
        self._run(self._job_session)

    def _on_synced(self, payload):
        """同步成功：合并进本地历史 → 写盘 → 重画。

        🔴 合并规则是**用户明确要求的**（2026-10-12）：同一天数据不同就
        远端覆盖本地，本地没有的日期直接存 —— 具体在 `self_history.merge()`。
        """
        self.data = payload
        days = payload.get("days") or []
        today = (payload.get("summary") or {}).get("today")
        added, updated = self_history.merge(self.store, self.account, days, today)
        self_history.mark_updated(self.store, self.account,
                                  datetime.now().strftime("%Y-%m-%d %H:%M"))
        ok, why = self_history.save(SELF_FLOW_FILE, self.store)
        if not ok:
            log("本地流量历史写盘失败：%s" % why)
        sync_note = ("已同步：新增 %d 天、更新 %d 天%s"
                     % (added, updated, "" if ok else "（但本地存不下，见日志）"))
        # 🔴 写操作成功后会**自动刷新**一次，刷新走的就是这条路。
        #    直接覆盖的话，用户刚看到的「已下线」会被同步提示盖掉 ——
        #    那就等于**不知道刚才那一下成没成**。所以这里追加。
        if self.keep_note and self.note:
            self.note = self.note + "；" + sync_note
        else:
            self.note = sync_note
        self.keep_note = False
        self.syncing = False
        # 🔴 到这一步说明「服务端刚认可了这个会话」⇒ 这时候存才是可信的。
        #    存下来之后，「关掉程序再打开」也不用再读验证码。
        self._save_session()
        # 🔴 同步成功 ⇒ 手上有一个**有效会话** ⇒ 把它保住，别让它闲到过期。
        #    放在 `syncing = False` 之后：保活线程里要读 `self.syncing`。
        self._keepalive_start()
        self._render()

    # ------------------------------------------------------------ 本地历史

    def _load_local(self):
        """只读本地历史（**不联网**）。"""
        self.store, self.store_why = self_history.load(SELF_FLOW_FILE)
        if self.store_why:
            log("账号中心面板：%s" % self.store_why)

    def clear_history(self):
        """清掉**当前账号**的本机历史（只删这个账号，不动别人的）。"""
        if not self_history.days_of(self.store, self.account):
            self.note = "本机没有 %s 的流量历史" % self.account
            self._render()
            return
        if not self._ask_yes_no("清掉本机保存的 %s 的流量历史吗？\n"
                                "（清掉之后就只剩学校那边最近 30 天的数据了）"
                                % self.account):
            return
        n = self_history.clear(self.store, self.account)
        ok, why = self_history.save(SELF_FLOW_FILE, self.store)
        if not ok:
            log("本地流量历史写盘失败：%s" % why)
        self.note = "已清掉 %s 的本机历史（%d 个账号）" % (self.account, n)
        self._render()

    def _ask_yes_no(self, msg):
        """确认框。`self.ask` 被测试替换掉时就不弹模态框了。

        ⚠️ 模态框会把无人值守的自动化**挂住**（项目里 `--guitest` 吃过这个亏），
           所以凡是新增模态框，都要留一条能被替换掉的路。
        """
        if self.ask is not None:
            return bool(self.ask(msg))
        try:
            from tkinter import messagebox
            return bool(messagebox.askyesno(APP_TITLE, msg, parent=self.win))
        except Exception:
            log("账号中心面板确认框失败:\n%s" % traceback.format_exc())
            return False

    # ------------------------------------------------------------ 写操作（B 档）
    #
    # 🔴 这一节是**唯一**能触发不可逆动作的地方。三条铁律（对应
    #    `self_actions.py` 的模块规矩，那边也有测试钉着）：
    #      ① 每个动作**必须先二次确认**（`_do_action(confirm=...)`）。
    #      ② **绝不重试** —— `fn` 只跑一次。写操作重试 = 做第二遍。
    #      ③ 网络在**子线程**里跑（复用 `_run`），界面不卡。
    #
    # ⚠️ 还有一条**界面**上的规矩：所有按钮都只在 `_can_act()` 为真时画 ——
    #    没同步过（没会话）时点了也是白点，画出来只会骗人。

    def _acts(self):
        """拿写操作句柄（复用 `self.svc` 已登录的会话）。**没同步过就返回 None。**"""
        if self.data is None:
            return None
        if self.actions is None:
            self.actions = self_actions.SelfActions(self.svc)
        return self.actions

    def _can_act(self):
        """现在能不能画写操作按钮：**同步过**（有会话）且**没有别的写操作在跑**。"""
        return self.data is not None and not self.acting

    def _ask_text_input(self, title, prompt, initial=""):
        """要一行文字（停机原因）。`self.ask_text` 被测试替换时就不弹模态框。

        ⚠️ 和 `_ask_yes_no` 同一个理由：模态框会把无人值守的自动化**挂住**。
        """
        if self.ask_text is not None:
            return self.ask_text(title, prompt, initial)
        try:
            from tkinter import simpledialog
            return simpledialog.askstring(title, prompt, initialvalue=initial,
                                          parent=self.win)
        except Exception:
            log("账号中心面板输入框失败:\n%s" % traceback.format_exc())
            return None

    def _do_action(self, label, fn, confirm=None):
        """写操作的**唯一入口**。`fn` 会在子线程里跑，且**只跑一次**。"""
        if self.acting:
            return                      # 防连点：上一个还没回来
        if confirm and not self._ask_yes_no(confirm):
            return
        self.acting = label
        self.note = "正在%s…" % label
        self._render()
        self._run(lambda: self._job_action(label, fn))

    def _job_action(self, label, fn):
        """子线程：跑一次写操作，结果放回队列。**绝不重试。**"""
        try:
            r = fn()
            self._put("act_ok", (label, r))
        except self_actions.ActionError as e:
            self._put("act_err", (label, e.kind, e.msg))
        except Exception as e:
            # `_run` 里也有一层兜底，但这里自己接一下好带上**操作名** ——
            # 用户看到的「下线失败：…」比光一句「内部错误」有用得多。
            log("账号中心面板写操作异常:\n%s" % traceback.format_exc())
            self._put("act_err", (label, "other",
                                  "内部错误（已记日志）：%s: %s"
                                  % (type(e).__name__, e)))

    def _on_action_ok(self, payload):
        """写操作成功 ⇒ **自动刷新一次**。

        🔴 为什么必须刷新：不刷新的话界面上还写着「这台设备在线 / 状态正常」，
           用户会以为没生效，**再点一次** —— 而那一下是在重复做同一件事。
        """
        label, r = payload
        self.acting = ""
        msg = (r or {}).get("msg") or ""
        self.note = "已%s%s" % (label, ("（%s）" % msg) if msg else "")
        self.keep_note = True           # 让刷新那一步**追加**而不是覆盖这条
        self.start_sync()

    def _on_action_err(self, payload):
        """写操作失败。🔴 **按 `kind` 说人话** —— 尤其是要分清
        「不知道成没成」（`net`）和「学校明确拒绝」（`denied`）：
        只有前者可以再点一次，后者再点就是在骚扰学校服务器。"""
        label, kind, msg = payload
        self.acting = ""
        if kind == "net":
            tail = "（网络没通。「这次到底成没成」不确定 —— 稍后同步一次看状态。）"
        elif kind == "session":
            tail = "（登录状态过期了 —— 点「同步数据」重新登录再试。）"
        elif kind == "denied":
            tail = "（学校那边明确拒绝了，「不要再点」。）"
        elif kind == "refused":
            tail = ""
        else:
            tail = ""
        self.note = "%s失败：%s%s" % (label, msg, tail)
        self.keep_note = False
        self._render()

    # -------------------------------------------------- 三个具体动作的入口

    def _kick(self, dev):
        """把某台在线设备踢下线。**不可逆，它立刻断网。**"""
        act = self._acts()
        if act is None:
            self.note = "要先点一次「同步数据」登录，才能下线设备。"
            self._render()
            return
        who = "%s（%s）" % (dev.get("os") or "未知设备", dev.get("ip") or "-")

        def job():
            # 🔴 **踢之前现取一次**在线清单。两个理由：
            #    ① 踢人要的 `billingId` / `serverIp` 是**每次登录**才有的，
            #       而界面上那份（`load()` 带回来的）**故意没有**这两个字段
            #       —— `self_service.online()` 就是为了「结构上凑不出踢人请求」。
            #    ② 顺带解决竞态：用户点的时候那台可能已经下线了。
            targets = act.online_targets(self.account)
            t = None
            for x in targets:
                if (x.get("ip") == dev.get("ip")
                        and x.get("mac") == dev.get("mac")):
                    t = x
                    break
            if t is None:
                raise self_actions.ActionError(
                    "other", "这台设备已经不在线了（点「同步数据」刷新一下）")
            return act.kick(self.account, t)

        self._do_action(
            "下线 %s" % who, job,
            confirm="把 %s 下线吗？\n\n它「立刻断网」，要重新认证才能上网。\n"
                    "如果这台就是你现在用的，你自己也会断。" % who)

    def _unbind(self, b):
        """解绑一个 MAC 槽位。**不可逆，那台设备要重新认证。**"""
        act = self._acts()
        if act is None:
            self.note = "要先点一次「同步数据」登录，才能解绑设备。"
            self._render()
            return
        mac = b.get("mac") or "-"
        self._do_action(
            "解绑 %s" % mac,
            lambda: act.clear_mac(self.account, b.get("clear_type")),
            confirm="把 %s 从绑定里解掉吗？\n\n"
                    "那台设备要重新认证才能上网。" % mac)

    def _stop_now(self):
        """立即停机。**不可逆（要复机才能上网），而且会立刻断网。**"""
        act = self._acts()
        if act is None:
            self.note = "要先点一次「同步数据」登录，才能停机。"
            self._render()
            return
        reason = self._ask_text_input(
            "停机", "停机原因（学校那边会记下来）", initial="本人在软件里主动停机")
        if reason is None:              # 用户取消了
            return
        reason = reason.strip()
        if not reason:
            # 前端自己也校验这条（`shutdownReason` 不能为空）—— 空着发过去
            # 学校多半会拒，而且报错信息看不出原因。
            self.note = "停机原因不能为空。"
            self._render()
            return
        self._do_action(
            "停机", lambda: act.stop_now(self.account, reason),
            confirm="确认停机吗？\n\n停机后「立刻断网」，"
                    "要「复机」才能再上网。")

    def _recover(self):
        """复机（从停机状态恢复）。"""
        act = self._acts()
        if act is None:
            self.note = "要先点一次「同步数据」登录，才能复机。"
            self._render()
            return
        self._do_action("复机", lambda: act.recover(self.account),
                        confirm="确认复机吗？（复机后就能正常上网了）")

    # ------------------------------------------------------------ 渲染

    def _clear(self):
        for w in list(self.body.winfo_children()):
            w.destroy()
        # 分列状态跟着一起清 —— 列 Frame 刚被 destroy 掉，
        # 留着引用会让 `_card()` 往一块死控件里塞东西。
        self.cols = []
        self.col_i = 0
        self.colrow = None
        # 正文宽度先按**单列**重算一遍：验证码 / 失败 / 忙碌这几个临时视图
        # 只画一张卡，走的就是单列 —— 它们不调 `_start_cols()`，
        # 不在这里重算的话会用到上一次（可能是半列宽）的旧值，文字被静默裁掉。
        self.cw = max(200, self._body_w() - 2 * _CARD_PAD_X)
        # 🔴 顺手把 `_entry` 清掉：它指向的控件刚被 destroy 了，留着一个
        #    「已经死掉」的 widget 引用，下次 `.get()` 会抛 TclError。
        #    这类错误只在特定点击顺序下出现（比如验证码界面还没画完就按了回车），
        #    平时测不出来 —— 与其等它偶发，不如在这里断掉。
        self._entry = None

    # ---------------------------------------------------------- 横向排版

    # 两列排版的**宽度门槛**：正文窄于这个数就退回单列。
    # 🔴 判据不是「窗口多宽」，而是「正文实际拿到多少」—— 窗口宽 ≠ 正文宽
    #    （左边有导航栏、右边有滚动条列），拿窗口宽度判断会在窄屏上误判。
    # 阈值怎么来的：单列时图表要 `CHART_W`(500) + 两侧卡片内边距(28) ≈ 528 才不裁；
    #    两列时每列各拿到 (正文-16)/2，取 760 相当于每列 ~372、卡片内容 ~344 ——
    #    说明文字折行还好，图表挤成 31 根细柱子但**不会被裁**（宽度是算出来的，
    #    不是写死的），两列把总高度砍掉近一半，这个交换划算。
    #    低于 760（每列不到 372）时说明文字会折成很多行，两列反而更难看。
    # ⚠️ 760 / 16 都是**设计像素**（上面整段推理都是按 96 DPI 算的），
    #    而 `avail` 是**物理像素** ⇒ 必须过 `dp()` 才能比，不然在 150% 的屏上
    #    门槛会显得高 1.5 倍，好好的宽屏会被判成「太窄」而退回单列。
    MIN_2COL_W = dp(760)
    COL_GAP = dp(16)          # 两列之间的缝

    def _body_w(self):
        """量出正文**实际**能用的宽度（像素）。

        ⚠️ 为什么不拿 `PANEL_W` 算：嵌入模式下宽度是主窗口给的，
           而且用户随时能拖动改宽。写死一个常量 = 换台电脑/拖一下窗口就错。
        ⚠️ 第一次 `_render()` 时控件还没布局完（宽度是 1），
           所以 `update_idletasks()` 之后再问；还是问不到就用单列时代的
           默认宽度兜底 —— 那时下一帧的 `<Configure>` 会触发重排（见 `_on_canvas`）。
        """
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        for wdg, minus in ((self.canvas, 0), (self.win, _SCROLL_COL + dp(2))):
            try:
                v = wdg.winfo_width()
            except Exception:
                v = 0
            if v and v > dp(10):
                return max(dp(200), v - minus)
        return CHART_W + 2 * _CARD_PAD_X        # 兜底：老的单列宽度

    def _start_cols(self):
        """按正文宽度决定铺几列，并把 `self.cw`（卡片内容宽度）算出来。

        返回列数。`self.cols` 为空 = 单列（卡片直接塞 `self.body`）。

        ⚠️ 两列时外面还要套一层 `self.colrow`（`fill="x"`）——
           不套的话，两列用 `side="left"`、页脚说明用默认的 `side="top"`，
           Tk 的 packer 会把页脚排到**两列右边那块空白**里去（先左后上，
           上边那条是从"剩下的区域"里切），而不是排到它们下面。
        """
        avail = self._body_w()
        n = 2 if avail >= self.MIN_2COL_W else 1
        self.cols = []
        self.colrow = None
        if n > 1:
            self.colrow = self.tk.Frame(self.body, bg=BG)
            self.colrow.pack(fill="x")
            for i in range(n):
                c = self.tk.Frame(self.colrow, bg=BG)
                # ⚠️ 必须 `fill="both", expand=True` 且两列**同权**：
                #    宽度不等的话，`_card()` 里按算出来的列宽设的 wraplength
                #    就和实际列宽对不上 —— 短的那列文字会被裁，而且不报错。
                c.pack(side="left", fill="both", expand=True,
                       padx=(0, self.COL_GAP) if i < n - 1 else 0)
                self.cols.append(c)
            colw = (avail - self.COL_GAP * (n - 1)) / float(n)
        else:
            colw = avail
        # 卡片内容是「列宽 - 两侧卡片内边距」，和单列时代的 CHART_W 是同一个算法。
        self.cw = max(200, int(colw) - 2 * _CARD_PAD_X)
        self.col_i = 0
        self._laid_w = avail
        return n

    def _col(self, i):
        """下一张卡放进第 i 列。"""
        self.col_i = i

    def _card(self, title):
        """一张圆角卡片，返回往里塞控件的 Frame。

        ⚠️ 卡片的**父容器**是「当前这一列」（`self.cols` 为空时就是正文本身）——
           标题 Label 也必须跟着进同一列，不然标题会跑到正文最上面去。
        """
        parent = self.cols[self.col_i] if self.cols else self.body
        if title:
            self.tk.Label(parent, text=title, bg=BG, fg=SUB,
                          font=(self.fam, self.base - 1)
                          ).pack(anchor="w", pady=(dp(8), dp(4)))
        cv, inner = round_panel(parent, self.tk, _CARD_PAD_X, dp(12), R_CARD)
        cv.pack(fill="x")
        return inner

    def _row(self, parent, name, value, fg=None, bold=False):
        """一行「键 —— 值」。间距走 `ROW_GAP`（单行记录档，见「列表间距规范」）。"""
        f = self.tk.Frame(parent, bg=CARD)
        f.pack(fill="x", pady=dp(self.ROW_GAP))
        self.tk.Label(f, text=name, bg=CARD, fg=SUB,
                      font=(self.fam, self.base)).pack(side="left")
        self.tk.Label(f, text=value, bg=CARD, fg=fg or FG,
                      font=(self.fam, self.base, "bold" if bold else "normal")
                      ).pack(side="right")

    def _mini_button(self, parent, text, command, bg, fg, hover, active):
        """卡片里的**小**按钮（写操作用）。

        ⚠️ 高度和 `round_chip` 那类不一样：它是要**点**的，太矮不好点，
           所以给 `base * 2 + 8`（约 30px）。宽度由 `pad_x` 撑。
        ⚠️ 返回的是控件，**摆放由调用方负责** —— 卡片里统一用
           `pack(anchor="e")` 放在记录的右下方：左边是文字、右边是按钮，
           扫一眼就知道哪个按钮管哪条记录。
        """
        h = self.base * 2 + 8
        return round_button(parent, text, command, (self.fam, self.base - 1),
                            bg=bg, fg=fg, hover=hover, active=active,
                            parent_bg=CARD, height=h, pad_x=dp(14),
                            radius=R_CTRL)

    def _busy_note(self, text):
        """把正文换成一句「正在…」。**只用在同步过程中**（那时没有别的东西可看）。"""
        self.view_locked = True
        self._clear()
        inner = self._card(None)
        self.tk.Label(inner, text=text, bg=CARD, fg=SUB,
                      font=(self.fam, self.base)).pack(anchor="w")
        self._autosize()

    def _set_sync_button(self):
        """同步中把按钮禁用 —— 否则用户会连点，每次都发一次登录请求，
        把 3 次闸门白白耗掉。

        ⚠️ **只改 state，不改文字**：`round_button` 的兼容性契约里只有
           `state`（见它的文档），`configure(text=...)` 会抛 TclError。
           2026-10-12 实测过：写在 `try/except` 里的话它**静默失败**，
           按钮看着还是「同步数据」—— 但 `state` 是同一个调用里先处理的，
           所以「变灰」是生效的。
           「正在进行到哪一步」由正文的提示文字负责（正在连接 / 正在登录），
           比按钮上三个点清楚得多，所以这里不折腾文字。
        """
        try:
            self.btn_sync.configure(state="disabled" if self.syncing else "normal")
        except Exception:
            log("账号中心面板按钮状态失败:\n%s" % traceback.format_exc())

    def _render(self):
        """按当前状态把整个面板重画一遍。**所有界面改动只从主线程调这里。**

        🔴 顺序不能换：**先定列（列数 + `self.cw`），再画卡片**。
           卡片的 `wraplength`、图表画布宽度全是从 `self.cw` 推出来的，
           反过来会用到上一次的宽度 —— 那种错只在「窗口宽度变过」时出现，
           表现是文字被静默裁掉。
        """
        if self.closed:
            return
        self.view_locked = False      # 数据视图：可以让重排自由重画
        self._set_sync_button()
        self._clear()
        ncol = self._start_cols()
        if ncol < 2:
            order = [(0, k) for k in self._ONE]
        else:
            order = ([(0, k) for k in self._LEFT]
                     + [(1, k) for k in self._RIGHT])
        for col, key in order:
            if not self._has_card(key):
                continue
            self._col(col)
            self._card_call(key)()
        self._foot_note()
        self._autosize()

    # ---- 卡片清单：**新增一张卡要改这里三处**，漏了会静默少一张卡 ----
    # （`_LEFT` / `_RIGHT` 决定宽屏时放哪列，`_ONE` 决定窄屏时的先后，
    #   `_card_call` 把名字接到真正的画卡函数上。）
    #
    # 为什么分成这两组（不是随便对半砍）：
    #   左列 = 「我这个账号」—— 状态、流量趋势、账号资料。
    #          这几张是**关于账号本身**的，看的时候是一件事。
    #   右列 = 「我在用它」—— 谁在线、用了哪些会话、花了多少钱、能买什么、
    #          要办事去哪。这几张是**关于使用过程**的，而且会随数据长短差很多。
    # 高度上两列也大致相当（实测左 ≈1195 / 右 ≈1255，见 `selfwin_test.py`
    # 的两列平衡断言）—— 差太多的话短的那列底下会空一大块。
    # ⚠️ 「可购套餐」原本在左列，2026-10-16 截图之后挪到了右列：它在那组里
    #    排在最后、又是最短的一张，左列整个就比右列多了 160px 的空白。
    _LEFT = ("account", "flow", "profile")
    _RIGHT = ("devices", "bill", "fee", "buspkg", "links")
    # 单列（窄屏）时的顺序 —— 就是加两列之前那个顺序：
    # 概要 → 趋势 → 明细 → 当前 → 备查 → 链接。
    _ONE = ("account", "flow", "bill", "devices", "profile", "fee", "buspkg",
            "links")
    # 哪几张卡需要「同步过一次」。不需要的只有两张：流量图表（读本机历史）
    # 和「去学校网页办」（就是几个网址）—— 没同步过也要画，否则面板是个空壳。
    _NEEDS_DATA = ("account", "bill", "devices", "profile", "fee", "buspkg")

    def _has_card(self, key):
        return not (key in self._NEEDS_DATA and self.data is None)

    def _card_call(self, key):
        """把卡片名接到画卡函数上。

        ⚠️ 用 `raise` 而不是静默返回：名字写错时**必须当场炸**。
           返回一个空操作的话，界面上只是少一张卡，没有任何报错 ——
           正是本项目最忌讳的那种失败。
        """
        d = self.data
        table = {
            "account": lambda: self._card_account(d),
            "flow": self._card_flow,
            "bill": lambda: self._card_bill(d),
            "devices": lambda: self._card_devices(d.get("devices") or []),
            "profile": lambda: self._card_profile(d),
            "fee": lambda: self._card_fee(d),
            "buspkg": lambda: self._card_buspkg(d),
            "links": self._card_links,
        }
        if key not in table:
            raise ValueError("_card_call: 不认识的卡片名 %r（会静默少一张卡）"
                             % (key,))
        return table[key]

    def _foot_note(self):
        days = self_history.days_of(self.store, self.account)
        stamp = self_history.updated_at(self.store, self.account)
        if days:
            span = "%s ~ %s，共 %d 天" % (days[0]["date"], days[-1]["date"], len(days))
            txt = "本机存了 %s。上次同步：%s。" % (span, stamp or "（没有记录）")
        else:
            txt = ("本机还没存过 %s 的流量数据 —— 点右上角「同步数据」读一次，"
                   "以后不登录也能看。" % self.account)
        if self.store_why:
            txt += "\n⚠️ %s" % self.store_why
        # 🔴 A 档（详单/停机/套餐）没读到时要**显式**说，见 `self_service.full()`：
        #    安静吞掉的话，用户会把「没读到」读成「这个账号没有上网记录」——
        #    那是把错误说成事实，比直接报错更坏。
        if self.data and self.data.get("center_error"):
            txt += ("\n⚠️ 账号中心有几项没读到：%s。"
                    "这几项这次是空的，不代表「没有」。"
                    % self.data["center_error"])
        self.tk.Label(self.body, text=txt, bg=BG, fg=SUB, justify="left",
                      wraplength=self.cw, font=(self.fam, self.base - 2)
                      ).pack(anchor="w", pady=(dp(8), dp(0)))
        if getattr(self, "note", ""):
            self.tk.Label(self.body, text=self.note, bg=BG, fg=BLUE_DARK,
                          justify="left", wraplength=self.cw,
                          font=(self.fam, self.base - 2)).pack(anchor="w", pady=(dp(2), dp(0)))

    # ------------------------------------------------------------ 账号卡

    def _card_account(self, d):
        f, t = d.get("flow") or {}, d.get("time") or {}
        inner = self._card("账号")
        self._row(inner, "账号状态", d.get("status") or "-")

        # --- 停机 / 复机状态（A 档，2026-10-16 加）---
        # ⚠️ `d["stop"]` 为 None 表示「这次没读到」（A 档容错路径），
        #    **不是**「状态是正常」⇒ 这种情况**不画这一行**，
        #    由页脚的 center_error 说明「没读到」。假装成正常是这里最糟的错。
        st = d.get("stop")
        if isinstance(st, dict):
            state = st.get("state") or "未知"
            # 语义色：正常=绿、停机中=红、复机中=黄、未知=灰。
            # 🔴 用**查表**而不是 if/else 链：以后学校加了新的 pageType，
            #    `parse_stop` 会给「未知」+ 保留原始值，这里自然落到灰色，
            #    不会因为漏写一个分支而显示成正常。
            color = {"正常": OK, "停机中": DANGER, "复机中": WARN}.get(state, SUB)
            self._row(inner, "停机状态", state, fg=color, bold=True)
            extra = []
            if st.get("reason"):
                extra.append("原因：%s" % st["reason"])
            if st.get("date"):
                extra.append("时间：%s" % st["date"])
            if extra:
                self.tk.Label(inner, text="　".join(extra), bg=CARD, fg=SUB,
                              justify="left", wraplength=self.cw - 20,
                              font=(self.fam, self.base - 2)).pack(anchor="w")

            # --- 停机 / 复机按钮（B 档，2026-10-16 加）---
            # 🔴 **只在「正常」和「停机中」两个状态下画按钮**。别的取值
            #    （「未知」/「复机中」/以后学校新加的）**不画** ——
            #    我们不知道那时候什么操作是合法的，宁可不给按钮，
            #    让用户去学校网页。这是红线 51 的同类：不认识的取值别硬套。
            if self._can_act():
                if state == "正常":
                    self._mini_button(
                        inner, "停机", self._stop_now,
                        DANGER_SOFT, DANGER, DANGER_HOVER, DANGER_ACTIVE
                    ).pack(anchor="e", pady=(dp(6), dp(0)))
                elif state == "停机中":
                    self._mini_button(
                        inner, "复机", self._recover,
                        BLUE_SOFT, BLUE_DARK, BLUE_HOVER, BLUE_PRESS
                    ).pack(anchor="e", pady=(dp(6), dp(0)))

        self._row(inner, "上网套餐", d.get("plan") or "-")
        self._row(inner, "套餐到期", d.get("expire") or "-")
        self._row(inner, "计费日", d.get("billing_day") or "-")
        # 余额只在按金额扣费（chargeType == "1"）时才有意义 ——
        # 和学校网页的显示条件一致，别在套餐制下显示一个恒为 0 的"余额"。
        if str(d.get("charge_type")) == "1":
            self._row(inner, "余额", "%.2f 元" % (d.get("balance") or 0.0))

        # --- 套餐可用流量（A 档 `getpackageinformation.do`，2026-10-16 加）---
        # 🔴 **只在 > 0 时显示**。这个字段实测是 `0`（因为融合套餐不限量），
        #    而「可用流量 0」在界面上会被读成「一点流量都没了」—— 和事实相反。
        #    限量套餐用完了确实也是 0，但那时上面「限额」那行会写具体额度，
        #    用户能看出是「用完了」而不是「不限量」。
        pk = d.get("package")
        if isinstance(pk, dict):
            avail = pk.get("available_flow") or 0
            if float(avail) > 0:
                self._row(inner, "套餐可用流量", self_service.fmt_flow(avail))

        lim = []
        if f.get("limit"):
            lim.append("流量 %s" % f["limit"])
        if t.get("limit"):
            lim.append("时长 %s" % t["limit"])
        if lim:
            self.tk.Label(inner, text="限额：" + "　".join(lim), bg=CARD, fg=SUB,
                          font=(self.fam, self.base - 1)
                          ).pack(anchor="w", pady=(dp(6), dp(0)))
        # 累计是**学校那边**的数（本机历史里没有它 —— 我们只攒每日明细，
        # 而每日明细只覆盖服务端给的那 30 天窗口）。
        # 明确写「学校」二字，免得和上面图表里的「共计」混淆 ——
        # 那两个数**本来就该不一样**（一个是全部历史、一个是本机攒下来的）。
        self.tk.Label(inner,
                      text="学校记录：累计 %s　时长 %s"
                           % (self_service.fmt_flow(f.get("total")),
                              self_service.fmt_hours(t.get("total") or 0)),
                      bg=CARD, fg=SUB, font=(self.fam, self.base - 2)
                      ).pack(anchor="w", pady=(dp(6), dp(0)))

    # ------------------------------------------------------------ 详单卡

    # ------------------------------------------------------------ 列表间距规范
    # 🔴 2026-10-17 加。用户原话：「很多地方可读性很差，比如这里右边，
    #    文字的行与行之间没有隔开，导致很难获取到有效信息」。
    #
    #    排查下来的真因**不是字号，是分组**：账号中心右侧是一屏高密度信息
    #    （10 条会话 × 每条两行 + 费用明细 + 设备清单），而当时各个列表的
    #    间距是 1~2 设计像素、而且**只有部分列表画了分隔线** ——
    #    结果就是「会话的第二行」和「下一条会话的第一行」贴在一起，
    #    四行连成一片，眼睛找不到断点。
    #
    #    所以立这套三档规矩，判据只有一个：**这两行是不是同一条记录**。
    #      · `GROUP_IN` —— 同一条记录**内部**的两行（时间行 / 流量行）。
    #                      小间距 = 「它俩是一伙的」，读的时候不该断开。
    #      · `ITEM_GAP` —— 两条记录**之间**，并配一条 1px 的 `LINE` 分隔线。
    #                      大间距 + 分隔线 = 「这里断开了，下一条是新记录」。
    #      · `ROW_GAP`  —— **单行**记录之间（费用明细、套餐清单、键值行）。
    #                      没有分隔线，只靠间距分开；比 GROUP_IN 大、比 ITEM_GAP 小。
    #
    #    ⚠️ 这三档只用在**右侧滚动区**里，改它们不影响窗口高度（`CONTENT_H`）——
    #       设置页那边的竖向间距是另一套，别混。
    #    ⚠️ 加新列表时**别自己写 pady 数字**：挑一档用。散落的字面量正是
    #       「有的列表松、有的紧」的来源，也就是这次要修的东西。
    GROUP_IN = 3          # 同一条记录内部的两行之间
    ITEM_GAP = 10         # 两条记录之间（配合 1px 分隔线）
    ROW_GAP = 4           # 单行记录之间（不画分隔线）

    BILL_SHOW = 10        # 界面上最多列几条会话（服务端可能给几百条）

    @staticmethod
    def _mmdd(s):
        """`2026-10-14 15:05:53` → `10-14 15:05`。

        ⚠️ 用**切片**而不是 `strptime`：服务端给过 `"(null)"` 这种脏值
           （见 `self_service._clean`），`strptime` 会当场抛异常把整个面板打挂。
           切不出来就原样返回，难看但不会崩。
        """
        s = s or ""
        return s[5:16] if len(s) >= 16 else s

    def _card_bill(self, d):
        """上网详单卡（A 档 `getbilldetail.do`，2026-10-16 加）。

        🔴 **这里的「详单合计」和账号卡里的「学校记录：累计」不是同一个口径**
           （红线 35）。实测：
             · 详单合计 = 114180.32 + 501650.28 = **615.83 GB**（只统计生成了
               记录的会话，区间 2026-04-17 ~ 2026-10-14）
             · 账号卡那个「累计」= 690876 MB = **674.68 GB**（`getindexchart`
               的 `cumulativeFlow`，含没生成详单的部分）
           差 12%，**两个数都是对的**。所以这一卡必须写明「详单合计」，
           而且把区间一起写出来 —— 不写清楚用户会以为我们算错了。
        """
        inner = self._card("上网详单")
        b = d.get("bill")
        if not isinstance(b, dict):
            # A 档容错路径：这次没读到。**明说**，别说成「没有记录」。
            self.tk.Label(inner, text="（这次没读到详单，见下方说明）",
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w")
            return

        ss = b.get("sessions") or []
        s = b.get("summary") or {}
        if not ss:
            self.tk.Label(inner, text="（这个账号还没有上网记录）", bg=CARD,
                          fg=SUB, font=(self.fam, self.base)).pack(anchor="w")
            return

        # 🔴 会话是**最新在前**（实测）：首条最新、末条最旧。
        #    哪天学校改了排序，区间会反着写 —— 所以区间用 min/max 取，
        #    不靠数组顺序。
        starts = [x.get("start") or "" for x in ss]
        span = "%s ~ %s" % (min(starts)[:10], max(starts)[:10])
        self._row(inner, "区间", span)
        # ⚠️ `total` 是**服务端说的**总数；`sessions` 是**我们真拉到的**。
        #    被翻页上限截断时两者会不一样，所以两个都写出来。
        tot, got = s.get("total"), len(ss)
        self._row(inner, "会话数", "%d 次" % tot if tot else "%d 次" % got)
        self._row(inner, "详单合计时长", self_service.fmt_duration(s.get("seconds")))
        self._row(inner, "详单合计流量",
                  self_service.fmt_flow((s.get("up") or 0) + (s.get("down") or 0)))
        if (s.get("fee") or 0) > 0:
            self._row(inner, "详单合计费用", "%.2f 元" % s["fee"])
        # 🔴 红线 35：同屏出现两个**口径不同**的流量数（详单合计 601.40 GB /
        #    账号卡的「学校记录：累计」674.68 GB），差 12%。**不解释就会被当成
        #    算错** —— 所以把「为什么不一样」直接写在界面上，而不是只写在注释里。
        self.tk.Label(inner,
                      text="（口径：只统计生成了记录的会话。和账号卡里"
                           "「学校记录：累计」不是同一个数 —— 两个都对。）",
                      bg=CARD, fg=SUB, justify="left",
                      wraplength=self.cw - 20,
                      font=(self.fam, self.base - 3)).pack(anchor="w", pady=(dp(3), dp(0)))
        if tot and got and tot != got:
            self.tk.Label(inner, text="⚠️ 服务端共 %d 次，只取回 %d 次"
                          % (tot, got), bg=CARD, fg=WARN,
                          font=(self.fam, self.base - 2)).pack(anchor="w", pady=(dp(4), dp(0)))

        # --- 最近几条会话 ---
        show = ss[:self.BILL_SHOW]
        self.tk.Frame(inner, bg=LINE, height=dp(1)).pack(fill="x", pady=(dp(8), dp(0)))
        for i, x in enumerate(show):
            # 🔴 两条会话之间：**大间距 + 一条分隔线**（见「列表间距规范」）。
            #    改之前这里什么都没有 —— 上一条的「流量行」和下一条的「时间行」
            #    直接贴在一起，10 条会话看下来是 20 行连续文字，找不到断点。
            if i:
                self.tk.Frame(inner, bg=LINE, height=dp(1)
                              ).pack(fill="x", pady=(dp(self.ITEM_GAP), dp(0)))
            top = self.tk.Frame(inner, bg=CARD)
            top.pack(fill="x", pady=(dp(9), dp(0)))
            self.tk.Label(top,
                          text="%s → %s" % (self._mmdd(x.get("start")),
                                            self._mmdd(x.get("end"))),
                          bg=CARD, fg=FG,
                          font=(self.fam, self.base)).pack(side="left")
            self.tk.Label(top, text=self_service.fmt_duration(x.get("seconds")),
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base - 1)).pack(side="right")
            # 🔴 上行/下行**分开放**，不合起来 —— 用户明确说过要看上下行。
            #    `terminateCause` 放在末尾：它解释「这次为什么结束」，
            #    实测取值是「空闲超时 / 用户主动下线 / 账号抢占 / 管理员重置」。
            # ⚠️ `GROUP_IN` 的小间距是**故意小**的：它和上面那行是**同一条会话**，
            #    留太大反而会让「两行一组」的分组感散掉（见「列表间距规范」）。
            self.tk.Label(inner,
                          text="↑ %s　↓ %s　%s"
                               % (self_service.fmt_flow(x.get("up")),
                                  self_service.fmt_flow(x.get("down")),
                                  x.get("cause") or ""),
                          bg=CARD, fg=SUB, justify="left",
                          wraplength=self.cw - 20,
                          font=(self.fam, self.base - 2)
                          ).pack(anchor="w", pady=(dp(self.GROUP_IN), dp(0)))
        if len(ss) > len(show):
            self.tk.Label(inner, text="（共 %d 次，这里只列最近 %d 次）"
                          % (len(ss), len(show)), bg=CARD, fg=SUB,
                          font=(self.fam, self.base - 3)).pack(anchor="w", pady=(dp(8), dp(0)))

    # ------------------------------------------------------------ 设备卡

    def _card_devices(self, dev):
        inner = self._card("在线设备（%d 台）" % len(dev))
        if not dev:
            self.tk.Label(inner, text="当前没有设备在线", bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w")
            return
        for i, x in enumerate(dev):
            if i:
                # 两条设备之间：大间距 + 分隔线（见「列表间距规范」）
                self.tk.Frame(inner, bg=LINE, height=dp(1)
                              ).pack(fill="x", pady=(dp(self.ITEM_GAP), dp(0)))
            # 🔴 排版顺序是**用户要求的**（2026-10-12）：
            #    ① 设备类型（Windows / Android…）② 上线时间 ③ 其余写小字。
            #    原来第一行是 IP + MAC，一眼看不出「这是哪台设备」。
            top = self.tk.Frame(inner, bg=CARD)
            top.pack(fill="x", pady=(dp(9), dp(0)) if i else dp(0))
            # `osInfo` 实测是 "Windows" 这种人类可读的串；空的话**不许猜**
            # （`terminalType` 是 "a" 这种代码，含义没验证过）。
            self.tk.Label(top, text=(x.get("os") or "未知设备"), bg=CARD, fg=FG,
                          font=(self.fam, self.base + 1, "bold")).pack(side="left")
            since = x.get("since") or ""
            if since:
                self.tk.Label(top, text="%s 上线" % since, bg=CARD, fg=SUB,
                              font=(self.fam, self.base - 1)).pack(side="right")
            extra = [x.get("ip") or "-", x.get("mac") or "-"]
            if x.get("nas"):
                extra.append(x["nas"])
            # ⚠️ **故意不显示 `kind`（`terminalType`）**：实测那个字段是 "a"
            #    这种**代码**，含义没验证过。显示成「类型 a」对用户毫无意义，
            #    而猜它的含义（1=电脑 2=手机…）就是编 —— 两条都不能要。
            #    ⚠️ 数据层**仍然保留**这个字段（白名单里），只是界面不用。
            # ⚠️ `GROUP_IN`：这行和设备名是**同一台设备**，所以要贴紧。
            self.tk.Label(inner, text="　".join(extra), bg=CARD, fg=SUB,
                          justify="left", wraplength=self.cw - 20,
                          font=(self.fam, self.base - 2)
                          ).pack(anchor="w", pady=(dp(self.GROUP_IN), dp(0)))
            # --- 「下线这台」按钮（B 档，2026-10-16 加）---
            # 🔴 只在有会话时才画（`_can_act`）。点了会二次确认 + 现取一次
            #    在线清单再踢（见 `_kick` 的说明）。
            if self._can_act():
                self._mini_button(
                    inner, "下线这台", (lambda x=x: self._kick(x)),
                    DANGER_SOFT, DANGER, DANGER_HOVER, DANGER_ACTIVE
                ).pack(anchor="e", pady=(dp(6), dp(0)))

    # ------------------------------------------------------------ 账号资料卡

    def _sub(self, parent, text):
        """卡片**内部**的小分区标题（比卡片标题小一档、用加粗压住正文）。"""
        self.tk.Label(parent, text=text, bg=CARD, fg=SUB,
                      font=(self.fam, self.base - 1, "bold")
                      ).pack(anchor="w", pady=(dp(9), dp(2)))

    def _card_profile(self, d):
        """账号资料卡（A 档 `getbaseinfo.do`，2026-10-16 加）。

        ⚠️ 这个接口返回 53+6+24 个字段，**数据层是白名单取的**
           （`self_service.parse_baseinfo`）—— 这里只画白名单里那几个。
           界面这层**不再过滤**：白名单已经保证「不认识的不带出来」。

        🔴 **空值不画成空行**。这些字段对真实账号本来就会有一半是空的
           （没停机就没有 `stopDate`）。画一排「-」会让卡片看着像坏了。
           「整个接口没读到」是**另一回事** —— 那时 `base is None`，
           走下面第一条分支**明说**（红线 50）。

        ⚠️ 白名单里还有几个字段**故意没画**（`currAccumulateFlow` /
           `groupChangeFlag` / `groupChangeDate` / `flowtimeflag` /
           `operationAuthResult` / `operationAuthTime`）—— 它们的含义
           只是从字段名猜的，**没实测过**。给一个数配一个猜的标签，
           比不显示更糟（用户会当真）。要画得先弄明白它们是什么。
        """
        inner = self._card("账号资料")
        b = d.get("base")
        if not isinstance(b, dict):
            self.tk.Label(inner, text="（这次没读到账号资料，见下方说明）",
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w")
            return
        text, num = b.get("text") or {}, b.get("num") or {}
        shown = [0]

        def row(name, value, fg=None, bold=False):
            if value in (None, ""):
                return
            self._row(inner, name, str(value), fg=fg, bold=bold)
            shown[0] += 1

        def tv(k):
            return (text.get(k) or "").strip()

        def nv(k):
            return num.get(k)

        def pair(name, flow, hours):
            """一行里放「流量 + 时长」两个数（单位由 `fmt_*` 各自处理）。"""
            parts = []
            if flow is not None:
                parts.append(self_service.fmt_flow(flow))
            if hours is not None:
                parts.append("时长 %s" % self_service.fmt_hours(hours))
            row(name, "　".join(parts))

        # --- 日期 ---
        row("开通日期", tv("openingDate"))
        row("生效日期", tv("effectiveDate"))
        row("到期日期", tv("expirationDate"))
        row("首次使用", tv("firstUsetime"))
        row("最近修改", tv("latestEditDate"))
        row("上次月结", tv("lastmonthlyChargetime"))

        # --- 认证 ---
        if any(x not in (None, "") for x in (tv("authResult"), nv("loginCount"),
                                             nv("webLoginFailTimes"))):
            self._sub(inner, "认证")
        ar = tv("authResult")
        # ⚠️ 只把**认得出来的**成功值上绿；别的取值原样显示、不上色
        #    （红线 51 的同类：枚举值不认识就别往「正常」上兜底）。
        row("认证结果", ar, fg=OK if ar in ("成功", "认证成功") else None, bold=True)
        if nv("loginCount") is not None:
            row("登录次数", "%d 次" % int(nv("loginCount")))
        fails = nv("webLoginFailTimes")
        if fails is not None:
            # 失败次数多了会被锁号 —— 只在 > 0 时上告警色
            row("认证失败次数", "%d 次" % int(fails),
                fg=WARN if float(fails) > 0 else None)

        # --- 代理 / 共享检测 ---
        # 这几项是学校判断「账号有没有被多人共享一条网」的依据，
        # 也是**被停机最常见的原因**。学校自己的网页上不会解释它是干嘛的。
        if any(x not in (None, "") for x in (tv("proxyCtrlResult"),
                                             nv("proxyCtrlTimes"),
                                             tv("proxyCtrlStopTime"))):
            self._sub(inner, "代理 / 共享检测")
            row("检测结果", tv("proxyCtrlResult"))
            if nv("proxyCtrlTimes") is not None:
                row("检测次数", "%d 次" % int(nv("proxyCtrlTimes")))
            row("上次处理时间", tv("proxyCtrlStopTime"))
            self.tk.Label(inner,
                          text="（学校用这几项判断账号有没有被多人共享上网；"
                               "检测到会限制或停机。）",
                          bg=CARD, fg=SUB, justify="left",
                          wraplength=self.cw - 20,
                          font=(self.fam, self.base - 3)
                          ).pack(anchor="w", pady=(dp(3), dp(0)))

        # --- 学校记账 ---
        # ⚠️ 口径和上面的图表卡**不同**：这里全是**学校那边**的数。
        #    图表卡画的是本机攒的历史，两个数本来就该不一样（红线 35）。
        if any(nv(k) is not None for k in ("accumulateFlow", "currMonthFlow",
                                           "lastMonthFlow", "remainflow",
                                           "accumulateTime", "currMonthTime",
                                           "lastMonthTime", "remaintime")):
            self._sub(inner, "学校记账")
        pair("累计", nv("accumulateFlow"), nv("accumulateTime"))
        pair("本月", nv("currMonthFlow"), nv("currMonthTime"))
        pair("上月", nv("lastMonthFlow"), nv("lastMonthTime"))
        pair("剩余", nv("remainflow"), nv("remaintime"))
        money = nv("accumulateMoney")
        if money is not None:
            # ⚠️ 单位（元）是按同族的 `sum*Money` 推断的，**没实测过**
            row("累计消费", "%.2f 元" % float(money))

        # --- MAC 绑定槽位（B 档解绑要用 `clear_type`，见 self_actions.py）---
        binds = b.get("bind") or []
        if binds:
            self._sub(inner, "已绑定的设备（%d 个槽位）" % len(binds))
            for x in binds:
                line = x.get("mac") or "-"
                # ⚠️ **只显示 `os`（"Windows" 这种人类可读的串），不显示 `kind`**
                #    —— `kind` 是 `terminalType`，实测是 "a" 这种**代码**，
                #    含义没验证过。显示成「a」对用户毫无意义，猜它的含义又是编。
                #    （和 `_card_devices` 里那条一模一样 —— 第一版这里抄漏了，
                #      被截图看出来了：界面上真写着「Windows　a」。）
                if x.get("os"):
                    line += "　" + x["os"]
                self.tk.Label(inner, text=line, bg=CARD, fg=FG, justify="left",
                              wraplength=self.cw - 20,
                              font=(self.fam, self.base - 1)
                              ).pack(anchor="w", pady=(dp(2), dp(0)))
                # --- 「解绑」按钮（B 档，2026-10-16 加）---
                # ⚠️ `clear_type` 就是**这个槽位**的号（1~6），直接取着传下去；
                #    界面**不提供**「按序号解绑」那种入口（容易解错槽）。
                if self._can_act():
                    self._mini_button(
                        inner, "解绑", (lambda y=x: self._unbind(y)),
                        DANGER_SOFT, DANGER, DANGER_HOVER, DANGER_ACTIVE
                    ).pack(anchor="e", pady=(dp(4), dp(0)))

        if not shown[0] and not binds:
            self.tk.Label(inner, text="（学校这次没有返回可显示的账号资料字段）",
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w")

    # ------------------------------------------------------------ 费用记录卡

    FEE_SHOW = 6          # 扣费 / 缴费各最多列几条

    def _rec_row(self, parent, when, what, money):
        """费用记录的一行：左边「时间 + 类型」，右边金额。

        ⚠️ 间距走 `ROW_GAP`（单行记录档），不写死数字 —— 原来这里是 `dp(1)`，
           六条记录挤成一坨，时间和金额对不齐眼睛。
        """
        f = self.tk.Frame(parent, bg=CARD)
        f.pack(fill="x", pady=dp(self.ROW_GAP))
        self.tk.Label(f, text=when or "-", bg=CARD, fg=SUB,
                      font=(self.fam, self.base - 2)).pack(side="left")
        self.tk.Label(f, text=what or "", bg=CARD, fg=FG,
                      font=(self.fam, self.base - 1)
                      ).pack(side="left", padx=(dp(8), dp(0)))
        self.tk.Label(f,
                      text="-" if money is None else "%.2f 元" % float(money),
                      bg=CARD, fg=FG,
                      font=(self.fam, self.base - 1, "bold")).pack(side="right")

    def _card_fee(self, d):
        """费用记录卡：**扣费**（`getfeerecord.do`）+ **缴费**（`getchargerecord.do`）。

        🔴 两个**方向相反**的集合放一张卡里，小标题必须写清楚是哪一边 ——
           「扣费」是学校扣你的，「缴费」是你交给学校的。混着看会以为在重复计费。

        🔴 四类汇总**即使全是 0 也画**：0 本身是有效信息（「这个套餐不单独计费」），
           和「没读到」是两回事 —— 后者是 `fee is None`，走上面那条分支明说。
        """
        inner = self._card("费用记录")
        fee, chg = d.get("fee"), d.get("charge")

        # --- 扣费 ---
        if not isinstance(fee, dict):
            self.tk.Label(inner, text="（这次没读到扣费记录，见下方说明）",
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w")
        else:
            self._sub(inner, "扣费（学校扣你的）")
            s = fee.get("sum") or {}
            for label, key in (("固定费", "fix"), ("月租", "month"),
                               ("业务费", "bus"), ("流量时长费", "flowtime")):
                v = s.get(key)
                self._row(inner, label,
                          "-" if v is None else "%.2f 元" % float(v))
            rows = fee.get("rows") or []
            if not rows:
                self.tk.Label(inner, text="（没有扣费明细）", bg=CARD, fg=SUB,
                              font=(self.fam, self.base - 1)
                              ).pack(anchor="w", pady=(dp(3), dp(0)))
            for x in rows[:self.FEE_SHOW]:
                self._rec_row(inner, (x.get("time") or "")[:10],
                              x.get("kind") or "", x.get("money"))
            if len(rows) > self.FEE_SHOW:
                self.tk.Label(inner, text="（共 %d 条，这里只列最近 %d 条）"
                              % (len(rows), self.FEE_SHOW), bg=CARD, fg=SUB,
                              font=(self.fam, self.base - 3)
                              ).pack(anchor="w", pady=(dp(3), dp(0)))

        # --- 缴费 ---
        if not isinstance(chg, dict):
            self.tk.Label(inner, text="（这次没读到缴费记录，见下方说明）",
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w", pady=(dp(8), dp(0)))
        else:
            rows = chg.get("rows") or []
            self._sub(inner, "缴费（你交给学校的）")
            if not rows:
                self.tk.Label(inner, text="（没有缴费记录）", bg=CARD, fg=SUB,
                              font=(self.fam, self.base - 1)).pack(anchor="w")
            for x in rows[:self.FEE_SHOW]:
                what = x.get("kind") or ""
                if x.get("state"):
                    what += "　%s" % x["state"]
                self._rec_row(inner, (x.get("time") or "")[:10], what,
                              x.get("money"))
            if len(rows) > self.FEE_SHOW:
                self.tk.Label(inner, text="（共 %d 条，这里只列最近 %d 条）"
                              % (len(rows), self.FEE_SHOW), bg=CARD, fg=SUB,
                              font=(self.fam, self.base - 3)
                              ).pack(anchor="w", pady=(dp(3), dp(0)))

    # ------------------------------------------------------------ 可购套餐卡

    def _card_buspkg(self, d):
        """可购套餐卡（A 档 `getbusinesspackage.do`，2026-10-16 加）。**只列，不买。**"""
        inner = self._card("可购套餐")
        p = d.get("buspkg")
        if not isinstance(p, dict):
            self.tk.Label(inner, text="（这次没读到可购套餐，见下方说明）",
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w")
            return
        rows = p.get("rows") or []
        if not rows:
            # 🔴 **不许说成「你没有可购套餐」**。这个账号实测返回就是空的
            #    （融合套餐由电信侧办理），但「学校返回空列表」和「你不能买」
            #    是两件事 —— 后者是事实，前者只是我们看到的。
            self.tk.Label(inner, text="学校返回的可购套餐列表是空的。",
                          bg=CARD, fg=SUB,
                          font=(self.fam, self.base)).pack(anchor="w")
            self.tk.Label(inner,
                          text="（要办套餐请到学校自助服务网页 —— 涉及付款，"
                               "本软件不代办。）",
                          bg=CARD, fg=SUB, justify="left",
                          wraplength=self.cw - 20,
                          font=(self.fam, self.base - 2)
                          ).pack(anchor="w", pady=(dp(3), dp(0)))
            return
        for x in rows:
            f = self.tk.Frame(inner, bg=CARD)
            # 单行记录档（见「列表间距规范」）。原来是 dp(1)，一行挨一行。
            f.pack(fill="x", pady=dp(self.ROW_GAP))
            self.tk.Label(f, text=x.get("name") or "（未命名套餐）", bg=CARD, fg=FG,
                          font=(self.fam, self.base - 1)).pack(side="left")
            right = []
            if x.get("money") is not None:
                right.append("%.2f 元" % float(x["money"]))
            if x.get("flow") is not None:
                right.append(self_service.fmt_flow(x["flow"]))
            if right:
                self.tk.Label(f, text="　".join(right), bg=CARD, fg=SUB,
                              font=(self.fam, self.base - 1)).pack(side="right")

    # ------------------------------------------------------------ 去学校网页办

    # C 档：涉及**钱 / 第三方**的，**只放跳转链接**（红线 9：别替别人处理钱，
    # 也别依赖第三方服务器 —— 让学校自己的页面去办，我们只负责「一键打开」）。
    #
    # ⚠️ 路由是 **hash 模式**（`app.js` 里写的是 `mode:"hash"`），所以页面地址形如
    #    `<base>/self/index.html#/<route>`。
    # ⚠️ 下面这些 route 名是**从 app.js 的路由表里抄出来的**，不是猜的
    #    （`path:"/payfees",name:"PayFees"` 这一串）。
    SELF_LINKS = (
        ("账号充值 / 在线支付", "payfees"),
        ("卡充值", "cardpay"),
        ("转账", "chargetransfer"),
        ("办理套餐", "subscribeOrder"),
        ("变更套餐", "changeorder"),
    )
    # 官方文件。⚠️ 这两个是**实测活着**的（2026-10-16 取到 206 + 正确的
    #    Content-Type）。同批的第三个（`uploadpic/zznet.pdf`《校园网使用手册》）
    #    这次已经是 **404** —— 说明这几个地址**会变**，发版前要重新核一遍。
    SELF_DOCS = (
        ("《关于加强校园网账号安全管理的通知》（PDF）",
         "/uploadpic/notice/20260513.pdf"),
        ("《暑假自助暂停通知》（DOCX）",
         "/uploadpic/notice/20260703.docx"),
    )

    def _open_url(self, url):
        """用系统浏览器打开。🔴 **打不开就把网址显示出来**，别静默失败。

        静默失败的后果：用户点了「打开」什么也没发生，只会以为按钮坏了 ——
        而正确做法（复制网址自己打开）他根本想不到。
        """
        if open_in_browser(url):
            self.note = "已在浏览器里打开（那个页面要再登录一次）。"
        else:
            self.note = "没打开浏览器。网址：%s" % url
        self._render()

    def _link_row(self, parent, label, url):
        """一行「左边说明 + 右边『打开』」。间距走 `ROW_GAP`。"""
        f = self.tk.Frame(parent, bg=CARD)
        f.pack(fill="x", pady=dp(self.ROW_GAP))
        self.tk.Label(f, text=label, bg=CARD, fg=FG, justify="left",
                      wraplength=self.cw - 96,
                      font=(self.fam, self.base - 1)).pack(side="left")
        h = self.base * 2 + 6
        round_chip(f, self.tk, "打开", (self.fam, self.base - 2), BLUE_DARK,
                   BLUE_SOFT, page=CARD, height=h, pad_x=dp(13),
                   command=(lambda u=url: self._open_url(u))).pack(side="right")

    def _card_links(self):
        """「去学校网页办」卡（C 档 + 官方文件）。

        🔴 **只放链接，不代办**。理由（红线 9）：充值 / 支付会走到**公网第三方**
           （`ht.huarantech.com.cn:20080`），套餐办理涉及资费 —— 钱这件事让学校
           自己的页面去处理，我们只负责「一键打开那个页面」。

        ⚠️ 这张卡**不需要会话**（就是几个网址），所以**没同步过也画** ——
           这样面板在「离线优先」的默认状态下也有用，不只是个空壳。
        """
        inner = self._card("去学校网页办")
        self.tk.Label(inner,
                      text="这几件事涉及付款，本软件不代办 —— 点开的是学校自己的"
                           "网页。\n（浏览器里要再登录一次；登录后如果没直接跳到"
                           "那一页，在左边菜单里找一下。）",
                      bg=CARD, fg=SUB, justify="left",
                      wraplength=self.cw - 20,
                      font=(self.fam, self.base - 2)).pack(anchor="w")
        for label, route in self.SELF_LINKS:
            self._link_row(inner, label, "%s/self/index.html#/%s"
                           % (self.svc.base, route))
        self._sub(inner, "官方文件（学校自己的服务器上）")
        for label, path in self.SELF_DOCS:
            self._link_row(inner, label, "%s%s" % (self.svc.base, path))

    # ------------------------------------------------------------ 图表卡

    def _card_flow(self):
        """流量图表卡：按天/按月 + 月份导航 + 柱状图 + 汇总行。

        数据来自**本机历史**（`self_history`），不来自这次联网 ——
        所以没同步过也能看（只要以前同步过）。
        """
        days = self_history.days_of(self.store, self.account)
        inner = self._card("数据流量")
        bar = self.tk.Frame(inner, bg=CARD)
        bar.pack(fill="x")
        for key, label in (("day", "按天"), ("month", "按月")):
            on = (self.view == key)
            round_chip(bar, self.tk, label, (self.fam, self.base - 1),
                       "#ffffff" if on else SUB,
                       BLUE if on else NEUTRAL_SOFT, page=CARD, height=dp(22),
                       hover=None if on else BTN_ACTIVE,
                       command=(lambda k=key: self.set_view(k))).pack(
                side="left", padx=(dp(0), dp(6)))
        if self.view == "day" and days:
            self._month_nav(bar, days)
        self.tk.Label(bar, text="单位：MB / GB", bg=CARD, fg=SUB,
                      font=(self.fam, self.base - 3)).pack(side="right")

        if not days:
            cv = self.tk.Canvas(inner, bg=CARD, highlightthickness=0, bd=0,
                                width=self.cw, height=dp(110))
            cv.pack(fill="x")
            cv.create_text(self.cw / 2, 50, text="（本机还没有流量数据）",
                           fill=SUB, font=(self.fam, self.base))
            cv.create_text(self.cw / 2, 74,
                           text="点右上角「同步数据」读一次，以后不登录也能看",
                           fill=SUB, font=(self.fam, self.base - 2))
            return

        bars = self._series(days)
        self._draw_bars(inner, bars)
        self._summary_rows(inner, bars)

    def _month_nav(self, parent, days):
        """‹ 10月 › —— 只在按天视图出现，切的是「看哪个月」。"""
        months = sorted({d["date"][:7] for d in days})
        if not self.ym or self.ym not in months:
            self.ym = months[-1]
        i = months.index(self.ym)
        nav = self.tk.Frame(parent, bg=CARD)
        nav.pack(side="left", padx=(dp(6), dp(0)))
        prev = "‹" if i > 0 else " "
        nxt = "›" if i < len(months) - 1 else " "
        for sym, delta, enabled in ((prev, -1, i > 0),
                                    (nxt, +1, i < len(months) - 1)):
            if sym == " ":
                continue
            round_chip(nav, self.tk, sym, (self.fam, self.base), BLUE_DARK,
                       BLUE_SOFT, page=CARD, height=dp(20), hover=BLUE_HOVER,
                       command=(lambda d=delta: self.shift_month(d))
                       ).pack(side="left", padx=(dp(0), dp(4)))
        self.tk.Label(nav, text="%s 年 %s 月" % (self.ym[:4], int(self.ym[5:])),
                      bg=CARD, fg=FG, font=(self.fam, self.base - 1, "bold")
                      ).pack(side="left", padx=(dp(4), dp(0)))

    def set_view(self, key):
        if key not in ("day", "month") or key == self.view:
            return
        self.view = key
        self.note = ""
        self._render()

    def shift_month(self, delta):
        days = self_history.days_of(self.store, self.account)
        months = sorted({d["date"][:7] for d in days})
        if self.ym in months:
            i = months.index(self.ym) + delta
            if 0 <= i < len(months):
                self.ym = months[i]
                self._render()

    def _series(self, days):
        """当前视图要画的柱子：`[{"label":…, "value": MB, "tip":…}, …]`。"""
        if self.view == "month":
            return [{"label": "%d月" % int(m["month"][5:]),
                     "value": m["flow"],
                     "tip": "%s（%d 天）" % (m["month"], m["n"])}
                    for m in self_history.group_by_month(days)]
        ym = self.ym or days[-1]["date"][:7]
        return [{"label": str(int(d["date"][8:10])),
                 "value": d["flow"],
                 "tip": d["date"]}
                for d in days if d["date"][:7] == ym]

    def _draw_bars(self, parent, bars):
        """自绘柱状图。

        ⚠️ 三条「不这么写就会难看/看不懂」的规矩：

          1. **柱顶数值标签要自动避让。** 一个月最多 31 根柱子，卡片宽度
             （`self.cw`）放不下 31 个「12.34 GB」。所以按可用间距决定
             「每隔几根标一个」，但**最高的那根永远标** —— 那是用户最想看的一个数。
          2. **日期刻度同理**，挤不下就隔几根标一个。
          3. **`None` 不是 0。** 服务端没给某天就是没这根柱子（画成空位），
             而「确实用了 0」要画成一根 0 高度的柱子。两者不能混
             （`self_history` 那边已经把 `None` 保住了，这里别把它当 0）。
        """
        cv = self.tk.Canvas(parent, bg=CARD, highlightthickness=0, bd=0,
                            width=self.cw, height=CHART_H + 30)
        cv.pack(fill="x")
        n = len(bars)
        if not n:
            cv.create_text(self.cw / 2, CHART_H / 2, text="这个月没有数据",
                           fill=SUB, font=(self.fam, self.base - 1))
            return

        top_pad = 30                      # 柱顶标签要占的高度
        base_y = CHART_H                  # x 轴（柱子从这里往上长）
        plot_h = base_y - top_pad
        vmax = max(float(b["value"] or 0.0) for b in bars) or 1.0
        slot = float(self.cw) / n
        # 柱宽：跟着间距走，但**上下都夹住** ——
        # 下限 4px（一个月 31 根时还能看见），上限 44px
        # （只画 2 个月时不然会变成两根巨大的色块）。
        bw = max(4.0, min(44.0, slot * 0.56))

        # x 轴
        cv.create_line(0, base_y, self.cw, base_y, fill=LINE)

        # 日均线（虚线）。**它算的是「本图这几根柱子」的平均**，
        # 和下面汇总里那个「日均」是**同一个口径**（2026-10-12 实测发现：
        # 原来一个按图算、一个按全部历史算，同屏出现两个不同的「日均」，
        # 看着就像算错了）。所以两处的标签必须能对上。
        total = sum(float(b["value"] or 0.0) for b in bars)
        avg = total / n
        if avg > 0:
            ay = base_y - plot_h * (avg / vmax)
            cv.create_line(0, ay, self.cw, ay, fill=CHART_AVG, dash=(4, 3))
            cv.create_text(self.cw - 2, ay - 7, anchor="e",
                           text="日均 %s" % self_service.fmt_flow_short(avg),
                           fill=SUB, font=(self.fam, self.base - 4))

        # 柱顶标签要隔几根标一个（最高的那根永远标）
        lab_w = 62.0
        step = max(1, int(lab_w / slot) + (1 if slot < lab_w else 0))
        day_step = max(1, int(16.0 / slot) + (1 if slot < 16.0 else 0))
        imax = max(range(n), key=lambda i: float(bars[i]["value"] or 0.0))

        for i, b in enumerate(bars):
            cx = slot * (i + 0.5)
            v = float(b["value"] or 0.0)
            hgt = plot_h * (v / vmax)
            if hgt > 0:
                cv.create_rectangle(cx - bw / 2, base_y - hgt, cx + bw / 2,
                                    base_y, fill=BLUE, outline="")
            else:
                # 确实用了 0 ⇒ 画一小段浅色，和「没有这根柱子」区分开
                cv.create_rectangle(cx - bw / 2, base_y - 2, cx + bw / 2,
                                    base_y, fill=CHART_ZERO, outline="")
            if i == imax or i % step == 0:
                cv.create_text(cx, base_y - hgt - 11,
                               text=self_service.fmt_flow_short(v),
                               fill=FG if i == imax else SUB,
                               font=(self.fam, self.base - 4,
                                     "bold" if i == imax else "normal"))
            if i % day_step == 0 or i == n - 1:
                cv.create_text(cx, base_y + 14, text=b["label"], fill=SUB,
                               font=(self.fam, self.base - 3))

    def _summary_rows(self, parent, bars):
        """图表下面那几行汇总。

        🔴 **口径必须写在界面上**（2026-10-12 实测踩出来的）：第一版里
        「共计」按**全部本机历史**算、「日均」按**本图这几根柱子**算，
        同屏出现两个不同口径的数，看着就像算错了。
        现在分两段写清楚：一段「本机全部（N 天）」，一段「本图 N 天」，
        而且图上那条日均虚线 = 下面「本图」的日均（**同一个口径**）。

        🔴 上行/下行是**「知道的那几天」的和**，并且把天数写出来
        （`8/9 天`）—— 因为**今天的上下行服务端不给**（实测），
        而今天几乎总在明细里。写成「一天不知道就显示 —」的话，
        用户要看的这个数就永远看不到（见 `self_history.summarize` 的说明）。
        """
        days = self_history.days_of(self.store, self.account)
        s = self_history.summarize(days)
        grid = self.tk.Frame(parent, bg=CARD)
        grid.pack(fill="x", pady=(dp(10), dp(0)))

        def cell(r, c, name, value, bold=False):
            self.tk.Label(grid, text=name, bg=CARD, fg=SUB,
                          font=(self.fam, self.base - 1)
                          ).grid(row=r, column=c * 2, sticky="w", padx=(dp(0), dp(4)))
            self.tk.Label(grid, text=value, bg=CARD, fg=FG,
                          font=(self.fam, self.base - 1,
                                "bold" if bold else "normal")
                          ).grid(row=r, column=c * 2 + 1, sticky="w",
                                 padx=(dp(0), dp(18)))

        def part(v, known, n):
            """带「算了几天的」后缀 —— 全部都有值时就不加，免得啰嗦。"""
            if v is None:
                return "—"
            txt = self_service.fmt_flow(v)
            if known < n:
                txt += "（%d/%d 天）" % (known, n)
            return txt

        self.tk.Label(grid, text="本机全部（%d 天）" % s["n"], bg=CARD, fg=SUB,
                      font=(self.fam, self.base - 1, "bold")
                      ).grid(row=0, column=0, columnspan=4, sticky="w")
        cell(1, 0, "共计", self_service.fmt_flow(s["flow"]), True)
        cell(1, 1, "时长",
             self_service.fmt_hours(s["hours"]) if s["hours"] is not None else "—")
        cell(2, 0, "上行", part(s["up"], s["up_days"], s["n"]))
        cell(2, 1, "下行", part(s["down"], s["down_days"], s["n"]))

        if bars:
            got = sum(float(b["value"] or 0.0) for b in bars)
            scope = ("本图 %d 天" % len(bars)) if self.view == "day" \
                else ("本图 %d 个月" % len(bars))
            self.tk.Label(grid, text="%s：共计 %s　日均 %s"
                          % (scope, self_service.fmt_flow(got),
                             self_service.fmt_flow(got / len(bars))),
                          bg=CARD, fg=SUB, font=(self.fam, self.base - 2)
                          ).grid(row=3, column=0, columnspan=4, sticky="w",
                                 pady=(dp(4), dp(0)))

    # ------------------------------------------------------------ 验证码 / 失败

    def _draw_fail(self, msg):
        self.view_locked = True
        self._clear()
        inner = self._card(None)
        self.tk.Label(inner, text="读不到数据", bg=CARD, fg=DANGER,
                      font=(self.fam, self.base, "bold")).pack(anchor="w")
        self.tk.Label(inner, text=msg, bg=CARD, fg=FG, wraplength=self.cw - 20,
                      justify="left", font=(self.fam, self.base)
                      ).pack(anchor="w", pady=(dp(6), dp(0)))
        self.tk.Label(inner,
                      text="（这个功能要连上校园网、并且学校自助服务能访问才行。\n"
                           "　本机已经存下来的流量历史不受影响，下次点「同步数据」再试）",
                      bg=CARD, fg=SUB, wraplength=self.cw - 20, justify="left",
                      font=(self.fam, self.base - 1)).pack(anchor="w", pady=(dp(6), dp(0)))
        self._back_row(inner)
        self._autosize()

    def _draw_captcha(self, png, note=None):
        """画验证码输入界面。`note` 非空时显示成一行红色提示（验证码错了）。"""
        self.view_locked = True
        self._clear()
        inner = self._card(None)

        if png:
            import base64
            try:
                # 🔴 Tk 8.6 原生支持 PNG，**不需要 PIL**（已实测）。
                #    data= 接受 base64 字符串。
                self._cap_small = self.tk.PhotoImage(
                    data=base64.b64encode(png).decode("ascii"))
                # 130x40 → 520x160（放大倍数见 CAPTCHA_ZOOM，和窗口宽度绑定）
                self._cap_big = self._cap_small.zoom(CAPTCHA_ZOOM)
            except Exception:
                log("验证码图片渲染失败:\n%s" % traceback.format_exc())
                self._cap_small = self._cap_big = None
        else:
            # 🔴 **没有新图时必须把旧图丢掉。**
            #    2026-10-12 读代码时发现的真 bug：原来是「png 为空就什么都不做」，
            #    于是 `_cap_big` 还是**上一张**验证码 —— 界面一边显示旧图，
            #    一边写着「换了一张，请重新输入」。用户照着旧图输，必然再错一次，
            #    还会以为是自己眼花了。宁可不显示图，也不能显示错的图。
            self._cap_small = self._cap_big = None

        if self._cap_big is not None:
            self.tk.Label(inner, image=self._cap_big, bg=CARD).pack(pady=(dp(2), dp(8)))
        else:
            self.tk.Label(inner, text="（验证码图片显示不出来）", bg=CARD,
                          fg=WARN, font=(self.fam, self.base)).pack(pady=(dp(2), dp(8)))

        self.tk.Label(inner, text="请输入图片里的 4 个字符", bg=CARD, fg=SUB,
                      font=(self.fam, self.base - 1)).pack()
        e = self.tk.Entry(inner, font=(self.fam, self.base + 8), width=8,
                          justify="center", bg=FIELD, fg=FG, relief="flat",
                          insertbackground=FG)
        e.pack(pady=(dp(6), dp(2)), ipady=dp(4))
        e.focus_set()
        e.bind("<Return>", lambda _ev: self._submit())
        self._entry = e

        if note:
            # 提示文字要跟「到底有没有换到新图」一致 —— 说「换了一张」却
            # 显示旧图（或者根本没图）就是在骗用户，见上面那段说明。
            self.tk.Label(inner,
                          text=("验证码不对，换了一张，请重新输入"
                                if self._cap_big is not None else
                                "验证码不对，而且新的没取到 ——\n"
                                "点「换一张」再试一次"),
                          bg=CARD, fg=DANGER, justify="left",
                          font=(self.fam, self.base - 1)).pack(pady=(dp(4), dp(0)))

        left = self.tries >= SELF_LOGIN_MAX
        if left:
            self.tk.Label(inner,
                          text="已经试了 %d 次。为避免账号被锁，这里停下来了 ——\n"
                               "请确认密码没错，然后关掉窗口重开。" % self.tries,
                          bg=CARD, fg=DANGER, justify="left",
                          font=(self.fam, self.base - 1)).pack(pady=(dp(6), dp(0)))

        btns = self.tk.Frame(inner, bg=CARD)
        btns.pack(pady=(dp(10), dp(2)))
        h = self.base * 2 + 8
        if not left:
            round_button(btns, "确定", self._submit, (self.fam, self.base),
                         bg=BLUE, fg="#ffffff", hover=BLUE_DARK,
                         active=BLUE_ACTIVE, parent_bg=CARD, height=h,
                         radius=R_CTRL).pack(side="left", padx=(dp(0), dp(8)))
        round_chip(btns, self.tk, "换一张", (self.fam, self.base),
                   BLUE_DARK, BLUE_SOFT, page=CARD, height=h,
                   command=self._renew).pack(side="left", padx=(dp(0), dp(8)))
        round_chip(btns, self.tk, "取消", (self.fam, self.base), SUB,
                   NEUTRAL_SOFT, page=CARD, height=h, hover=BTN_ACTIVE,
                   command=self._cancel).pack(side="left")
        self._autosize()

    def _back_row(self, inner):
        """一行「返回」——失败页/验证码页用它回到数据页。"""
        btns = self.tk.Frame(inner, bg=CARD)
        btns.pack(pady=(dp(10), dp(2)))
        h = self.base * 2 + 8
        round_chip(btns, self.tk, "返回", (self.fam, self.base), BLUE_DARK,
                   BLUE_SOFT, page=CARD, height=h,
                   command=self._cancel).pack(side="left")

    def _cancel(self):
        """取消同步 / 返回：回到「本机历史」那个视图（不登录也看得到）。"""
        self.syncing = False
        self._render()

    def _draw_login_err(self, msg):
        """🔴 非验证码类的登录失败 —— **不许重试**。"""
        self.view_locked = True
        self._clear()
        inner = self._card(None)
        self.tk.Label(inner, text="登录不上自助服务", bg=CARD, fg=DANGER,
                      font=(self.fam, self.base, "bold")).pack(anchor="w")
        self.tk.Label(inner, text=msg, bg=CARD, fg=FG, wraplength=self.cw - 20,
                      justify="left", font=(self.fam, self.base)
                      ).pack(anchor="w", pady=(dp(6), dp(0)))
        self.tk.Label(inner,
                      text="这不是验证码的问题。\n"
                           "如果提示密码不对，请先在主界面把密码改对再试。\n"
                           "（这里**故意不再重试** —— 反复登录失败可能被学校锁号）",
                      bg=CARD, fg=SUB, wraplength=CHART_W - 20, justify="left",
                      font=(self.fam, self.base - 1)).pack(anchor="w", pady=(dp(6), dp(0)))
        self._back_row(inner)
        self._autosize()

    def _submit(self):
        if self.tries >= SELF_LOGIN_MAX:
            return
        e = self._entry
        if e is None:
            return
        try:
            code = (e.get() or "").strip()
        except Exception:
            # 控件已经被 destroy 了（比如界面刚被重画）—— 当作没输入
            return
        if not code:
            return
        e.configure(state="disabled")
        self._busy_note("正在登录…")
        self._run(lambda: self._job_login(code))

    def _renew(self):
        """换一张验证码。**不计入登录次数**（没发登录请求）。"""
        self._busy_note("正在取新验证码…")
        self._run(self._job_captcha)


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
    # 改一个数字，窗口高度就跟着变（窗口高度 = 内容高度，见 `window_geometry`）。
    #
    # 📜 历史（2026-10-04 把卡片从「1px 描边的 Frame」换成圆角 Canvas 时）：
    #     当时刻意做到**逐像素等价** ——
    #       旧：box 高 = body + 2×14(ipady) + 2×1(highlightthickness) = body + 30
    #           body 宽 = inner 宽 - 2×18 - 2×1                        = inner 宽 - 38
    #       新：cv  高 = body + 2×15                                    = body + 30
    #           body 宽 = cv 宽 - 2×19                                  = inner 宽 - 38
    #     所以「光换卡片实现本身」内容高度一个像素都没变（实测：新旧都量到 814）。
    #     ⚠️ 别把这条历史误读成「改卡片不用重量」——同一轮里另外三处改动
    #        （列表去掉方角描边 −2、自启换圆角复选框 +1、隐私小链接换圆角片 −2）
    #        还是让 CONTENT_H 从 830 变成了 827。
    # 🔴 谁要动这三个数，必须同时跑 `content_h_probe.py` 重新量，
    #    并把结果同步进 `geometry_test.py` 的 CONTENT_H / MEASURED_HEIGHTS。
    # 🔴 还有一条硬约束：`pad_x >= radius` 且 `pad_y >= radius`（round_panel 会断言）。
    #    违反的话 body 那块白矩形会盖住圆角，看着就像「圆角没生效」。
    #
    # 🔴 2026-10-17（换 Fluent）：**这三个数原来没有套 `dp()`** —— 是个漏网的
    #    单位 bug。2026-10-16 打开高 DPI 感知之后，所有尺寸的含义都从
    #    「设计像素」变成了「物理像素」（见 `dp()` 的说明），而这三个数漏掉了，
    #    于是 150% 的屏上卡片内边距实际只有 15px（设计上要的是 15 设计像素
    #    = 22.5px）—— 卡片挤得像没有内边距，这正是用户说「UI 太丑」的一部分。
    #    补上 `dp()` 之后卡片才恢复该有的呼吸感。
    #    ⚠️ 连带后果：内容高度涨了（5 张卡 × 每张 +14px），`CONTENT_H` 必须重量。
    #
    # （放在类属性而不是 __init__ 里：`card()` 的默认参数要在**类定义时**求值，
    #   那一刻还没有 self —— 见 `card()` 的签名。）
    CARD_PAD_X = dp(19)
    CARD_PAD_Y = dp(15)
    CARD_R = R_CARD

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
        # 分区标题的字号（2026-10-17 加）。原来是 SMALL —— 比正文还小一号的
        # 灰字，一屏看下去标题压不住正文，也就是「层次不清」。
        # 🔴 Win11 的分区标题是**加粗 + 和正文同号**，靠字重和颜色（近黑）拉开，
        #    不靠放大。所以这里取 BASE 而不是 BASE+1。
        #    ⚠️ 改这里必须连带重标定 `title_row_h()`（它按字号算行高）和
        #       `geometry_test.CONTENT_H`（标题行变高会顶高每一张卡）。
        self.SEC_FS = self.BASE

        self.root.option_add("*Font", (self.fam, self.BASE))
        ico = icon_path()
        if ico:
            try:
                self.root.iconbitmap(default=ico)
            except Exception:
                pass

        # --- 布局骨架：**左侧导航 + 右侧内容**（2026-10-16 改）---
        #
        # 🔴 原来是「一个功能开一个 Toplevel」：设置一个窗口、「账号中心」一个
        #    窗口、「使用说明」又一个窗口。用户原话：
        #      「每加一个新功能就要加一个窗口，那用户同时看好几个功能
        #        不就全屏都是窗口了」
        #    所以现在**整个程序只有一个窗口**，功能都在内容区里进出。
        #    加新功能 = 往 `self.PAGES` 里加一行 + 写一个 `_page_xxx()`，
        #    不再多开窗口。
        #
        # ⚠️ 导航放左边而不是顶部标签：竖向空间是这个界面最紧的资源
        #    （内容 827px / 上限 906px），横向很富裕。见 NAV_W 那边的说明。
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True, padx=OUTER_PAD, pady=OUTER_PAD)
        self.nav = tk.Frame(shell, bg=BG, width=NAV_W)
        self.nav.pack(side="left", fill="y")
        # ⚠️ `pack_propagate(False)`：不让子控件把导航栏撑宽。
        #    不关掉的话它会跟着内容长，`NAV_W` 就成了摆设。
        self.nav.pack_propagate(False)
        self.content = tk.Frame(shell, bg=BG)
        self.content.pack(side="left", fill="both", expand=True,
                          padx=(NAV_GAP, 0))
        # 内容区里的每一页都是一个 Frame，同一时刻只 pack 一个。
        # 「设置」页就是原来那套内容，**一行都没动**，只是换了个爹。
        self.page_settings = tk.Frame(self.content, bg=BG)
        self.page_center = None      # 懒建（建的时候要开面板、连线程）
        self.page_help = None

        outer = tk.Frame(self.page_settings, bg=BG)
        # ⚠️ 这里**不再加 padx/pady** —— `shell` 已经给过最外层留白了，
        #    再加一层会和「账号中心」那一页差 14px，切页时内容会跳一下。
        outer.pack(fill="both", expand=True)

        # 导航栏 + 初始页。**必须在内容都建完之后**：
        # 导航项要按 `self_service_available()` 决定显不显示「账号中心」。
        self.page = ""
        self.panel = None
        self._build_nav()
        self.show_page("settings")

        # 内容放进可滚动区域。屏幕矮的时候（1366x768、1080p@150%…）内容会比窗口高，
        # 没有滚动的话底部「开机自启」就永远够不到 —— 那是真的没法开自启，不是难看。
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        holder = tk.Frame(canvas, bg=BG)
        inner = tk.Frame(holder, bg=BG)
        inner.pack(fill="both", expand=True, padx=(dp(0), dp(2)), pady=dp(0))
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
        #
        # 🔴 2026-10-10（UI 方案 C）：**五个分区从五色收成一套蓝**。
        #    原来是紫 / 蓝 / 绿 / 红 / 青各一个 —— 和站点那边六色图标是同一个毛病：
        #    颜色太多、太平均，反而成了「AI 生成」的观感。
        #    ⚠️ 但「单调」这个老问题（2026-10-04 用户提过）依然要防 ——
        #       现在靠**图标形状**（list / user / power / lock / globe 五个不同图形）
        #       和浅底徽章本身提供层次，不再靠色相。
        #    ⚠️ 「网络状态」保留绿色：那是**真的语义色**（在线 / 离线），
        #       和「装饰用的彩色」不是一回事。别顺手也改成蓝。
        self.SEC_STYLE = {
            "已保存的账号": (BLUE, BLUE_SOFT, "list"),
            "账号信息":     (BLUE, BLUE_SOFT, "user"),
            "开机自启":     (BLUE, BLUE_SOFT, "power"),
            "隐私":         (BLUE, BLUE_SOFT, "lock"),
            "网络状态":     (OK, OK_SOFT, "globe"),
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
        head.pack(fill="x", pady=(dp(2), dp(10)))
        self.lbl(head, "%s · 设置" % APP_TITLE, self.TITLE, True, bg=BG).pack(side="left")
        # 版本号紧跟在标题右边，小字弱色。用户要报问题时第一眼就能看到它。
        # **同时它也是「检查更新」的入口**（做成链接样式的小字，不新增第四个按钮 ——
        # 主按钮固定三个是既有的界面铁律，改按钮要同步改 set_busy 的控件元组）。
        #
        # ⚠️ 视觉上必须让人看出「这行字能点」：只绑 cursor 是没用的 ——
        # 截图里它跟普通灰字一模一样，用户根本不会去点。所以给它加：
        #   ① 下划线（链接的通用约定）
        #   ② 浅蓝圆角底（和周围的灰底区分开，但不抢主按钮的视觉）
        # 这两个加起来，一眼就知道是可点的东西。
        #
        # 2026-10-04 从 `tk.Label` 换成 Canvas 圆角片（`round_chip`）：
        # Label 只有直角，和周围的圆角卡片不搭。接口是对齐的（见 round_chip 的契约），
        # `set_busy()` 改文字/配色和 `update_wiring_check()` 的点击自检都照常工作。
        self.btn_ver = round_chip(head, tk, "v%s　检查更新" % VERSION,
                                  (self.fam, self.SMALL, "underline"), BLUE_DARK, BLUE_SOFT,
                                  page=BG, height=self.SMALL + 14, hover=BLUE_HOVER,
                                  command=lambda: self.on_check_update())
        self.btn_ver.pack(side="left", padx=(dp(10), dp(0)))
        # ⚠️ 静止态底色**必须比页面底色 BG 明显深一点**（2026-10-04 实测踩到）：
        #    第一版这里写的是「BG 本身那个值」，于是按钮和背景完全融成一片，
        #    截图里就剩四个灰字，和普通说明文字毫无区别，用户根本不会去点。
        #    这类错**没有任何报错**，只有把截图放大才看得出来。
        #    ⚠️ 2026-10-17 换 Fluent 时**又差点踩一次**：新的 BG 是 `#f3f3f3`，
        #       而「落在页面灰底上的次要按钮」那档 `BTN_SOFT_ON_BG`(`#e9e9e9`)
        #       就是照着「和 BG 差 10 级」选的 —— 别再顺手改回浅色。
        #       `ui_contrast_test.py` 第 7 节把这一对钉着。
        #
        # 🔴 2026-10-16：原来这里还有个「使用说明」按钮，**删掉了**。
        #    它和左侧导航栏里的「使用说明」是同一个页面 —— 两个入口并列，
        #    用户会以为「这两处打开的不是一个东西」。导航栏是**唯一**的页面入口。
        #    ⚠️ 删掉之后**内容高度一个像素都没变**：这一行的高度由标题字号
        #    （`self.TITLE`，比这个按钮高）决定，不是由这个按钮。
        #    `content_h_probe.py` 复核过（仍是 827），`geometry_test.CONTENT_H` 不用动。

        # --- 状态条 ---
        # 圆角卡 + 彩色圆角图标徽章 + 文字。
        # 原来是「4px 竖条 + 小圆点 + 文字」—— 三样都在传达同一个颜色，既挤、
        # 又都做不大（竖条 4px、圆点 10px）。现在合成一个**会跟着状态变色**的
        # 图标徽章，和分区标题同一套造型：一眼能看出「这是状态」，
        # 也不会跟下面那块整块彩色底的提示消息混起来。
        #
        # ⚠️ 内边距 12/17 是量出来的：旧实现是「1px 描边 + pady=11」= 24，
        #    这里 2×12 = 24，**逐像素等价**。半径必须 <= 内边距（round_panel 会断言）。
        status_cv, status_body = round_panel(inner, tk, dp(17), dp(12), R_CARD)
        status_cv.pack(fill="x", pady=(dp(0), dp(10)))
        st_icon = tk.Canvas(status_body, width=dp(22), height=dp(22), bg=CARD,
                            highlightthickness=0, bd=0)
        st_icon.pack(side="left", padx=(dp(0), dp(10)))
        status_text = self.lbl(status_body, "正在检测网络状态…", self.BASE, bg=CARD, fg=FG)
        status_text.pack(side="left")
        self.st_icon = st_icon
        self.status_text = status_text

        # 「测一下速度」入口（2026-10-07 加）。
        #
        # 🔴 为什么**不新开一张卡**：设置窗口的高度直接等于内容高度，而内容一旦
        #    越过 `sh * SCREEN_H_RATIO`（本机 1707x1067 → 906px）就会冒出滚动条。
        #    当前内容 827px，再加一张卡（约 90px）就正好压线 —— 在他自己的机器上
        #    凭空多一条滚动条，而「开机自启」会掉到滚动区外面去。
        #    放进状态卡右侧既不用动高度，语义也对：它量的是「网络状态」这件事。
        #
        # 造型和「检查更新」「清除本机保存的账号密码」同一套（浅底 + 同色深字 +
        # hover 加深）—— 同样是「看得出能点」的圆角片，同样**不新增第四个主按钮**
        # （主按钮固定三个是界面铁律）。
        self.btn_probe = round_chip(
            status_body, tk, "测一下速度", (self.fam, self.SMALL), BLUE_DARK, BLUE_SOFT,
            height=self.SMALL + 14, hover=BLUE_HOVER,
            command=lambda: self.on_speed_probe())
        self.btn_probe.pack(side="right")

        # ⚠️ 「账号中心」的入口**搬到左侧导航栏了**（2026-10-16）。
        #    原来这里是状态卡右侧的一个圆角片（和「测一下速度」并排）——
        #    现在整个程序是「一个窗口多个页面」，导航栏是**唯一**的页面入口。
        #    留一个重复入口只会让人以为「这两个地方打开的不是同一个东西」，
        #    所以这里**故意不留**。
        # ⚠️ 删掉它之后这一行的高度**一个像素都没变**：行高由「测一下速度」
        #    那个圆角片决定（`self.SMALL + 14`），它还在。
        #    `content_h_probe.py` 复核过，`geometry_test.CONTENT_H` 不用动。

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
        # 🔴 学校下拉框挂在**标题行右侧**，而不是下面新起一行（2026-10-12）。
        #    为什么（这不是审美选择，是几何逼出来的）：
        #      设置窗口的高度**直接等于内容高度**，而 16:10 屏上
        #      `sh * SCREEN_H_RATIO` 只给到 906px。在下面新起一行要 80px
        #      （实测：标签 22 + 间距 5 + 选择框 41 + 间距 12），加完是 **907px**
        #      —— 正好压线，用户屏幕上凭空多一条滚动条、「开机自启」掉到滚动区外面。
        #      实测数字见 `content_h_probe.py`。
        #    挂在标题行右侧则是 **+0px**：标题行本来就 22px 高（徽章 22 + 标题字 22），
        #      只要选择片也不超过 22px，整行高度一点不变。
        #    ⚠️ 所以那个片的高度必须是 `linespace(SMALL)`（下面算出来的），
        #       不能图省事写死一个更大的数 —— 一旦超过 22，整行被撑高，
        #       内容高度跟着涨，而**这不会有任何报错**。
        self.sec(c_form, "账号信息", right=self.school_selector)
        self.lbl(c_form, "学号 / 账号", self.SMALL, fg=SUB).pack(anchor="w", pady=(dp(0), dp(5)))
        self.var_uid = tk.StringVar()
        self.entry_box(c_form, self.var_uid)
        self.lbl(c_form, "密码", self.SMALL, fg=SUB).pack(anchor="w", pady=(dp(12), dp(5)))

        # --- 密码框（带隐私保护）---
        # 规则都在 PasswordField 里（那样才能单独建窗口测交互），这里只负责摆放。
        self.pwd_field = PasswordField(c_form, tk, (self.fam, self.BASE),
                                       (self.fam, self.SMALL), FG)
        self.pwd_field.box.pack(fill="x")

        # --- 按钮（横排、右对齐，Win11 对话框的摆法）---
        # 🔴 三个主按钮是界面铁律：`set_busy` 的控件元组就是这三个，改数量要同步改它。
        #
        # 🔴 **造的顺序 = 从右往左**（2026-10-17）：`side="right"` 是「先 pack 的
        #    贴最右」，所以这里先 primary、最后 danger，视觉上从左到右才是
        #    「退出当前账号 / 仅保存 / 切换到此账号」—— 主按钮在最右，符合 Win11。
        #    ⚠️ 顺序写反**不报任何错**，只会让主按钮跑到最左边（变成 macOS 的
        #       规矩），所以这句话钉在这儿。见 `mkbtn` 的说明。
        btns = tk.Frame(c_form, bg=CARD)
        btns.pack(fill="x", pady=(dp(16), dp(0)))
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
                            justify="left", wraplength=dp(700), padx=dp(14), highlightthickness=1,
                            highlightbackground=LINE)
        self.msg.pack(fill="x", ipady=dp(10), pady=(dp(0), dp(10)))
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
                                  justify="left", wraplength=dp(680))

        # --- 隐私 / 清除本机凭据 ---
        # 为什么要有这个入口（2026-09-21）：以前清凭据只有命令行 `--purge`，
        # 对普通用户等于没有 —— 他要的是「别让这台电脑再留着我的密码」，
        # 不该逼他先学会开终端。
        # 做成和「检查更新」同款的小链接，**不新增第四个主按钮**（主按钮固定三个
        # 是界面铁律；这里也不进 set_busy 的控件元组 —— 忙碌拦截由 on_purge 自己做）。
        c_priv = self.card(pady=(dp(0), dp(0)))
        self.sec(c_priv, "隐私")
        self.lbl(c_priv, "账号密码只存在这台电脑上，不会上传到任何服务器。",
                 self.SMALL, fg=SUB).pack(anchor="w")
        # 同样换成圆角片（和版本号那行同一个理由：Label 只有直角）。
        # 和版本号那行同一个道理：光绑 cursor 是没用的 —— 截图里它跟普通灰字
        # 一模一样，用户根本不会去点。下划线 + 浅红底 + hover 加深，才看得出能点。
        self.btn_purge = round_chip(c_priv, tk, "清除本机保存的账号密码",
                                    (self.fam, self.SMALL, "underline"), DANGER, DANGER_SOFT,
                                    height=self.SMALL + 14, hover=DANGER_HOVER,
                                    command=lambda: self.on_purge())
        self.btn_purge.pack(anchor="w", pady=(dp(10), dp(0)))

        # --- 状态 ---
        self.state = {"busy": False, "queue": None, "buttons": None,
                      # 升级用的临时状态。都要走界面线程，所以放在这里而不是局部变量。
                      "pending": None, "dl_text": "",
                      # 账号列表里每行的「删除」按钮。render_accounts 每次重建列表都会换一批，
                      # 所以这里存的是**当前这一批**。set_busy 必须能拿到它们 —— 见下面的说明。
                      "row_btns": []}

    # ------------------------------------------------------------ 页面 / 导航

    # 🔴 **加新功能就来这里加一行**，别再开新窗口。
    #    (key, 导航栏文字, 图标)。图标必须是 `_ICON_SHAPES` 里有的名字
    #    （写错了 `nav_button` 会当场抛异常，不会静默画个空白）。
    # ⚠️ 「账号中心」只有郑航有（嘉兴是深澜 Srun），`_build_nav` 会自己跳过。
    PAGES = (
        ("settings", "设置", "power"),
        ("center", "账号中心", "user"),
        ("help", "使用说明", "list"),
    )

    def _build_nav(self):
        """搭左侧导航栏。

        ⚠️ 导航面板的 `pad_x`/`pad_y` **必须 >= radius**（`round_panel` 的硬约束，
           不满足会当场抛异常）—— 这里取 12/12 配 `R_CARD`(8)。

        🔴 2026-10-17 换 Fluent：面板底色从 `CARD`（白）改成 `NAV_BG`（比内容区
           深一档的灰），且**描边同色**（= 看不见描边）。Win11 的导航窗格是
           和窗口同色的一整条，不是一张浮在上面的白卡片。
        """
        cv, body = round_panel(self.nav, self.tk, dp(12), dp(12), R_CARD,
                               fill=NAV_BG, line=NAV_BG)
        cv.pack(fill="both", expand=True)
        self.nav_btns = {}
        # ⚠️ 两侧内边距要和上面 `round_panel` 的 `pad_x` 一致（都是 dp(12)），
        #    否则导航项会伸进圆角里、或者右边空出一截。
        inner_w = NAV_W - 2 * dp(12)
        h = self.BASE * 2 + 18
        for key, label, icon in self.PAGES:
            if key == "center" and not self_service_available():
                continue
            b = nav_button(body, self.tk, label, icon, (self.fam, self.BASE),
                           (lambda k=key: self.show_page(k)),
                           width=inner_w, height=h, page=NAV_BG)
            b.pack(fill="x", pady=(dp(0), dp(4)))
            self.nav_btns[key] = b

    def show_page(self, key):
        """切到某一页。

        🔴 **这是「一个窗口多个页面」的落点。** 以前每个功能开一个 Toplevel，
           现在功能都在这个窗口的内容区里进出 —— 同时看好几个功能也不会
           「满屏都是窗口」。

        ⚠️ 切页**不销毁**别的页（只 `pack_forget`）：账号中心里那个内存会话
           （cookie）和本机历史都挂在面板对象上，销毁了就得重新读一次验证码。
           真正要销毁的场合（账号换了 / 清了凭据）走 `_drop_center()`。
        """
        if key not in self.nav_btns:
            return
        if key == "center":
            target = self._ensure_center()
            if target is None:
                # 账号密码没填全 / 面板建不起来。提示 `_ensure_center` 已经弹过了，
                # 这里退回设置页 —— 别把用户丢在一张空白页上（那看着像程序坏了）。
                key, target = "settings", self.page_settings
        elif key == "help":
            target = self._ensure_help()
        else:
            target = self.page_settings
        # ⚠️ `self.page` / 高亮都放在**最后**统一设：`_ensure_center()` 可能
        #    间接再进一次 `show_page`（重建面板那条路），先设的话会被它改掉。
        self.page = key
        for k, b in self.nav_btns.items():
            b.set_active(k == key)
        for p in (self.page_settings, self.page_center, self.page_help):
            if p is not None and p is not target:
                p.pack_forget()
        target.pack(fill="both", expand=True)

    def _center_creds(self):
        """取「账号中心」要用的 (账号, 密码, 门户地址)。取不到就提示并返回 None。

        🔴 用的是**界面上填的那份账号密码**（没填就退回已保存的那份）——
           自助服务要用同一份凭据去登录，不该再问用户要一遍。
           这也意味着：**界面上填的是哪个账号，看到的就是哪个账号的数据**。
        """
        cfg = load_config()
        uid = (self.var_uid.get() or "").strip() or (cfg.get("userId") or "")
        pwd = self.pwd_field.get() or (cfg.get("passwd") or "")
        if not uid or not pwd:
            self.show("「账号中心」要用账号密码登录学校自助服务。\n"
                      "请先在上面把账号密码填好（或保存一个账号）再点。", "err")
            return None
        base = (cfg.get("portalHost") or DEFAULTS["portalHost"]).rstrip("/")
        return uid, pwd, base

    def _ensure_center(self):
        """按需建「账号中心」那一页。建不成就返回 None。

        🔴 **懒建**：面板一建出来就会挂一个 `after()` 轮询和一个后台线程，
           启动时不该干这事（用户可能根本不点它）。
        """
        creds = self._center_creds()
        if creds is None:
            return None
        uid, pwd, base = creds
        if self.page_center is not None:
            # 账号没换 ⇒ 沿用旧面板（内存会话还在，不用重新读验证码）。
            if getattr(self.panel, "account", None) == uid:
                return self.page_center
            # 🔴 账号换了就必须重建：面板里的会话和本机历史都是**按账号**的，
            #    沿用旧面板会显示上一个账号的数据 —— 而两边的数字都"看着正常"，
            #    用户根本看不出错（这正是账号卡顶上要写清账号的原因）。
            self._drop_center(go_settings=False)
        box = self.tk.Frame(self.content, bg=BG)
        # 🔴 **先摆进布局，再建面板**。面板在构造时就要量正文宽度
        #    （决定分几列、每一行文字多宽换行），没摆进布局的话量到的是 1px，
        #    只能退回兜底的旧宽度 —— 表现是「打开的一瞬间是单列，
        #    两百毫秒之后才跳成两列」。不报错，但一眼就看得出来是毛病。
        box.pack(fill="both", expand=True)
        box.update_idletasks()
        try:
            panel = SelfServiceWindow(self.root, self.tk, self.fam, self.BASE,
                                      uid, pwd, base, host=box)
        except Exception:
            log("打开账号中心面板失败:\n%s" % traceback.format_exc())
            self.show("打不开「账号中心」面板。", "err")
            box.destroy()
            return None
        self.page_center = box
        self.panel = panel
        return box

    def _drop_center(self, go_settings=True):
        """把「账号中心」这一页整个丢掉（账号变了 / 清了凭据时用）。

        ⚠️ 正在看这一页时要**先切回设置页** —— 否则用户会盯着一张已经
           被销毁的空白页，还以为程序卡了。
        ⚠️ `go_settings=False` 是给 `_ensure_center()` 自己用的：
           它是**在 `show_page("center")` 里被调用的**，那里再套一层
           `show_page("settings")` 会把页面的最终状态搞乱（外层回来之后
           又会把 center 页 pack 上，导航高亮和实际显示对不上）。
        """
        if self.panel is not None:
            try:
                self.panel.close()
            except Exception:
                log("收起账号中心面板异常:\n%s" % traceback.format_exc())
        self.panel = None
        if self.page_center is not None:
            try:
                self.page_center.destroy()
            except Exception:
                pass
        self.page_center = None
        if go_settings and self.page == "center":
            self.show_page("settings")

    def _ensure_help(self):
        """按需建「使用说明」那一页。

        ⚠️ 正文和按钮栏都走模块级的 `text_panel` / `text_panel_bar` ——
           和命令行 `--help` 那个独立窗口共用**同一份实现**。
           2026-10-04 圆角化时只改了一份、症状是「弹窗截图字节一字没变」，
           那次教训就是因为存在第二份实现。
        """
        if self.page_help is not None:
            return self.page_help
        self.page_help = self.tk.Frame(self.content, bg=BG)
        text_panel(self.page_help, self.tk, self.fam, self.BASE,
                   HELP_TEXT + SWITCH_HELP, pad=0)
        # ⚠️ 按钮写「返回设置」而不是「知道了」：「知道了」是弹窗的说法，
        #    嵌在页面里会让人以为点了会关掉什么。
        text_panel_bar(self.page_help, self.tk, self.fam, self.BASE,
                       lambda: self.show_page("settings"),
                       label="返回设置", pad=0)
        return self.page_help

    # ------------------------------------------------------------ 通用小控件

    def lbl(self, parent, text, size=None, bold=False, fg=FG, bg=CARD, **kw):
        return self.tk.Label(parent, text=text, bg=bg, fg=fg,
                             font=(self.fam, size or self.BASE,
                                   "bold" if bold else "normal"), **kw)

    def card(self, pady=(dp(0), dp(9)), padx=CARD_PAD_X, ipady=CARD_PAD_Y, radius=CARD_R):
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

    def sec(self, parent, text, right=None):
        """分区标题：圆角彩色图标徽章 + **加粗近黑**的标题字（Win11 的层次画法）。

        🔴 2026-10-17 改：标题字号 `SMALL` → `SEC_FS`（= 正文同号），
           颜色 `SEC_FG` → 近黑。原来它是「比正文还小一号的灰字」，
           一屏看下去标题压不住正文，就是用户说的「层次不清」。
           Win11 的分区标题靠**字重**拉开层次，不靠放大、更不靠换颜色。

        ⚠️ 高度**不再是固定 22px** 了：标题行高 = `max(徽章 22, 标题字行高)`，
           字号一涨行高就跟着涨。所以改 `SEC_FS` 必须同步：
             · `title_row_h()` —— 它按字号算，会自动跟；
             · `geometry_test.CONTENT_H` —— **这个不会自动跟，必须重量**。
           漏掉后者的表现是「窗口比内容矮一点」，底下那张卡被静默裁掉一截。

        `right` 是一个**工厂函数**，不是控件：`sec` 收下它、把「标题行」这个 Frame
        交给它，由它现场造出要右对齐的控件并返回。
        🔴 为什么必须是工厂（2026-10-12 踩到）：Tk 里**控件的 master 在创建时就定死了**，
           `pack()` 只会把它放进自己的 master —— 先在别处建好、再交给这里 pack，
           它**不会**跑到标题行里去，而且**不报任何错**，只是位置不对。
        ⚠️ `right` 造出来的控件**也必须 <= 标题行高**，否则整行被撑高 ——
           这类错同样**不报任何异常**，只表现为「窗口莫名其妙变高了」。
           现在那个高度是 `title_row_h()` 算出来的，别再按老值 22 写死。
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
        # ⚠️ 原来是 `anchor="w"`。改成 `fill="x"` 才能把 right 推到最右边；
        #    对没有 right 的分区没有任何视觉影响（子控件本来就是左对齐的）。
        head.pack(fill="x", pady=(dp(0), dp(7)))
        icon_badge(head, self.tk, icon, fg, soft).pack(side="left")
        self.lbl(head, text, self.SEC_FS, True, SEC_FG).pack(side="left", padx=(dp(8), dp(0)))
        if right is not None:
            right(head).pack(side="right")
        return head

    def title_row_h(self):
        """分区标题那一行的高度（= `linespace(SEC_FS)`，按字体算出来的）。

        学校选择片必须**恰好**这么高，才不会把标题行撑高（见 sec / school_selector）。
        按字体算而不是写死数字 —— 换字号时会自己跟着走。
        🔴 2026-10-17：跟着 `SEC_FS` 走，不再是 `SMALL`（标题字号涨了一档）。
        """
        import tkinter.font as tkfont
        try:
            return int(tkfont.Font(font=(self.fam, self.SEC_FS)).metrics("linespace"))
        except Exception:
            return self.SMALL + 13

    def school_selector(self, head):
        """造出「学校 [郑州航空工业管理学院 ▾]」，挂在「账号信息」标题行右侧。

        🔴 为什么不新起一行（2026-10-12）：设置窗口的高度**直接等于内容高度**，
           而 16:10 屏上 `sh * SCREEN_H_RATIO` 只给到 906px。新起一行要 80px
           （实测：标签 22 + 间距 5 + 选择框 41 + 间距 12），加完是 **907px** ——
           正好压线，用户屏幕上凭空多一条滚动条、「开机自启」掉到滚动区外面。
           挂在标题行右侧是 **+0px**：那行本来就 22px 高。实测见 `content_h_probe.py`。

        ⚠️ 不用 `tk.OptionMenu`：它是**直角**的，跟一屏圆角控件不搭
           （和当初把 Label 换成 round_chip 是同一个理由）。
        """
        row = self.tk.Frame(head, bg=CARD)
        self.lbl(row, "学校", self.SMALL, fg=SUB).pack(side="left", padx=(dp(0), dp(6)))
        # 文字里带个 ▾ —— 一眼看出「这里能点开」。少了它，浅底小片看着就是个静态标签。
        self.var_school = self.tk.StringVar(value=school_key())
        holder = {}
        self.chip_school = round_chip(
            row, self.tk, self._school_chip_text(), (self.fam, self.SMALL),
            BLUE_DARK, BLUE_SOFT, page=CARD, pad_x=dp(11), height=self.title_row_h(),
            hover=BLUE_HOVER, command=lambda: self._open_school_menu(holder["cv"]))
        holder["cv"] = self.chip_school
        self.chip_school.pack(side="left")
        return row

    def _school_chip_text(self):
        """选择片上那行字。末尾的「▾」是**唯一**的「这里能点开」的提示 ——
        去掉它，这块浅蓝小片看着就是个静态标签（和「当前使用」那种纯标签分不开）。

        ⚠️ `▾` 前面留两个空格：那个字形本身的左右留白很小，
           贴着学校名会看成「院▾」糊在一起（放大截图核对过）。
        """
        return "%s  ▾" % school_name()

    def _open_school_menu(self, anchor):
        """弹出学校菜单。`anchor` 是那块圆角片（菜单贴在它正下方）。"""
        menu = self.tk.Menu(self.root, tearoff=0, font=(self.fam, self.BASE),
                            bg=CARD, fg=FG, activebackground=BLUE_SOFT,
                            activeforeground=BLUE_DARK)
        for k, name in school_choices():
            menu.add_radiobutton(label=name, value=k, variable=self.var_school,
                                 command=lambda kk=k: self.on_school(kk))
        try:
            menu.tk_popup(anchor.winfo_rootx(),
                          anchor.winfo_rooty() + anchor.winfo_height())
        finally:
            menu.grab_release()

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
        cv, box = round_panel(parent, self.tk, dp(8), dp(6), R_CTRL, fill=FIELD)
        cv.pack(fill="x")
        e = self.tk.Entry(box, font=(self.fam, self.BASE), relief="flat", bd=0,
                          bg=FIELD, fg=FG, highlightthickness=0, insertbackground=FG,
                          textvariable=textvariable)
        e.pack(fill="x", padx=dp(3), ipady=dp(2))
        e.bind("<FocusIn>", lambda ev: panel_set_line(cv, BLUE))
        e.bind("<FocusOut>", lambda ev: panel_set_line(cv, LINE))
        return cv, e

    # 主按钮之间的水平间距（设计像素）。Win11 的对话框按钮之间就是这么大。
    BTN_GAP = 8

    def mkbtn(self, parent, text, cmd, kind="ghost"):
        """造一个主按钮，**横排、右对齐、宽度跟着文字走**（Win11 对话框的摆法）。

        🔴 2026-10-17 改的摆法：原来是 `fill="x"` + 纵向排列 —— 三个按钮各占
           一整行、横贯卡片，一共吃掉 134px 高。用户原话「可以让它横着排版，
           充分发挥电脑优势」。现在横排右对齐：高度只占一行（40px），
           省下的 ~94px 全给了内容区。

        ⚠️ **右对齐的顺序靠 pack 顺序反着来**：`side="right"` 的控件是
           「先 pack 的贴最右」，所以调用方必须先造主按钮（primary）、
           最后造 danger —— 视觉上从左到右才是「退出 / 仅保存 / 切换到此账号」，
           主按钮落在最右边，正是 Win11 的规矩（确认键在右）。
           调用方顺序写反了**不会报错**，只会让主按钮跑到最左边 ——
           所以 `_card_form` 那边有一句注释钉着这件事。

        ⚠️ `pack` 里**不能带 `fill="x"`**：`round_button` 已经按文字量好了宽度
           （见它内部 `measure(text) + pad_x`），一旦 `fill="x"` 就会把这个
           宽度覆盖掉，又变回通栏 —— 而且不报任何错。
        """
        style = {
            "primary": (BLUE, "#ffffff", BLUE_DARK, BLUE_ACTIVE),
            "ghost":   (BTN_SOFT, BTN_FG, BTN_HOVER, BTN_ACTIVE),
            "danger":  (DANGER_SOFT, DANGER, DANGER_HOVER, DANGER_ACTIVE),
        }[kind]
        # 高度按字号算（换字号时自己跟着走）。半径走圆角规范里的「控件」那一档，
        # 不再用胶囊 —— Win11 的按钮是**小圆角**，不是两头全圆的胶囊。
        h = self.BASE * 2 + 14
        b = round_button(parent, text, cmd, (self.fam, self.BASE),
                         bg=style[0], fg=style[1], hover=style[2], active=style[3],
                         parent_bg=CARD, height=h, radius=R_CTRL)
        b.pack(side="right", padx=(dp(self.BTN_GAP), dp(0)))
        return b

    # ------------------------------------------------------------ 状态与刷新

    def on_wheel(self, ev):
        """滚轮：按**当前页**决定滚哪个可滚动区。

        ⚠️ 2026-10-16 起窗口里不止一个可滚动区（设置页一个、账号中心页一个）——
           还像原来那样只滚 `self.canvas` 的话，在账号中心页滚轮**完全无效**，
           而且不报任何错（滚的是看不见的那一块）。这类「静默无效」正是
           本项目最忌讳的失败方式，所以这里显式按页分派。
        ⚠️ 说明页走 `tk.Text`，**它自带滚轮支持**，这里不掺和 ——
           掺和进去就会连带把看不见的设置页也滚一遍。
        """
        if self.page == "center" and self.panel is not None \
                and not self.panel.closed:
            pairs = ((self.panel.canvas, self.panel.holder),)
        elif self.page == "settings":
            pairs = ((self.canvas, self.holder),)
        else:
            return
        for cv, holder in pairs:
            # 内容比视口高才滚，否则别抢事件（短内容时滚轮该干嘛干嘛）
            try:
                if holder.winfo_reqheight() > cv.winfo_height():
                    cv.yview_scroll(-1 if ev.delta > 0 else 1, "units")
            except Exception:
                pass

    def draw_status_icon(self, st):
        """按状态重画徽章（浅底 + 同色系图标）。

        浅底和图标色**必须成对换** —— 只换图标色的话，红图标压在浅绿底上
        看着就是「画错了」，而这类错在截图里不一定显眼，只能靠这里锁死。

        🔴 尺寸一律走 `dp()`（2026-10-17 修）：画布是 `dp(22)` 建的，原来这里
           却用裸的 `22 / 11 / 7 / 13` 画 —— 150% 的屏上等于在一个 33×33 的
           画布左上角画了个 22×22 的小方块，**又小又是偏的**。
           这类「容器跟着 DPI 变、内容不变」的错在 100% 屏上完全看不见。
        """
        col = NET_COLOR.get(st, MUTED)
        soft = NET_SOFT.get(st, NEUTRAL_SOFT)
        b = dp(22)
        self.st_icon.delete("all")
        _draw_round_rect(self.st_icon, 0, 0, b, b, dp(7), soft)
        _draw_icon(self.st_icon, "globe", col, b / 2.0, b / 2.0, size=dp(14))

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
            self.warn_auto.pack(anchor="w", pady=(dp(8), dp(0)))
        elif old_args:
            # 0.2.0 装的 .lnk 参数是 `--auto`，没有 `--guard` —— 开机照常登录，
            # 但掉线后不会自动重连。不提示的话用户会以为新版本没修好。
            self.warn_auto.configure(
                text="⚠ 开机自启还是「旧设置」：开机照常登录，但掉线后不会自动重连。\n"
                     "   把下面的勾取消、再重新勾选一次即可升级。")
            self.warn_auto.pack(anchor="w", pady=(dp(8), dp(0)))
        else:
            self.warn_auto.pack_forget()

    def show(self, text, kind="info"):
        color = {"ok": (OK_SOFT, OK_FG),
                 "err": (DANGER_SOFT, DANGER_FG),
                 "info": (BLUE_SOFT, BLUE_FG)}[kind]
        self.msg.configure(text=text, bg=color[0], fg=color[1])
        self.msg.pack(fill="x", ipady=dp(10), pady=(dp(0), dp(10)))

    def set_busy(self, b):
        self.state["busy"] = b
        st = "disabled" if b else "normal"
        # 「测一下速度」也在这条链上（2026-10-07 加）。它不在「三个主按钮」那条
        # 界面铁律里（它是状态卡里的圆角片，和「检查更新」同一类），但必须跟着灰：
        # 忙碌时 start_job 是**静默 return** 的 —— 用户点了没反应，只会以为坏了。
        for w in (self.btn_switch, self.btn_save, self.btn_out, self.btn_probe):
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
                     fg=SUB).pack(anchor="w", padx=dp(14), pady=dp(14))
            return
        for i, uid in enumerate(ids):
            a = store["accounts"][uid]
            row = self.tk.Frame(self.list_inner, bg=CARD)
            row.pack(fill="x")
            if i:
                self.tk.Frame(self.list_inner, bg=LINE, height=dp(1)).pack(fill="x")
            left = self.tk.Frame(row, bg=CARD)
            left.pack(side="left", fill="x", expand=True, padx=dp(14), pady=dp(9))
            line = self.tk.Frame(left, bg=CARD)
            line.pack(anchor="w")
            self.lbl(line, uid, self.BASE, True).pack(side="left")
            if uid == cur:
                round_chip(line, self.tk, "当前使用", (self.fam, self.SMALL - 1),
                           BLUE_DARK, BLUE_SOFT).pack(side="left", padx=(dp(8), dp(0)))
            if uid == store.get("lastOnline"):
                round_chip(line, self.tk, "上次登录", (self.fam, self.SMALL - 1),
                           OK_FG, OK_SOFT).pack(side="left", padx=(dp(4), dp(0)))
            # 🔴 这个账号是在**另一所学校**下保存的（2026-10-12 加）。
            #    账号库两校共用，所以列表里会混着别的学校的账号 ——
            #    点错一次就会把 A 校的密码发到 B 校的门户（把密码交给第三方）。
            #    所以必须**看得见**：这里给一个警示色小标签，on_switch 里还会再问一句。
            #    ⚠️ 只在「不一样」时才显示 —— 老账号没有这个标记（`school` 缺失），
            #       按「跟当前一致」处理，不打扰用户。
            #    ⚠️ 高度不受影响：round_chip 只有 18px，比行高低，
            #       靠 pack(side="left") 垂直居中，不会动 CONTENT_H。
            #    ⚠️ 直接读上面已经取好的 `store`，**不要再调 account_school()** ——
            #       那个函数会重新读一遍 accounts.json，每个账号一行就是一次文件读；
            #       更要紧的是中途别的进程写一下，列表里就会出现「同一次渲染、
            #       两个不同时刻的数据」。
            a_school = a.get("school") if isinstance(a.get("school"), str) else ""
            if a_school and a_school != school_key():
                round_chip(line, self.tk, school_name(a_school),
                           (self.fam, self.SMALL - 1), WARN, WARN_SOFT
                           ).pack(side="left", padx=(dp(4), dp(0)))
            if a.get("lastSuccess"):
                self.lbl(left, "上次成功：%s" % a["lastSuccess"], self.SMALL - 1,
                         fg=SUB).pack(anchor="w", pady=(dp(3), dp(0)))
            right = self.tk.Frame(row, bg=CARD)
            right.pack(side="right", padx=dp(14))
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
                         bg=BTN_SOFT, fg=BTN_FG, hover=BTN_HOVER, active=BTN_ACTIVE,
                         parent_bg=CARD, height=rh, radius=R_CTRL, pad_x=dp(26)
                         ).pack(side="left", padx=(dp(0), dp(6)))
            # 「删除」原来的静止态是**白底**，在白卡片上完全看不出是个按钮
            # （截图放大后才发现的）。给一层很浅的红：既看得出能点，
            # 又不会像「退出当前账号」那样抢眼。
            b_del = round_button(right, "删除", lambda u=uid: self.drop(u),
                                 (self.fam, self.SMALL),
                                 bg=DANGER_SOFT, fg=DANGER, hover=DANGER_HOVER,
                                 active=DANGER_ACTIVE, parent_bg=CARD,
                                 height=rh, radius=R_CTRL, pad_x=dp(26))
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
        self._sync_school()
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
        elif kind == "upd_download_failed":
            # 参数：失败原因 + 建议用浏览器打开的地址（见 start_upgrade）。
            #
            # 为什么要在界面线程里单独处理、而不是并进 upd_result：
            # 这里要弹一个询问框（后台线程不能弹），而且弹完之后**程序还在跑** ——
            # 和 upd_result 那些「一次性的结论」不是一回事。
            reason, url = payload
            self.show("下载失败：%s" % reason, "err")
            try:
                if self.messagebox.askyesno(
                        APP_TITLE,
                        "自动下载没有成功。\n\n%s\n\n是否用浏览器打开下载页？\n\n"
                        "浏览器下载支持断点续传（网络断了能接着下），"
                        "比程序内下载更稳。" % reason):
                    if open_in_browser(url):
                        self.show("已用浏览器打开下载页：\n%s\n\n"
                                  "下好之后，把它改名成和原来一样的文件名、"
                                  "覆盖掉原文件即可（账号密码不受影响）。" % url,
                                  "info")
                    else:
                        self.show("打不开浏览器。可以手动复制这个地址去下载：\n%s"
                                  % url, "info")
            except Exception:
                log("处理下载失败提示异常:\n%s" % traceback.format_exc())
            finally:
                # 和 upd_result 同一个理由：任何一条路径都不能让界面卡在忙碌态，
                # 否则三个按钮全灰、用户只能关掉重开。
                self.set_busy(False)
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
                elif st == updater.STATE_CURRENT:
                    # 「已是最新」也**明确告诉用户**（2026-10-11 用户反馈：点完只在
                    # 状态栏看到一行小字，以为「什么都没发生」，进而怀疑功能坏了）。
                    # 用弹框而不是只改状态栏：用户是**主动点**的，不算打扰；
                    # 而这一条恰恰是「点完没反应」的重灾区 —— 弹窗是唯一看漏不了的反馈。
                    # 顺序：先写好状态栏、解除忙碌，最后弹框（弹框会阻塞在这里等用户点）。
                    self.show(updater.describe(st, info), "ok")
                    self.set_busy(False)
                    self.messagebox.showinfo(
                        APP_TITLE,
                        "已是最新版本 v%s。\n\n%s" % (VERSION, updater.source_hint()))
                else:
                    # 结果里带上更新源：用户是自己点「检查更新」才看到这行的，
                    # 不存在打扰；而他有权知道这程序在跟哪个地址说话。
                    self.show(updater.describe(st, info) + "\n" + updater.source_hint(),
                              "info")
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

                # 进度状态**必须是调用方持有的可变容器**，不能挂在回调函数对象上。
                # 见 on_retry 那条注释：函数属性跨重试不重置，界面会卡住不动。
                prog = {"last": -1, "t0": time.perf_counter()}

                def on_prog(got, total):
                    # 节流和文案都在 dl_progress_text 里（纯函数，能被脚本断言）。
                    txt = dl_progress_text(prog, got, total)
                    if txt:
                        self.state["queue"].put(("upd_progress", txt))

                def on_pick(url, n, total):
                    # 说清「现在从哪儿下、这是第几个源」。多源之后这一点更重要：
                    # 用户有权知道程序在跟哪个地址说话（同 source_hint 的理由）。
                    host = urlparse(url).hostname or url
                    head = "下载中" if n == 1 else "换源"
                    self.state["queue"].put(("upd_progress", "%s：从 %s 下载（%d/%d）…"
                                             % (head, host, n, total)))

                def on_retry(attempt, attempts, why):
                    # 重试必须说一句：否则进度会卡在某个百分比不动，
                    # 用户以为死机了就会再点一次 —— 那就并发下两份、还可能互相覆盖。
                    #
                    # 进度状态一并归零（2026-10-08 修）：重试是从 0 重新下，
                    # 而 `last` 还停在上一轮的最高百分比上。⚠️ dl_progress_text
                    # 现在自己也能认出来（进度回退 = 新的一轮），这里是显式说一遍
                    # 意图 —— 那个自动判断是给「忘了调」的情况兜底的。
                    dl_progress_reset(prog)
                    self.state["queue"].put(("upd_progress", "网络中断，正在重试 %d/%d…"
                                             % (attempt, attempts)))

                # 下载这件事本身（先试增量、不行再走多源完整包）在 updater 里，
                # 不在这儿 —— 那段判断只在补丁路上出岔子时才走到降级分支，
                # 塞在闭包里就只能靠点界面撞运气，而它有完整的测试（patch_test.py）。
                # 界面只负责：把进度转成消息、把失败转成「要不要用浏览器下」的询问。
                ok, reason, used = updater.download_update(
                    exe, info, VERSION, dest, work_dir=work_dir,
                    on_progress=on_prog, on_retry=on_retry, on_pick=on_pick,
                    on_stage=lambda t: self.state["queue"].put(("upd_progress", t)),
                    on_log=log)
                if not ok:
                    # 下载失败时 download() 自己会清掉 `.part`，但 `.new` 可能
                    # 是上一轮留下的（下载成功、替换失败）。12 MB 的东西别留在盘上。
                    updater.discard(dest)
                    log("更新下载失败：%s" % reason)
                    # 交给界面线程去问「要不要用浏览器下」—— messagebox 不能在
                    # 后台线程弹。顺带把这次试过的源带上：浏览器下载支持断点续传，
                    # 网络不稳时它比程序内下载可靠得多。
                    self.state["queue"].put(("upd_download_failed", (
                        reason, browser_fallback_url(info, used))))
                    return
                log("更新包下载完成并校验通过：%s（来自 %s）" % (dest, used))

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
            t0 = time.perf_counter()
            try:
                st, info = updater.check(VERSION)
            except Exception:
                log("检查更新异常:\n%s" % traceback.format_exc())
                st, info = updater.STATE_UNKNOWN, "检查更新时出错"
            # 🔴 这条日志以前**根本没有**（2026-10-11 用户报「检查更新很慢」时才发现）：
            #    成功路径一行都不写 ⇒ 事后完全无法取证，只能靠外部复现，
            #    而「慢」这种问题恰恰只在用户那台机器、那条线路上才显形。
            #    记四件事：本机版本 / 结果 / 耗时 / 取的哪个地址 ——
            #    一眼能分出是「网络慢」还是「程序卡住」。
            log("检查更新：本机 v%s → %s，耗时 %.1f 秒（%s）"
                % (VERSION, st, time.perf_counter() - t0, updater.MANIFEST_URL))
            if st == updater.STATE_UNKNOWN:
                log("  没拿到清单的原因：%s" % (info,))
            self.state["queue"].put(("upd_result", (st, info)))
        threading.Thread(target=work, daemon=True).start()

    def on_speed_probe(self):
        """点「测一下速度」触发。

        测的是**到发布站的下载速度** —— 也就是「我这台机器能不能顺畅下到这个
        软件」，不是宽带标称带宽。为什么不去调中科大测速网，见 speed_probe
        上面的说明和 docs/DEVELOPMENT.md「测速」一节。

        走 start_job 这条通用后台任务：跑完由 handle_msg 统一收尾
        （load_all + show + refresh_status + set_busy(False)），
        不需要像「检查更新」那样自定义消息类型。
        """
        if self.state["busy"]:
            self.show("正在处理上一个操作，请稍候", "info")
            return
        self.start_job("正在测速…（约 3 秒）", self._speed_job)

    def _speed_job(self):
        """后台线程里跑。返回 (code, 给用户看的文案)。

        ⚠️ 失败时**必须说清「测的是到发布站的速度」**：网络正常但发布站暂时
           连不上时也会失败，而用户看到的只是一句「失败」—— 不说清他会以为
           自己网断了，然后去折腾校园网认证。
        """
        ok, info = speed_probe()
        if not ok:
            return (-1, "测速失败：%s\n"
                        "（这里测的是到发布站的下载速度，网络正常但发布站连不上时"
                        "也会失败）" % info)
        host = urlparse(updater.BASE_URL_HINT).netloc
        return (0, "测速完成（到 %s 的下载速度）\n%s" % (host, speed_describe(info)))

    def on_self_service(self):
        """点导航栏的「账号中心」触发（郑航自助服务）。

        ⚠️ 和 `on_speed_probe` 不同，这里不走 `start_job` ——
           面板有自己的线程和队列，`start_job` 那套（收尾只改状态栏文字）
           管不了它。忙碌拦截因此要自己做。
        ⚠️ 2026-10-16：从「开一个窗口」改成「切到一页」。
           账号密码的校验和面板的创建都在 `_ensure_center` 里，
           这里只负责「忙不忙」和「切页」。
        """
        if self.state["busy"]:
            self.show("正在处理上一个操作，请稍候", "info")
            return
        self.show_page("center")

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
        # 带上当前学校 —— 账号库是两所学校共用的，必须记得住「这个账号属于谁」，
        # 否则切错学校就会把 A 校的密码发到 B 校的门户（见 upsert_account 的说明）。
        upsert_account(uid, pwd, overwrite, school=school_key())

    def _sync_school(self):
        """把学校选择片拉回「配置文件里真实的值」。

        为什么要有这一步：用户点完菜单，`variable` 已经被改成新值了，
        但**写盘可能没成功**（比如正忙被拦下）。不同步的话界面显示新学校、
        配置里还是旧学校 —— 又是一处静默说谎。
        """
        try:
            k = school_key()
            self.var_school.set(k)
            self.chip_school.configure(text=self._school_chip_text())
        except Exception:
            pass                      # 界面正在销毁（关窗那一刻），画不了就算了

    def on_school(self, key):
        """学校下拉框选中了新的一项。"""
        if self.state["busy"]:
            self.show("正在处理上一个操作，请稍候", "info")
            self._sync_school()       # 把下拉框弹回真实值
            return
        old = school_key()
        if key == old:
            return
        new_key, dropped = set_school(key)
        self._sync_school()
        # 「清掉化石键」这件事必须说出来 —— 它是**修好了一个旧 bug**，
        # 而不是日常动作；不吭声的话用户会以为程序动了他的配置。
        note = ""
        if dropped:
            note = ("\n（顺带清掉了老版本自动写进配置文件的 %d 个旧参数：%s。"
                    "它们会让「换学校」这个动作不生效。）"
                    % (len(dropped), "、".join(dropped)))
        self.show("已切换到「%s」。\n下次登录、以及开机自启，都会走这所学校的认证门户。%s"
                  % (school_name(new_key), note), "ok")
        # 账号行上「属于别的学校」的标记要跟着变
        self.render_accounts()
        # 校园网段变了 → 状态栏那句「已连接校园网 / 未连接」得重新判
        self.refresh_status()

    def on_save(self):
        f = self.form()
        if not f:
            return
        update_config({"userId": f[0], "passwd": f[1]})
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
        # 🔴 **「账号中心」那一页也要整个丢掉**（2026-10-16 加）：
        #    它内存里存着一份**已经登录的会话 cookie**，还有拉回来的详单 /
        #    费用记录（带身份证号那种）。清凭据却留着这些，
        #    等于「用户以为清干净了、其实还挂着一个能看数据的页面」。
        #    这条是隐私问题，不是洁癖。
        self._drop_center()
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
        # 🔴 这个账号是在**另一所学校**下保存的 → 动手之前先把话说清楚。
        #    账号库是两所学校共用的，点错一次就会把 A 校的密码发到 B 校的
        #    门户服务器上 —— 那是把密码交给第三方，不能靠用户自己反应过来。
        #    ⚠️ 只在**有标记且和当前不一致**时才问：老账号没有标记（返回空串），
        #       按「跟当前一致」处理，不然每个老用户都会被多问一句。
        a_school = account_school(uid)
        if a_school and a_school != school_key():
            if not self.messagebox.askyesno(
                    APP_TITLE,
                    "账号 %s 是在「%s」下保存的，而当前选的是「%s」。\n\n"
                    "继续的话，它的密码会被发到「%s」的认证门户。\n\n"
                    "如果这是选错了学校，请先在上面的「学校」里改回去。\n"
                    "（如果确实要用它，点一次「仅保存」就能把标记改成当前学校。）\n\n"
                    "确定继续吗？"
                    % (uid, school_name(a_school), school_name(),
                       school_name())):
                return
        prev = load_config().get("userId") or ""
        update_config({"userId": uid, "passwd": pwd})
        self.upsert(uid, pwd, False)            # 不覆盖已存密码，回滚要靠它
        self.start_job("切换账号中，可能需要等待十几秒…",
                       self.action_runner(lambda: do_switch(uid, pwd, prev)))

    def show_help(self):
        """切换到「使用说明」这一页，返回那一页的 Frame。

        说明文字内置在 HELP_TEXT 里，所以程序不依赖外部 .txt —— 单独一个 exe
        拷到别的电脑上，照样翻得开说明。

        ⚠️ 2026-10-16：从**弹窗**改成**一页**。用户要的是「少开窗口」，
           而且这个说明是随手翻的东西，挂在一个单独窗口上反而挡着后面的设置。
           实现搬到 `_ensure_help()`，这里只做转发 ——
           `ui_shot.py` / `run()` 的自检仍按这个名字调。
        """
        self.show_page("help")
        return self.page_help

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
        # 🔴 `window_geometry` 认的是**设计像素** —— 里面那些 1000 / 1320 / 560 /
        #    0.68 全是按 96 DPI 标定的，而这里量到的是**物理像素**（高 DPI 感知
        #    打开之后 150% 的屏上两者差 1.5 倍）。所以先把入参折回设计像素去算，
        #    拿回来的结果再换回物理像素给 Tk。这样标定值和 `geometry_test.py`
        #    的断言一个字都不用改。
        w, h, x, y = window_geometry(sw / UI_SCALE, sh / UI_SCALE, reqh / UI_SCALE)
        w, h, x, y = dp(w), dp(h), dp(x), dp(y)
        # 内容装不下才挂滚动条。一次性决定，不做动态显隐 —— 动态 pack/pack_forget
        # 会改变 canvas 宽度、又触发 <Configure>，容易自己把自己绕进去。
        if reqh > h:
            self.vsb.pack(side="right", fill="y", before=self.canvas)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        ms = minsize_for(sw / UI_SCALE, sh / UI_SCALE)   # 同上：认设计像素
        self.root.minsize(dp(ms[0]), dp(ms[1]))
        # 窗口圆角（Win11）的**兜底声明**。⚠️ 实测过：本机 Win11 上窗口本来
        # 就是圆的，这里不会带来视觉变化，它的作用是「防止哪天被系统策略或
        # 第三方美化工具改回直角」。详见 `enable_round_corners` 的说明。
        # 放在 geometry 之后：窗口得先有真实尺寸，DWM 才认得这个偏好。
        self.round_corners = enable_round_corners(self.root)
        if self.smoke:
            self.root.update_idletasks()
            # 🔴 自检里所有模态弹窗都要换成**自动返回**的假框（2026-10-11）。
            #    下面 update_wiring_check 会真发一次点击、走完整条链；而「已是最新版本」
            #    那条路现在会弹 info 框 —— 模态框会**一直等用户点**，无人值守的
            #    --guitest 就会卡死在那儿直到超时。报出来是「自检没通过」，
            #    真实原因却是「没人点那个框」，差得十万八千里。
            real_mb = self.messagebox
            self.messagebox = _SilentBox()
            try:
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
            finally:
                self.messagebox = real_mb
            # 顺便把「使用说明」那一页建一次，确认它能起来。
            # ⚠️ 说明页是**懒建**的（点导航栏才建），自检不主动点一遍就永远
            #    测不到 —— 那种情况下一个 NameError 能一路带进发布版，
            #    用户点一下才发现是空白页。
            # 🔴 **但「账号中心」那一页故意不在自检里建**：
            #    它①要读用户真实账号密码（自检不该依赖这个）、
            #    ②会起一个后台线程 + 一个 `after()` 轮询。
            #    在无人的 --guitest 里起线程是自找麻烦。
            #    它的创建路径由 `selfwin_test.py` 的嵌入模式那一段覆盖。
            try:
                self.show_page("help")
                self.page_help.update_idletasks()
                self.show_page("settings")
            except Exception:
                log("说明页自检失败:\n%s" % traceback.format_exc())
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


def _fix_std_streams():
    """把 stdout/stderr 收拾成「print 任何东西都不会抛异常」的状态。

    要兜住三种形态（2026-10-16 补全，起因见下）：

      1. **None** —— `--windowed` 打包且没有重定向（双击运行就是这样）。
         `print` 会 `AttributeError: 'NoneType' has no attribute 'write'`。
         ⇒ 接一个 devnull。
      2. **被重定向的文本流**（文件 / NUL / 管道）—— 此时 `sys.stdout` 不是 None，
         编码是 **locale**（中文 Windows 上是 cp936）。输出里只要有它表示不了的
         字符（`⚠️`、`✅` 这类），`print` 就抛 `UnicodeEncodeError`。
         ⇒ 只把 errors 策略改成 `replace`。
      3. **正常终端** —— 也统一改 errors，代价只是「个别字符显示成 ?」。

    🔴 **为什么必须修，而不是「反正用户看不到就算了」**：
       `--windowed` 的 exe 遇到未捕获异常会弹一个**等人点 Close** 的错误框。
       调用方是脚本时（`build.py` 的冒烟测试把输出接到 DEVNULL），那个框没人点
       ⇒ 进程一直挂着，一路挂到调用方超时。而 build.py 报出来的是
       「--selftest 超时 45s / 界面可能有问题」—— **完全指不到真原因**。
       2026-10-16 实测踩到：0.6.0 的 `--selftest` 新增了一行带 `⚠️` 的
       「密码加密」，于是**每次构建都必然超时**（0.5.6 时没有非 GBK 字符，
       所以从没暴露过）。

    ⚠️ 只改 `errors`、**不改 `encoding`**：把编码改成 utf-8 之后，控制台仍按
       cp936 解释那些字节 ⇒ 中文全变乱码。改 errors 的代价只是个别字符显示成 `?`。

    返回 `(是否原本有 stdout, 是否成功改过 errors)` —— 调用方要用第一个值分辨
    「双击（没有终端）」和「从终端启动」，见 main() 里 `had_stdout` 的说明。
    """
    had = sys.stdout is not None
    patched = False
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8",
                                    errors="replace"))
            continue
        try:
            s.reconfigure(errors="replace")
            patched = True
        except Exception:
            # 不是 TextIOWrapper（被测试替身换过、或 Python 太老没 reconfigure）
            # —— 不致命，跳过就是。
            pass
    return had, patched


def main():
    # --windowed 打包后 stdout/stderr 是 None，某些库会因此报错。
    # ⚠️ 先把「本来有没有 stdout」记下来，**再**做兜底 ——
    #    兜底之后 sys.stdout 永远不是 None，就再也分辨不出「双击（没有终端）」
    #    和「从终端里启动」了。`--help` 要靠这个决定打印还是弹窗（见那边的注释）。
    #    具体怎么兜、为什么要兜这么多，见 `_fix_std_streams` 的说明。
    had_stdout = _fix_std_streams()[0]

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

    # 老版本存下的**明文**密码主动升级成密文（2026-10-15 加）。
    # ⚠️ 必须在 migrate_legacy_data() **之后** —— 继承过来的那份也可能是明文的，
    #    顺序反了就得等下次开机才升（甚至永远不升，如果用户再也不动设置）。
    #
    # 🔴 为什么**自检入口要跳过**：见 `should_upgrade_secrets()` 的说明。
    if should_upgrade_secrets(args):
        try:
            n_up = upgrade_secrets_to_encrypted()
            if n_up:
                log("已把 %d 处明文密码升级为加密存储" % n_up)
        except Exception:
            # 升级失败不能挡启动 —— 明文照样能用，只是少一层保护。
            log("明文密码升级失败（不影响使用）:\n%s" % traceback.format_exc())

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
            # 密码在磁盘上是什么状态（2026-10-15 加）。只报「有没有 enc:v1: 前缀」，
            # **不解密** —— 这份输出会被发出去。排查「开机自动登录说密码是空的」
            # 时第一手就看这行：多半是换了 Windows 用户 / 换了机器，DPAPI 解不开。
            "密码加密: %s" % secret_storage_text(),
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
            # 多源下载能不能解析出两个源（0.4.4）。**真跑一遍** evaluate →
            # candidate_urls，不是打印写死的说明 —— 写死的那句在函数被删掉
            # 之后照样打印，等于没有这个检查。见 download_source_desc()。
            "下载源: %s" % download_source_desc(),
            # 远程附加源（0.4.5）能不能被正确合并、主源有没有被顶掉。
            # 同样**真跑一遍** merge_sources，不是打印写死的说明。见 mirror_merge_desc()。
            "附加源: %s" % mirror_merge_desc(),
            # 增量更新（0.5.0）的两件事：补丁工具在不在、挑补丁的逻辑对不对。
            "补丁工具: %s" % patch_tool_desc(),
            "补丁挑选: %s" % patch_plan_desc(),
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
            update_config({"userId": uid, "passwd": pwd})
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
