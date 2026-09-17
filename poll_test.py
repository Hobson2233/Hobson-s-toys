# -*- coding: utf-8 -*-
"""界面轮询队列的行为测试 —— 不建 Tk，纯逻辑，秒级跑完。

背景（含一次**被测试纠正过的误判**，如实记下）：

    原来 poll() 长这样：

        try:
            while True:
                kind, payload = state["queue"].get_nowait()   # 靠 queue.Empty 退出
                ...
                elif kind == "job":
                    code, text = payload
                    set_busy(False)
                    load_all()
                    show(text, ...)
                    refresh_status()
        except Exception:
            pass

    我一开始的判断是「set_busy(False) 后面抛异常会让界面永久卡在忙碌」。
    **这个判断是错的** —— set_busy(False) 本来就是第一句，异常发生在它之后，
    它早就执行完了。第 6 条用例把这个误判当场证伪。

    真正站得住的两条缺陷是：

      (A) 异常被 `except Exception: pass` 静默吞掉，日志里一个字都没有。
          界面看起来"什么都没发生"，排查时完全无从下手。
      (B) `while True` 靠 queue.Empty 退出，所以**一条坏消息会中断整批**：
          同一轮里排在它后面的消息（比如网络状态更新）要等下一个 120ms
          周期才处理。不是丢消息，但"一次只推进一条"会让界面表现得很迟钝。

    另外，原来的 `set_busy(False)` 在前，只是**碰巧**让忙碌状态被解除了 ——
    这个保证依赖于"语句顺序"，谁哪天把 load_all() 挪到前面就会破。
    新写法用 try/finally 把保证写进语言结构里，不再依赖顺序。

用法: python poll_test.py        退出码 0 = 全过
"""
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C  # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %s %s" % ("[OK]" if ok else "[!!]", name))
    if not ok:
        print("       期望 %r，实际 %r" % (want, got))
        FAILED.append(name)
    return ok


def check_true(name, cond, detail=""):
    print("  %s %s%s" % ("[OK]" if cond else "[!!]", name,
                         ("  (%s)" % detail) if detail else ""))
    if not cond:
        FAILED.append(name)
    return cond


def new_logic(msgs, load_all, show, refresh, set_busy):
    """复刻**修复后**的写法：drain_queue + handle 内部 try/finally。"""
    q = queue.Queue()
    for m in msgs:
        q.put(m)

    def handle(msg):
        kind, payload = msg
        if kind == "job":
            code, text = payload
            try:
                load_all()
                show(text)
                refresh()
            finally:
                set_busy(False)

    return C.drain_queue(q, handle)


def old_logic(msgs, load_all, show, refresh, set_busy, logbox):
    """原样复刻**修复前**的写法（连 except Exception: pass 一起复刻）。

    logbox 是"如果旧写法也记日志，会记到这里"——用来验证它**没记**。
    """
    q = queue.Queue()
    for m in msgs:
        q.put(m)
    try:
        while True:
            kind, payload = q.get_nowait()
            if kind == "job":
                code, text = payload
                set_busy(False)
                load_all()
                show(text)
                refresh()
    except Exception as e:
        logbox.append(e)          # 旧写法这里是 pass，所以 logbox 会是空的


