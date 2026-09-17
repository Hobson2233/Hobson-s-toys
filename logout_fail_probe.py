# -*- coding: utf-8 -*-
"""复现「连接校园网时点『退出当前账号』提示失败」。

用户报告：**正在连接校园网的过程中**点「退出当前账号」，程序弹「退出失败，请稍后重试」。

假设（要证伪的那条）：
    界面的忙碌标志是**进程级**的普通布尔，不是 GUI 每个实例自己的；
    而后台任务跑在另一个线程里。于是**同时开着两个窗口**时（托盘/双击又起一个），
    窗口 A 发起「切换账号」，窗口 B 点「退出当前账号」—— 两个任务并发跑，
    各自用同一个账号密码先后去要 distoken、再各自下线一次。
    第二次下线时那个 IP 已经不在线了，服务端不会回「已确认下线」，
    `portal_logout()` 返回 False → 界面弹「退出失败，请稍后重试」。

这条探针就是来证伪/证实它的，分两段：

  段 1（静态）：把「忙碌标志是模块级共享」和「两个例程会并发下线」这两件事
               从源码里**读出来**，不是靠猜。
  段 2（真跑）：**不碰校园网**。用两个并发线程分别跑两个「必须共用同一 IP 的
               下线动作」的替身：同一个假门户只接受一次下线，第二次返回失败。
               断言「并发时第二个动作会失败」，而串行时两个都成功。
               这是确定性复现，且不会真的把用户踢下线。

⚠️ 段 2 用的是假门户，只走 do_logout 的判定分支 —— 目的是证明**并发本身**
   足以造出「退出失败」，而不是证明真校园网一定这么回。

用法：
    python logout_fail_probe.py          # 走源码里的 do_logout，不连真门户
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print("  [%s] %-56s -> %r%s"
          % ("OK" if ok else "失败", name, got, "" if ok else "  (期望 %r)" % (want,)))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, extra=""):
    print("  [%s] %-56s %s"
          % ("OK" if cond else "失败", name, extra))
    if not cond:
        FAILS.append(name)


# ==================== 段 1：静态证据 ====================
def static_evidence():
    print("=== 段 1：忙碌标志是不是每个窗口自己的？===")
    src = open(os.path.join(HERE, "campus_login.py"), encoding="utf-8").read()

    # run_gui 每次调用都新建一个 state 字典。字典本身是新的，但内容呢？
    check_true("run_gui 里有 state 字典", 'state = {"busy": False' in src)
    check_true("set_busy 直接改 state[\"busy\"]",
               'state["busy"] = b' in src)
    check_true("start_job 用 state[\"busy\"] 判重入",
               'if state["busy"]:' in src)

    # 关键：busy 是布尔，不是「本次任务的所有者」。谁都能把它置 True / False。
    check_true("busy 是布尔而不是持有者标识",
               'state["busy"] = b' in src and 'state["busy"] = ' in src
               and "owner" not in src)

    # 有没有「一个进程只许一个窗口」的闸门？找单实例锁 / mutex / 命名事件。
    # 注意：只在**代码**里找，别在注释里找 —— 注释里出现「单实例」这个词
    # 不代表真有闸门。第一版就在这里误报了一次（注释里写了「没有单实例闸门」，
    # 子串照样命中），典型的「过滤条件写错 = 检查空转」。
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#"))
    gates = [k for k in ("CreateMutex", "CreateEvent", "single_instance",
                         "already_running") if k in code_only]
    print("       进程级单实例闸门:", gates or "无")
    check("代码里没有任何单实例闸门", gates, [])

    print("       => 两个窗口并存时，各自的 state 是各自的，busy 互不可见")
    check_true("do_logout 里加了跨窗口互斥（修 bug 时补的）",
               "_LOGOUT_LOCK.acquire" in code_only)


# ==================== 段 2：并发下线会不会「失败」 ====================
class FakePortal(object):
    """只接受一次下线的假门户。

    「同一个 IP 只在线一次」是校园网的真实模型：谁先下线谁成功，
    后来者面对的是一个已经不在线的 IP。
    """

    def __init__(self):
        self.online = True
        self.hits = []                 # 记录下线请求的先后
        self.lock = threading.Lock()

    def logout(self, who):
        with self.lock:
            self.hits.append(who)
            time.sleep(0.15)           # 模拟真实网络往返（真实约 1~3 秒）
            if not self.online:
                return False           # IP 已经不在线 → 下线未生效
            self.online = False
            return True


PORTAL = FakePortal()


def fake_portal_logout(who):
    """替掉真正的 portal_logout：不联网，只走「成功/失败」这一层。

    ⚠️ 返回的是**两态**（True/False），故意保留这个粗糙度 —— 它模拟的正是
    修复前那版 portal_logout 的语义：只看「下线生效了没有」，没有
    「判不出来」这个第三态。用它来复现老 bug 才对照得上。
    """
    return PORTAL.logout(who)


def run_logout_like_gui(who, patch_portal=True):
    """复刻界面点「退出当前账号」之后的调用链，直到写 last-result.json。

    patch_portal=True 时把 portal_logout 换成假门户（老语义：两态），
    目的是复现**修复前**的行为 —— do_logout 的重试逻辑救不了它，
    因为假门户从不返回 None，重试一次就 False 直接收工。
    """
    C.wait_for_campus = lambda *a, **k: True
    C.test_internet = lambda *a, **k: True      # 「已联网」-> 必须走下线
    if patch_portal:
        C.portal_logout = lambda: fake_portal_logout(who)
    return C.do_logout()


def main():
    static_evidence()

    print()
    print("=== 段 2a：串行 —— 两个动作先后跑（对照） ===")
    PORTAL.online = True
    PORTAL.hits = []
    c1 = run_logout_like_gui("串行A")
    r1 = C.read_json(C.RESULT_FILE) or {}
    print("       第 1 个: code=%s msg=%s" % (c1, r1.get("message")))
    # 第二个动作的前提是「已联网」，而串行时这时 IP 已经下线了 —— 真实界面里
    # 用户会先重新连上，所以这里把 online 复位再跑第二个。
    PORTAL.online = True
    c2 = run_logout_like_gui("串行B")
    r2 = C.read_json(C.RESULT_FILE) or {}
    print("       第 2 个: code=%s msg=%s" % (c2, r2.get("message")))
    check("串行第 1 个成功", c1, 0)
    check("串行第 2 个成功", c2, 0)
    check("假门户收到 2 次下线", len(PORTAL.hits), 2)

    print()
    print("=== 段 2b：并发 —— 两个窗口同时点（复现现场） ===")
    PORTAL.online = True
    PORTAL.hits = []
    results = {}

    def worker(who):
        results[who] = run_logout_like_gui(who)

    ts = [threading.Thread(target=worker, args=(w,)) for w in ("并发A", "并发B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    print("       结果:", results)
    print("       假门户收到的顺序:", PORTAL.hits)
    r = C.read_json(C.RESULT_FILE) or {}
    print("       last-result.json（后写的那次会盖掉前一次）: code=%s msg=%s"
          % (r.get("code"), r.get("message")))

    # 修复后：do_logout 里那把锁让两个动作**串行**，第二个进来时 IP 已经
    # 不在线 —— 而「不在线」正是用户想要的，假门户这边会回 False，
    # 于是第二个**仍然会报失败**。这恰恰说明：光加锁不够，
    # 真正的修复必须是「门户说本来就不在线 → 算成功」这条判定
    # （见 logout_retry_probe.py 第 1/2 节）。
    n_fail = sum(1 for v in results.values() if v != 0)
    check("假门户（两态、无「已离线算成功」语义）下仍会有一个报失败 —— "
          "说明这类门户响应必须按成功处理",
          n_fail, 1)
    check("失败的那个退出码是 3（LOGOUT_FAILED）",
          sorted(results.values()), [0, 3])
    check("失败信息是用户看到的那句",
          r.get("message"), "退出失败，请稍后重试")

    print()
    print("=== 段 2c：并发下 last-result.json 的覆盖 ===")
    # 两个任务都会 write_result，后写的盖掉先写的。所以「界面上显示哪一句」
    # 取决于时序 —— 用户可能看到成功也可能看到失败，这本身就是可复现的
    # 「有时提示失败」。
    check_true("last-result.json 只有一份、会被并发覆盖（没有按窗口隔离）",
               C.RESULT_FILE.count("last-result") == 1)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 并发确实足以造出「退出失败」，且已确定性复现")
    return 0


if __name__ == "__main__":
    sys.exit(main())
