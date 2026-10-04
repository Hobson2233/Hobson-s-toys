# -*- coding: utf-8 -*-
"""端到端验证「按钮配色」这条门槛：真跑 --guitest，确认隐形按钮能拦住构建。

和 ui_contrast_test.py 的分工：
    · ui_contrast_test.py  —— 用假控件树证明**判定逻辑**会拦人（快、不碰 Tk）
    · 本文件              —— 证明**整条链路**是通的（慢、要建真窗口）

    链路：ui_contrast_check 抛 -> run_gui(smoke) 冒泡 -> --guitest 捕获并写
          GUI_FAIL + traceback 到 guitest.txt -> build.py 搜 HARD_FAIL_PHRASES
          -> return 1
    任何一环断了（异常被吞、只写 GUI_FAIL 不带标识串、两边标识漂移），
    门槛就形同虚设 —— 而它失效时**看不出任何异常**，构建照样绿。

⚠️ 必须走子进程：`--guitest` 分支结尾是 `exit_now()`（硬退），
   在同一个进程里调 `main()` 拿不到返回值，进程直接就没了。

⚠️ 补丁用 `-c` 注入，**不改源文件**（2026-10-04 改过一版）：
   原来是把 `bg="#dbe1eb"` 改成 BG、跑完再改回来。能用，但中途崩掉
   就会把隐形按钮留在仓库里 —— 而它**不报任何错**，下次构建照样 GUI_OK。
   注入式补丁随进程结束就没了，没有这个风险。

用法：
    python ui_contrast_gate_test.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C

SRC = os.path.join(HERE, "campus_login.py")
# 必须和 build.py 的判据同源，不在这里另写一份（见 wiring_gate_test.py 第 4b 节）。
import build as B  # noqa: E402

# 注入到子进程里的小补丁：把每个圆角按钮的静止底色改成它父容器的底色 ——
# 也就是「隐形」。就是本次真实踩到的那个 bug。
PATCH_CODE = """
import sys
sys.path.insert(0, %(repo)r)
import campus_login as C
_real = C.round_button
def _patched(*a, **k):
    if k.get("parent_bg"):
        k["bg"] = k["parent_bg"]
    return _real(*a, **k)
C.round_button = _patched
sys.argv = ["campus_login.py", "--guitest"]
sys.exit(C.main())
""" % {"repo": HERE}


def marker():
    p = os.path.join(C.data_dir(), "guitest.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "<读不到>"


def run(argv):
    # 不用 capture_output：onefile 的 bootloader 会留一个继承管道的子进程，
    # 管道写端不关就会挂死（build.py 顶部「坑 3」记过这件事）。这里虽然跑的是
    # 源码版、没那个问题，但保持同一个写法，免得以后换 exe 时踩回去。
    r = subprocess.run(argv, cwd=HERE, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=180)
    return r.returncode, marker()


def main():
    fails = []

    def ck(name, got, want):
        ok = got == want
        print("  [%s] %-44s -> %r%s"
              % ("OK" if ok else "失败", name, got, "" if ok else "   (期望 %r)" % (want,)))
        if not ok:
            fails.append(name)

    print("=== A. 源码原样：--guitest 应当 GUI_OK ===")
    code, text = run([sys.executable, SRC, "--guitest"])
    ck("退出码 0", code, 0)
    ck("标记是 GUI_OK", text.startswith("GUI_OK"), True)

    print()
    print("=== B. 注入「按钮隐形」补丁：必须 GUI_FAIL ===")
    code, text = run([sys.executable, "-c", PATCH_CODE])
    ck("退出码 1", code, 1)
    ck("标记是 GUI_FAIL", text.startswith("GUI_FAIL"), True)

    print()
    print("=== C. 失败文本里必须带着 build.py 认得的硬拦标识 ===")
    hit = [p for p in B.HARD_FAIL_PHRASES if p in text]
    ck("命中硬拦标识（build.py 搜的就是它）", bool(hit), True)
    print("       命中: %r" % (hit,))
    ck("原因点明是配色问题（不是别的凑巧失败）",
       ("几乎同色" in text) or ("看不出是个按钮" in text), True)

    print()
    print("=== D. 失败文本里必须点名是哪个按钮 ===")
    ck("点名了「使用说明」", "使用说明" in text, True)

    print()
    if fails:
        print("失败 %d 项: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("全部通过 —— 从「按钮隐形」到「构建判为失败」，整条链是通的")
    return 0


if __name__ == "__main__":
    sys.exit(main())
