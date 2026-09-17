# -*- coding: utf-8 -*-
"""验证「密码框接线自检」这条构建门槛真的会拦人，且不依赖 Tk。

背景：
    `pwd_wiring_check()` 能在真界面里发现「get() 接错成显示文本」这类 bug。
    但异常要变成「构建失败」还得经过一整条链路：
        pwd_wiring_check 抛 → run_gui(smoke) 冒泡 → --guitest 捕获并写
        GUI_FAIL + traceback 到 guitest.txt → build.py 在文本里搜
        WIRING_FAIL_PHRASE → return 1
    链路上任何一环断了（异常被吞、marker 只写 GUI_FAIL 不写原因、
    两边的标识串漂移），门槛就形同虚设。这里把整条链路走一遍。

为什么不建真窗口：
    受控会话里建 Tk 窗口时好时坏（同一份代码实测 0.9s / 14s / 直接挂住）。
    而这段「哪个方法该给什么」的判定是门槛的核心，必须可靠 —— 所以用假对象
    （duck typing）替掉密码框，只复刻 --guitest 的错误处理分支。
    真界面里那一次由 `--guitest` 跑（会往日志写「密码框接线自检通过」）。

用法：
    python wiring_gate_test.py
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C

FAILS = []


class _Entry(object):
    """密码框内部那个输入控件：只提供「显示出来的是什么」。"""

    def __init__(self, field):
        self._f = field

    def get(self):
        return self._f.shown()


class FakeField(object):
    """够用的假密码框：pwd_wiring_check 只用到这几个方法。

    broken 用来模拟两种真实的接线错误：
      "get_returns_shown" —— form() 接到了显示文本上（最危险，症状和密码填错一样）
      "no_mask"           —— 忘了打码（text() 直接给真密码）
    """

    def __init__(self, broken=None):
        self.real = ""
        self.reveal = False
        self.focused = False
        self.broken = broken
        self.entry = _Entry(self)

    def shown(self):
        if self.broken == "no_mask":
            return self.real
        if self.reveal:
            return self.real
        if not self.real:
            return ""
        if self.focused:
            return "*" * (len(self.real) - 1) + self.real[-1]
        return "*" * len(self.real)

    def get(self):
        if self.broken == "get_returns_shown":
            return self.shown()
        return self.real

    def set(self, v):
        self.real = v

    def set_focus(self, on):
        self.focused = on

    def toggle_reveal(self):
        self.reveal = not self.reveal


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-48s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def guitest_result(run_gui):
    """复刻 campus_login.py 里 --guitest 的错误处理分支（不真的建窗口）。"""
    try:
        run_gui(smoke=True)
        return "GUI_OK", 0
    except Exception:
        return "GUI_FAIL\n%s" % traceback.format_exc(), 1


def raises_phrase(field):
    """跑自检，返回 (是否抛异常, 文本)。不抛时文本是那句成功描述。"""
    try:
        return False, C.pwd_wiring_check(field)
    except AssertionError as e:
        return True, str(e)


def main():
    print("=== 1. 好的密码框：自检应通过 ===")
    ok, msg = raises_phrase(FakeField())
    check("没有抛异常", ok, False)
    check("返回一句成功描述", "通过" in msg, True)

    print()
    print("=== 2. get() 接错成显示文本：自检必须报警 ===")
    bad, msg = raises_phrase(FakeField(broken="get_returns_shown"))
    check("抛了 AssertionError", bad, True)
    check("用的是统一的失败标识（build.py 就搜这句）",
          msg.startswith(C.WIRING_FAIL_PHRASE), True)
    check("说明了错在哪", "真实密码" in msg, True)

    print()
    print("=== 3. 忘了打码：自检必须报警 ===")
    bad, msg = raises_phrase(FakeField(broken="no_mask"))
    check("抛了 AssertionError", bad, True)
    check("用的是统一的失败标识", msg.startswith(C.WIRING_FAIL_PHRASE), True)
    check("说明了错在哪", "打码" in msg, True)

    print()
    print("=== 4. 标识串是单一来源，两边不许各写一份 ===")
    with open(os.path.join(HERE, "build.py"), "r", encoding="utf-8") as f:
        build_src = f.read()
    check("build.py 引用了常量而不是字面量",
          ("WIRING_FAIL_PHRASE" in build_src), True)
    check("build.py 里没有再硬编码那句字面量",
          ('"' + C.WIRING_FAIL_PHRASE + '"') in build_src, False)

    print()
    print("=== 5. --guitest 那条链路：异常要变成 GUI_FAIL + 原因 ===")
    def boom(smoke=False):
        C.pwd_wiring_check(FakeField(broken="get_returns_shown"))
    result, code = guitest_result(boom)
    check("退出码变成 1", code, 1)
    check("标记不再是 GUI_OK", result.startswith("GUI_FAIL"), True)
    check("文本里带着失败标识（门槛搜的就是它）",
          C.WIRING_FAIL_PHRASE in result, True)

    print()
    print("=== 6. 正常情况不会误伤 ===")
    result, code = guitest_result(lambda smoke=False: None)
    check("退出码 0", code, 0)
    check("标记是 GUI_OK", result, "GUI_OK")
    check("GUI_OK 里不含失败标识（否则每次构建都会误拦）",
          C.WIRING_FAIL_PHRASE in result, False)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 这条门槛确实会拦人，而且不依赖 Tk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
