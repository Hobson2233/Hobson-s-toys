# -*- coding: utf-8 -*-
"""查一件事：`log()` 在后台线程里会不会偶尔卡住几秒。

背景（2026-10-17）：`selfwin_test.py` ⑨ 偶尔红，诊断数据显示
「进了兜底分支（crashes=1）但消息没入队（puts 里没有 fail）」——
而这两步之间**只有一行 `log(...)`**。所以要么 `log()` 卡住了，
要么 `_put()` 抛了。

这个探针只测 `log()` 和 `traceback.format_exc()` 的耗时分布，不碰界面。

    python probe_log_slow.py [线程数]
"""
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import campus_login as C                                 # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 200


def _boom():
    raise RuntimeError("假装代码写错了")


def _one(durs):
    t0 = time.perf_counter()
    try:
        _boom()
    except Exception:
        text = traceback.format_exc()
    t1 = time.perf_counter()
    C.log("探针：这是一条测试日志\n%s" % text)
    t2 = time.perf_counter()
    durs.append(((t1 - t0) * 1000.0, (t2 - t1) * 1000.0))


def main():
    d = os.path.join(HERE, "out")
    if not os.path.isdir(d):
        os.makedirs(d)
    C.LOG_FILE = os.path.join(d, "probe_log.log")
    if os.path.exists(C.LOG_FILE):
        os.remove(C.LOG_FILE)

    durs = []
    lock = threading.Lock()

    def worker():
        local = []
        for _ in range(max(1, N // 20)):
            _one(local)
        with lock:
            durs.extend(local)

    ts = [threading.Thread(target=worker) for _ in range(20)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0

    fmt = [x[0] for x in durs]
    wri = [x[1] for x in durs]
    print("共 %d 次调用，墙钟 %.2f 秒" % (len(durs), wall))
    print("format_exc  最大 %7.2f ms  平均 %6.2f ms"
          % (max(fmt), sum(fmt) / len(fmt)))
    print("log() 写文件 最大 %7.2f ms  平均 %6.2f ms"
          % (max(wri), sum(wri) / len(wri)))
    over = [x for x in wri if x > 100]
    print("超过 100ms 的次数：%d（其中超过 1000ms 的 %d）"
          % (len(over), len([x for x in wri if x > 1000])))
    if over:
        print("那些偏慢的调用（ms）：%s" % [round(x, 1) for x in sorted(over)[-10:]])
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
