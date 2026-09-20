# -*- coding: utf-8 -*-
"""守护（--guard）、自启参数、do_auto 重试的回归测试。

跑法：
    python guard_test.py            # 全部
    python guard_test.py --verbose  # 多打一些过程

设计原则（跟 network_state_test.py 一致）：
  · **每条断言都要能失败**。凡是「换个输入应该得到不同结果」的地方，
    都补一条阳性对照，证明这个断言不是恒真的。
  · **绝不碰用户真实数据**。accounts.json / guard.json / last-result.json
    全部重定向到临时目录，而且**写完要回读确认重定向真的生效了** ——
    否则可能一边"通过"一边把用户的账号文件写坏。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C

VERBOSE = "--verbose" in sys.argv
FAILS = []
CHECKS = [0]


def ck(name, got, want):
    CHECKS[0] += 1
    ok = got == want
    if not ok:
        FAILS.append("%s\n     得到: %r\n     期望: %r" % (name, got, want))
    if VERBOSE or not ok:
        print("  [%s] %s = %r" % ("OK " if ok else "BAD", name, got))
    return ok


def section(title):
    print("\n=== %s ===" % title)


# ---------------------------------------------------------------- 隔离环境
class Sandbox(object):
    """把模块里所有会落盘的路径重定向到临时目录。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="campus_guard_test_")
        self.saved = {}
        self.names = ("ACCOUNTS_FILE", "GUARD_FLAG", "RESULT_FILE", "CONFIG_FILE",
                      "LOG_FILE")

    def __enter__(self):
        for n in self.names:
            self.saved[n] = getattr(C, n)
            setattr(C, n, os.path.join(self.dir, n.lower()))
        # ⚠️ 回读确认重定向真的生效：写一次再检查文件落在临时目录里。
        #    不确认的话，一旦某个函数用的是缓存的旧路径，测试会一边"通过"
        #    一边把用户的真实账号文件写掉。
        C.write_json(C.ACCOUNTS_FILE, {"accounts": {}, "probe": True})
        real = os.path.isfile(C.ACCOUNTS_FILE) and C.ACCOUNTS_FILE.startswith(self.dir)
        if not real:
            raise RuntimeError("沙箱没生效！ACCOUNTS_FILE=%s" % C.ACCOUNTS_FILE)
        return self

    def __exit__(self, *a):
        for n, v in self.saved.items():
            setattr(C, n, v)
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def dead_pid():
    """找一个**确定不存在**的 pid。

    不能随便写个 999999 就假定它不存在 —— 先问一遍系统。
    （同理：不能假定「我构造的输入一定触发那条分支」。）
    """
    import psutil
    for cand in (999999, 999998, 888888, 777777):
        if not psutil.pid_exists(cand):
            return cand
    raise RuntimeError("找不到不存在的 pid，测试前提不成立")


