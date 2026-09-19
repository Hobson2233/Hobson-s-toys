# -*- coding: utf-8 -*-
"""把每一次 Action=auto 运行的耗时拆开看，定位「开机自启慢」到底慢在哪一段。

为什么要专门做这个：日志里只有「开始检查」和「校园网就绪」两行，
但两者之间可能是「等网卡接入」（wait_for_campus 轮询）也可能是
「超时重试」——不拆开就分不清该改哪个参数。
"""
import os
import re
import sys
from datetime import datetime

DD = r"C:\ProgramData\CampusLogin"

TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})  (.*)$")


def parse(path):
    """返回 [(ts, line), ...]"""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                m = TS.match(raw.rstrip("\n"))
                if m:
                    try:
                        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    out.append((t, m.group(2)))
    except OSError:
        pass
    return out


def main():
    files = []
    for name in sorted(os.listdir(DD)):
        if name.startswith("campus-login.log"):
            files.append(os.path.join(DD, name))
    events = []
    for p in files:
        ev = parse(p)
        print("%-30s %5d 行事件" % (os.path.basename(p), len(ev)))
        events.extend(ev)
    events.sort(key=lambda x: x[0])
    print("合计 %d 条" % len(events))
    print()

    # 切成一段段「运行」：以「开始检查」为界
    runs = []
    cur = None
    for t, line in events:
        if line.startswith("=== 开始检查"):
            if cur:
                runs.append(cur)
            cur = {"start": t, "head": line, "lines": []}
        elif cur is not None:
            if line.startswith("=== 开始检查"):
                pass
            cur["lines"].append((t, line))
    if cur:
        runs.append(cur)

    autos = [r for r in runs if "Action=auto" in r["head"]]
    print("Action=auto 运行 %d 次" % len(autos))
    print()

    print("%-19s %8s %8s %8s %8s %8s  %s" % (
        "开始时间", "等网卡", "联网检", "GET", "POST", "总耗时", "结果"))
    print("-" * 92)
    for r in autos:
        t0 = r["start"]
        # ⚠️ 必须把起点也放进 marks —— d() 是按 key 取的，
        #    只传 "start" 会取不到（marks 里没有这个键），静默返回 None。
        marks = {"start": t0}
        for t, line in r["lines"]:
            if line == "校园网就绪" and "ready" not in marks:
                marks["ready"] = t
            if line.startswith("联网检测") and "net" not in marks:
                marks["net"] = t
            if line.startswith("GET 最终地址") and "get" not in marks:
                marks["get"] = t
            if line.startswith("POST 状态") or line.startswith("POST 异常"):
                marks.setdefault("post", t)
            if line.startswith("===") and any(
                    k in line for k in ("认证成功", "认证后仍不通")):
                marks["end"] = t
            if "无法判定" in line or "已连通" in line:
                marks.setdefault("end", t)

        def d(a, b):
            if a in marks and b in marks:
                return int((marks[b] - marks[a]).total_seconds())
            return None

        def fmt(v):
            return "%7s" % ("-" if v is None else v)

        wait = d("start", "ready")
        net = d("ready", "net")
        get = d("net", "get")
        post = d("get", "post")
        total = d("start", "end")
        verdict = "?"
        for _t, line in r["lines"]:
            if "认证成功" in line:
                verdict = "成功"
                break
            if "认证后仍不通" in line or "登录失败" in line:
                verdict = "失败"
        if verdict == "?" and any("无法判定" in l for _t, l in r["lines"]):
            verdict = "未收尾"
        print("%-19s %s %s %s %s %s  %s" % (
            t0.strftime("%m-%d %H:%M:%S"), fmt(wait), fmt(net), fmt(get),
            fmt(post), fmt(total), verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