def main():
    real_log = C.log
    logs = []

    def fake_log(msg):
        logs.append(msg)

    C.log = fake_log
    try:
        print("1. 正常路径：3 条消息全部处理")
        busy = {"v": True}
        seen = []
        n_ok, n_err = new_logic(
            [("status", True), ("job", (0, "成功")), ("status", False)],
            load_all=lambda: seen.append("load"),
            show=lambda t: seen.append("show:" + t),
            refresh=lambda: seen.append("refresh"),
            set_busy=lambda b: busy.__setitem__("v", b))
        check("处理成功条数", n_ok, 3)
        check("出错条数", n_err, 0)
        check("忙碌已解除", busy["v"], False)
        check_true("刷新/展示都跑到了", "load" in seen and "refresh" in seen)

        print("\n2. 空队列：是正常结束，不该记日志、不该算错误")
        logs.clear()
        n_ok, n_err = new_logic([], lambda: None, lambda t: None,
                                lambda: None, lambda b: None)
        check("成功条数", n_ok, 0)
        check("出错条数（queue.Empty 不算错误）", n_err, 0)
        check("没有写日志", len(logs), 0)

        print("\n3. 刷新时抛异常：必须记进日志（缺陷 A）")
        busy = {"v": True}
        logs.clear()

        def boom():
            raise RuntimeError("模拟 load_all 里出错")

        n_ok, n_err = new_logic([("job", (0, "x"))], load_all=boom,
                                show=lambda t: None, refresh=lambda: None,
                                set_busy=lambda b: busy.__setitem__("v", b))
        check("出错条数", n_err, 1)
        check("忙碌已解除", busy["v"], False)
        check_true("★ 异常被记进日志（不是静默吞掉）",
                   any("界面刷新异常" in m for m in logs),
                   "日志 %d 条" % len(logs))

        print("\n4. 单条出错不影响同批其它消息（缺陷 B）")
        busy = {"v": True}
        logs.clear()
        done = []
        calls = {"n": 0}

        def sometimes_boom():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("第一条炸了")
            done.append("ok")

        n_ok, n_err = new_logic(
            [("job", (0, "a")), ("status", True), ("job", (0, "b"))],
            load_all=sometimes_boom, show=lambda t: None, refresh=lambda: None,
            set_busy=lambda b: busy.__setitem__("v", b))
        check("成功条数", n_ok, 2)
        check("出错条数", n_err, 1)
        check("★ 同批里后面的消息仍然处理", len(done), 1)

        print("\n5. ★ 反证：旧写法在同样场景下必须暴露缺陷 A 和 B")
        busy = {"v": True}
        old_logs = []
        old_done = []
        old_calls = {"n": 0}

        def old_boom():
            old_calls["n"] += 1
            if old_calls["n"] == 1:
                raise RuntimeError("第一条炸了")
            old_done.append("ok")

        old_logic([("job", (0, "a")), ("job", (0, "b"))],
                  load_all=old_boom, show=lambda t: None, refresh=lambda: None,
                  set_busy=lambda b: busy.__setitem__("v", b), logbox=old_logs)
        check_true("缺陷 A：旧写法静默吞掉异常（异常只进 logbox，无任何日志）",
                   len(old_logs) == 1,
                   "旧写法吞掉 1 个异常且不记录")
        check("缺陷 B：旧写法中断整批，第 2 条消息没被处理", len(old_done), 0)
        print("       ↑ 这两条「通过」是好事：说明本测试真能抓到旧写法的毛病。")

        print("\n6. 误判证伪：旧写法其实**会**解除忙碌状态")
        busy2 = {"v": True}
        old_logic([("job", (0, "x"))], load_all=boom, show=lambda t: None,
                  refresh=lambda: None,
                  set_busy=lambda b: busy2.__setitem__("v", b), logbox=[])
        check("旧写法也解除了忙碌（set_busy(False) 本来就是第一句）",
              busy2["v"], False)

        print("\n7. drain_queue 的错误只来自真实异常，不是 queue.Empty")
        logs.clear()
        n_ok, n_err = C.drain_queue(queue.Queue(), lambda m: None)
        check("空队列返回 (0, 0)", (n_ok, n_err), (0, 0))
        check("空队列不写日志", len(logs), 0)

        print("\n8. 非 job 消息出错也记日志，且不中断")
        logs.clear()
        q = queue.Queue()
        q.put(("weird", 1))
        q.put(("status", True))
        got = []
        n_ok, n_err = C.drain_queue(
            q, lambda m: (got.append(m[0]) if m[0] == "status" else 1 / 0))
        check("出错条数", n_err, 1)
        check("后面的消息仍然处理", got, ["status"])
        check_true("日志里有 ZeroDivisionError 的痕迹",
                   any("ZeroDivisionError" in m for m in logs))
    finally:
        C.log = real_log

    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), "；".join(FAILED)))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