# ---------------------------------------------------------------- A 参数读取
def test_lnk_arguments():
    section("A. 自启快捷方式的参数读取")
    tmp = tempfile.mkdtemp(prefix="campus_lnk_")
    try:
        # A1 本机**真实**的自启项。0.3.2 起自启优先走计划任务（见
        #    AUTOSTART_TASK_NAME 那段），.lnk 只是退路 —— 所以两条都看：
        #    有任务就看任务，没有才看 .lnk。这一条是「看一眼真机」的检查，
        #    不是回归用例；本机没开自启就跳过。
        st = C.task_state()
        if st["exists"]:
            ck("A1 真实计划任务读得出参数", bool(st["arguments"]), True)
            print("     计划任务参数 = %r" % st["arguments"])
        elif C.autostart_links():
            got = C._lnk_arguments(C.autostart_links()[0])
            # ⚠️ 只断言「读得出、且认得 --auto」，**不要写死完整值** ——
            #    这台机器上 .lnk 的参数会随版本升级而变（0.2.0 是 --auto，
            #    0.3.0 起是 --auto --guard）。钉死完整值的话，每次参数一变
            #    这条检查就假失败一次（2026-09-20 就是这么红的）。
            ck("A1 真实 .lnk 读得出参数且含 --auto",
               bool(got.startswith("--auto")), True)
            print("     .lnk 参数 = %r" % got)
        else:
            print("  [--] 本机没开自启，跳过 A1")

        # A2/A3 是**回归用例**：参数串在 .lnk 里跟后面的字符串之间没有可靠
        # 分隔符，紧跟的那一个字符是二进制残留（实测见过 `?` 和 `A`）。
        # 第一版按字符类截断，撞上 `A` 就粘成 `--guardA` —— 这两条就是钉住它的。
        # A3 只写 `--auto`（老参数），用来确认「旧参数」也能被正确读出。
        exe = os.path.join(tmp, "校园网自动登录.exe")
        open(exe, "wb").close()
        for tag, args in (("A2", C.AUTOSTART_ARGS), ("A3", "--auto")):
            p = os.path.join(tmp, "%s.lnk" % tag)
            C.make_lnk(p, exe, arguments=args, icon=exe, work_dir=tmp)
            got = C._lnk_arguments(p)
            ck("%s 写进去 %r，读回来一致" % (tag, args), got, args)
            if got != args:
                print("     ⚠️ 完整值: %r" % got)
        # A3b 白名单的阳性对照：认不出的开关必须返回空（不能「凡 --xxx 都算」）
        ck("A3b 未知开关认不出来", C._match_switch("--bogus"), "")
        ck("A3c 粘了残留字符仍能认出", C._match_switch("--guardA"), "--guard")
        ck("A3d 粘了引号仍能认出", C._match_switch('--auto"c:\\x'), "--auto")

        # A4 不是快捷方式 → 空串
        junk = os.path.join(tmp, "junk.bin")
        with open(junk, "wb") as f:
            f.write(b"this is not a lnk file at all")
        ck("A4 非 .lnk 文件读出空串", C._lnk_arguments(junk), "")

        # A5 不存在的文件 → 空串（不能抛异常）
        ck("A5 文件不存在读出空串", C._lnk_arguments(os.path.join(tmp, "nope.lnk")), "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_autostart_outdated():
    section("B. 「自启参数陈旧」的判定")
    tmp = tempfile.mkdtemp(prefix="campus_lnk_")
    saved = (C.is_frozen, C.autostart_links, C.app_path, C.task_state)
    try:
        exe = os.path.join(tmp, "校园网自动登录.exe")
        open(exe, "wb").close()
        p = os.path.join(tmp, "a.lnk")
        # 让 _lnk_points_to_me 认为「这个 .lnk 指向的就是我」
        C.is_frozen = lambda: True
        C.app_path = lambda: exe
        C.autostart_links = lambda: [p]
        # ⚠️ 必须把计划任务按成「不存在」：autostart_outdated() 现在会先问
        #    task_outdated()，而那读的是**这台机器上真实的任务**。
        #    不隔离的话，这几条用例的结果会随「本机开没开自启」而变 ——
        #    在自己机器上永远绿、在别人机器上红（或者反过来）。
        C.task_state = lambda: {"exists": False, "command": "",
                                "arguments": "", "logon": False}

        C.make_lnk(p, exe, arguments="--auto", icon=exe, work_dir=tmp)
        ck("B1 旧参数（--auto）→ 判定为陈旧", C.autostart_outdated(), True)
        # 阳性对照：同一套装置，只把参数换成新的，必须翻转
        C.make_lnk(p, exe, arguments=C.AUTOSTART_ARGS, icon=exe, work_dir=tmp)
        ck("B2 新参数（--auto --guard）→ 不陈旧", C.autostart_outdated(), False)
        # 阳性对照：目标指向别人时不算「陈旧」（那是 stale，另一码事）
        other = os.path.join(tmp, "别人的.exe")
        open(other, "wb").close()
        C.make_lnk(p, other, arguments="--auto", icon=other, work_dir=tmp)
        ck("B3 指向别的 exe → 不算陈旧", C.autostart_outdated(), False)
    finally:
        C.is_frozen, C.autostart_links, C.app_path, C.task_state = saved
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- C 守护判断
def test_guard_decide():
    section("C. guard_decide 判断表")
    S = C.GUARD_CONFIRM
    cases = [
        # (net, on_campus, streak, 期望, 说明)
        (True, True, 0, C.GUARD_IDLE, "在校园网且通着 → 不动"),
        (False, True, 0, C.GUARD_RELOGIN, "在校园网但被门户挡住 → 重连"),
        (None, True, 0, C.GUARD_WAIT, "测不出来第 1 次 → 再观察"),
        (None, True, S - 1, C.GUARD_WAIT, "测不出来还没到阈值 → 再观察"),
        (None, True, S, C.GUARD_RELOGIN, "连续测不出来到阈值 → 重连"),
        (False, False, 0, C.GUARD_IDLE, "不在校园网 → 不动（哪怕没认证）"),
        (True, False, 0, C.GUARD_IDLE, "不在校园网但能上网 → 不动"),
        (None, None, 0, C.GUARD_IDLE, "在不在校园网都判不出来 → 不动"),
        (False, None, S, C.GUARD_IDLE, "判不出在不在校园网 → 不动，不重连"),
    ]
    for net, on, streak, want, why in cases:
        ck("C net=%r on=%r streak=%d（%s）" % (net, on, streak, why),
           C.guard_decide(net, on, streak), want)

    # 阳性对照：这张表必须真的能产出三种不同的动作。
    # 只断言上面那几条的话，一个「永远返回 idle」的实现在大部分用例上也会通过。
    got = {C.guard_decide(n, o, s) for n, o, s in
           [(True, True, 0), (False, True, 0), (None, True, 0)]}
    ck("C10 三种输入产出三种不同动作（证明判断不是恒等）",
       sorted(got), sorted([C.GUARD_IDLE, C.GUARD_RELOGIN, C.GUARD_WAIT]))


# ---------------------------------------------------------------- D 守护标记
def test_guard_running():
    section("D. guard_running 的「自愈」")
    with Sandbox():
        # D1 没有标记文件
        ck("D1 没有标记文件 → 没在跑", C.guard_running(), (False, ""))
        if os.path.isfile(C.GUARD_FLAG):
            os.remove(C.GUARD_FLAG)

        # D2 新鲜标记 + 活着的 pid（就是本进程）
        C.write_json(C.GUARD_FLAG, {"pid": os.getpid(), "started": time.time()})
        got = C.guard_running()
        ck("D2 新鲜标记 + 活进程 → 在跑", got[0], True)
        # 阳性对照：上一条必须为 True，否则 D3 的「过期 → 没在跑」证明不了任何事
        #            （一个恒返回 False 的实现也能过 D3）。

        # D3 过期标记 + 活进程 → **必须**当作没在跑。
        #    退出走 TerminateProcess，finally 不执行，标记文件必然残留；
        #    这里判错的话守护就永远起不来了（而且是静默的）。
        C.write_json(C.GUARD_FLAG, {"pid": os.getpid(),
                                    "started": time.time() - (C.GUARD_SECONDS + 400)})
        got = C.guard_running()
        ck("D3 过期标记 → 没在跑（自愈）", got[0], False)

        # D4 新鲜标记 + 已死的 pid
        C.write_json(C.GUARD_FLAG, {"pid": dead_pid(), "started": time.time()})
        got = C.guard_running()
        ck("D4 新鲜标记 + 死进程 → 没在跑", got[0], False)

        # D5 时钟倒退（记录时刻在未来）→ 当作没在跑，别把守护永久锁死
        C.write_json(C.GUARD_FLAG, {"pid": os.getpid(),
                                    "started": time.time() + 99999})
        ck("D5 记录时刻在未来 → 没在跑", C.guard_running()[0], False)

        # D6 垃圾内容 → 没在跑，且不能抛异常
        with open(C.GUARD_FLAG, "w", encoding="utf-8") as f:
            f.write("not json at all")
        ck("D6 垃圾内容 → 没在跑", C.guard_running()[0], False)
        C.write_json(C.GUARD_FLAG, {"hello": "world"})
        ck("D7 缺 started 字段 → 没在跑", C.guard_running()[0], False)


# ---------------------------------------------------------------- E 主动退出避让
def test_logged_out_recently():
    section("E. 用户主动退出后的避让")
    with Sandbox():
        ck("E1 从没记过 → 不避让", C.logged_out_recently(), False)
        C.save_accounts({"accounts": {}, "lastOnline": "",
                         "lastLogout": C.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        ck("E2 刚刚退出过 → 避让", C.logged_out_recently(), True)
        # 阳性对照：E2 必须为 True，否则 E3 证明不了「时间窗口真的在起作用」。
        old = C.datetime.now() - timedelta(seconds=C.GUARD_RESPECT_LOGOUT + 600)
        C.save_accounts({"accounts": {}, "lastOnline": "",
                         "lastLogout": old.strftime("%Y-%m-%d %H:%M:%S")})
        ck("E3 退出很久了 → 不再避让", C.logged_out_recently(), False)
        C.save_accounts({"accounts": {}, "lastOnline": "", "lastLogout": "看不懂"})
        ck("E4 时间格式坏掉 → 不避让（不阻拦守护）", C.logged_out_recently(), False)

        # E5 note_logout 要真的写进 lastLogout（而不是只清 lastOnline）
        C.save_accounts({"accounts": {}, "lastOnline": "2026000001"})
        C.note_logout()
        store = C.load_accounts()
        ck("E5 note_logout 清空 lastOnline", store.get("lastOnline"), "")
        ck("E6 note_logout 记下时刻", bool(store.get("lastLogout")), True)


# ---------------------------------------------------------------- F do_auto 重试
class FakeTime(object):
    """替掉 campus_login 里的 time 模块，让 sleep 变成「只记账、不真睡」。

    ⚠️ 不能直接改全局的 time.sleep —— 那会把测试自己（和 tempfile 之类）
       一起改掉。这里只替换 campus_login 这个模块名字空间里的 time。
    """

    def __init__(self, real):
        self._real = real
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)

    def time(self):
        return self._real.time()

    def __getattr__(self, n):
        return getattr(self._real, n)


def test_do_auto_retry():
    section("F. do_auto 的重试（「开机自启慢」的修复点）")
    with Sandbox():
        saved = {}
        names = ("load_config", "wait_for_campus", "test_internet",
                 "portal_login", "check_password", "update_account_record", "time")
        for n in names:
            saved[n] = getattr(C, n)
        try:
            C.load_config = lambda: {"userId": "2026000001", "passwd": "x"}
            C.wait_for_campus = lambda s: True
            C.time = FakeTime(time)
            C.update_account_record = lambda u, p, ok: None

            # F1 第一次登录失败、第二次成功 → 必须重试并最终返回 0
            calls = {"n": 0}

            def flaky_login(uid, pwd):
                calls["n"] += 1
                return calls["n"] >= 2

            C.test_internet = lambda timeout=None: False
            C.portal_login = flaky_login
            C.check_password = lambda u, p: None
            code = C.do_auto()
            ck("F1 第一次失败第二次成功 → 退出码 0", code, 0)
            ck("F2 确实重试了（portal_login 调用次数 ≥ 2）", calls["n"] >= 2, True)
            ck("F3 重试次数正好 2", calls["n"], 2)

            # F4 阳性对照：把轮数压到 1，同一个场景必须**只**调一次。
            #     这一条证明 F2 对「重试轮数」是敏感的 —— 否则 F2 可能只是
            #     因为别的原因碰巧成立。
            calls["n"] = 0
            code1 = C.do_auto(attempts=1)
            ck("F4 attempts=1 时只调一次（阳性对照）", calls["n"], 1)
            ck("F5 attempts=1 时返回失败码 2", code1, 2)

            # F6 密码确定是错的 → 不该重试（别让用户在开机时干等）
            calls["n"] = 0
            C.portal_login = lambda u, p: (calls.__setitem__("n", calls["n"] + 1)
                                           or False)
            C.check_password = lambda u, p: False
            code = C.do_auto()
            ck("F6 密码错 → 返回 2", code, 2)
            ck("F7 密码错 → 只试 1 次，不重试", calls["n"], 1)

            # F8 一直等不到校园网 → 每轮都等，最后返回 4
            waited = {"n": 0}

            def no_campus(s):
                waited["n"] += 1
                return False

            C.wait_for_campus = no_campus
            C.check_password = lambda u, p: None
            code = C.do_auto()
            ck("F8 一直没校园网 → 返回 4", code, 4)
            ck("F9 每轮都尝试等待（3 轮）", waited["n"], C.AUTO_ATTEMPTS)

            # F10 已经能上网 → 直接成功，不该去登录
            calls["n"] = 0
            C.wait_for_campus = lambda s: True
            C.test_internet = lambda timeout=None: True
            code = C.do_auto()
            ck("F10 已联网 → 返回 0", code, 0)
            ck("F11 已联网 → 不去登录", calls["n"], 0)
        finally:
            for n, v in saved.items():
                setattr(C, n, v)


# ---------------------------------------------------------------- G 落盘文件
def test_no_real_data_touched():
    section("G. 确认没碰到真实数据")
    with Sandbox():
        C.save_accounts({"accounts": {"probe": {"passwd": "x"}}})
        # 真实路径应当是 C:\ProgramData\CampusLogin —— 沙箱里它不该被写到
        real_path = r"C:\ProgramData\CampusLogin\accounts.json"
        if os.path.isfile(real_path):
            with open(real_path, encoding="utf-8") as f:
                content = f.read()
            ck("G1 真实 accounts.json 里没有测试探针", "probe" in content, False)
        else:
            print("  [--] 真实 accounts.json 不存在，跳过 G1")
        ck("G2 沙箱目录里的文件确实写了", os.path.isfile(C.ACCOUNTS_FILE), True)


def main():
    # ⚠️ 缺依赖时必须**明确失败**，不能静默跳过那几条断言 ——
    #    "跳过" 和 "通过" 在输出里长得太像了，而 A2/A3 恰恰是这次新加的
    #    「参数读法」的核心用例。宁可让脚本红着脸退出。
    try:
        import pylnk3            # noqa: F401
    except ImportError:
        print("环境不完整：缺 pylnk3（用它写 .lnk 才能测参数读法）。")
        print("请用带 pylnk3 的解释器跑本脚本（项目里是 envs/gui314）。")
        return 3
    try:
        import psutil            # noqa: F401
    except ImportError:
        print("环境不完整：缺 psutil（守护的「进程还在不在」判定要用它）。")
        return 3

    print("campus_login.py 版本 %s" % C.VERSION)
    print("GUARD_SECONDS=%s GUARD_POLL=%s GUARD_CONFIRM=%s AUTO_ATTEMPTS=%s"
          % (C.GUARD_SECONDS, C.GUARD_POLL, C.GUARD_CONFIRM, C.AUTO_ATTEMPTS))
    test_lnk_arguments()
    test_autostart_outdated()
    test_guard_decide()
    test_guard_running()
    test_logged_out_recently()
    test_do_auto_retry()
    test_no_real_data_touched()

    print("\n" + "=" * 60)
    if FAILS:
        print("失败 %d / 共 %d 条：" % (len(FAILS), CHECKS[0]))
        for f in FAILS:
            print("  ✗ " + f)
        return 1
    print("全部通过：%d 条断言" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
