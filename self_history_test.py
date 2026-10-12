# -*- coding: utf-8 -*-
"""`self_history.py`（本地流量历史）的测试。**不联网、不碰真实数据。**

钉的是**边界**，不是「能不能存下来」：

  1. 🔴 **合并规则**（用户 2026-10-12 明确要求）：同一天数据不同 ⇒ 远端覆盖本地；
     本地没有的日期 ⇒ 追加；完全相同 ⇒ 不动（也不报「更新了」）。
  2. 🔴 **账号之间必须隔离** —— 库里是两个账号的数据，A 的绝不能出现在 B 下面。
     这条错了的后果很实在：看 A 的流量、其实是 B 的，而且**界面上看不出任何异常**。
  3. 🔴 **坏文件不销毁。** 读不出来的文件要改名成 `.bad` 留着（人可能想看一眼），
     而不是被下次同步直接覆盖掉。
  4. **坏数据不崩。** 单条脏记录只丢它自己，不影响别的；整个文件坏了也给空库。
  5. **上下行是「知道的那几天」的和，并且要能说出算了几天的。**
     因为**今天的上下行服务端不给**（实测），而今天几乎总在明细里 ——
     写成「一天不知道就整条给 None」的话，界面上会**永远显示「—」**；
     写成「把 None 当 0 加进去」的话，会**编出一个偏小的数**。
     两个都错。正确做法是给部分和 + 报出天数（口径写在界面上）。
     ⚠️ 但「一天都不知道」仍然是 `None` —— 「没有信息」≠「算了 0 天得到 0」。

用法：
    python self_history_test.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import self_history as H        # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-56s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    ok = bool(cond)
    print("  [%s] %-56s %s" % ("OK" if ok else "失败", name, detail))
    if not ok:
        FAILS.append(name)


def day(date, flow, up=None, down=None, hours=None):
    return {"date": date, "flow": flow, "up": up, "down": down, "hours": hours}


# ---------------------------------------------------------------- ① 读写

def test_io(tmp):
    print("\n① 读 / 写")
    p = os.path.join(tmp, "flow.json")

    store, why = H.load(p)
    check("文件不存在 → 空库", store, H.empty_store())
    check("文件不存在 → 没有原因（不算出错）", why, "")

    store = H.empty_store()
    H.merge(store, "2026000001", [day("2026-10-01", 100.0, 20.0, 80.0, 3.0)])
    ok, why = H.save(p, store)
    check_true("写成功", ok, why)
    check_true("写完之后没有留下 .tmp", not os.path.exists(p + ".tmp"), "")

    back, why = H.load(p)
    check("读回来和写进去的一样", back, store)

    # 往返之后查询接口也对
    check("days_of 读得到", H.days_of(back, "2026000001"),
          [day("2026-10-01", 100.0, 20.0, 80.0, 3.0)])
    check("库里的账号列表", H.accounts_in(back), ["2026000001"])


def test_save_failure(tmp):
    print("\n② 写不进去时要「报错」而不是「抛异常」，而且不能毁掉原文件")
    p = os.path.join(tmp, "flow.json")
    store = H.empty_store()
    H.merge(store, "A", [day("2026-10-01", 1.0)])
    H.save(p, store)
    before = open(p, "rb").read()

    # 把目标做成一个目录 → 写必然失败
    bad = os.path.join(tmp, "as_dir.json")
    os.makedirs(bad, exist_ok=True)
    ok, why = H.save(bad, store)
    check("写失败时 ok=False", ok, False)
    check_true("写失败时给了原因", bool(why), why)

    check_true("原来那个好文件一个字节都没变", open(p, "rb").read() == before, "")


# ---------------------------------------------------------------- ③ 坏文件

def test_corrupt(tmp):
    print("\n③ 坏文件：不崩、不销毁、改名成 .bad 留着")
    p = os.path.join(tmp, "corrupt.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{这不是 JSON")
    store, why = H.load(p)
    check("坏 JSON → 空库", store, H.empty_store())
    check_true("坏 JSON → 有原因", bool(why), why)
    check_true("原文件被改名成 .bad 留着（没删）", os.path.exists(p + ".bad"), "")
    check_true("原位置已经没有那个坏文件了", not os.path.exists(p), "")

    # 已经有 .bad 时不再覆盖它
    p2 = os.path.join(tmp, "corrupt2.json")
    with open(p2 + ".bad", "w", encoding="utf-8") as f:
        f.write("旧的 bad 备份")
    with open(p2, "w", encoding="utf-8") as f:
        f.write("又坏了")
    H.load(p2)
    check("已有的 .bad 不被覆盖", open(p2 + ".bad", encoding="utf-8").read(),
          "旧的 bad 备份")

    # 结构不认识
    p3 = os.path.join(tmp, "list.json")
    with open(p3, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    store, why = H.load(p3)
    check("顶层是 list → 空库", store, H.empty_store())
    check_true("顶层是 list → 有原因", bool(why), why)

    # 版本不认识 ⇒ 空库，但**不改名**（那是版本差异，不是坏文件）
    p4 = os.path.join(tmp, "newver.json")
    with open(p4, "w", encoding="utf-8") as f:
        json.dump({"version": 99, "accounts": {"A": {}}}, f)
    store, why = H.load(p4)
    check("版本不认识 → 空库", store, H.empty_store())
    check_true("版本不认识 → 有原因", bool(why), why)
    check_true("版本不认识时**不动**原文件", os.path.exists(p4), "")


# ---------------------------------------------------------------- ④ 合并

def test_merge(tmp):
    print("\n④ 合并规则（用户明确要求的那三条）")
    store = H.empty_store()

    added, updated = H.merge(store, "A", [
        day("2026-10-01", 100.0, 20.0, 80.0, 3.0),
        day("2026-10-02", 200.0, 30.0, 170.0, 4.0),
    ])
    check("第一次：新增 2 天", (added, updated), (2, 0))

    # 同样的数据再来一次 ⇒ 什么都不做
    added, updated = H.merge(store, "A", [
        day("2026-10-01", 100.0, 20.0, 80.0, 3.0),
        day("2026-10-02", 200.0, 30.0, 170.0, 4.0),
    ])
    check("完全相同 ⇒ 不新增也不更新", (added, updated), (0, 0))

    # 同一天、值变了 ⇒ 远端覆盖
    added, updated = H.merge(store, "A", [day("2026-10-01", 111.0, 22.0, 89.0, 3.5)])
    check("同一天值变了 ⇒ 算「更新 1 天」", (added, updated), (0, 1))
    check("覆盖后的值就是远端的", H.days_of(store, "A")[0],
          day("2026-10-01", 111.0, 22.0, 89.0, 3.5))

    # 新日期 ⇒ 追加
    added, updated = H.merge(store, "A", [day("2026-10-03", 300.0, 1.0, 299.0, 5.0)])
    check("新日期 ⇒ 算「新增 1 天」", (added, updated), (1, 0))
    check("按日期升序排好", [d["date"] for d in H.days_of(store, "A")],
          ["2026-10-01", "2026-10-02", "2026-10-03"])

    # 今天那条（up/down 是 None）
    added, updated = H.merge(store, "A", [], today=day("2026-10-12", 50.0))
    check("今天也能合进去", (added, updated), (1, 0))
    check("今天的上下行是 None（不是 0）", H.days_of(store, "A")[-1],
          day("2026-10-12", 50.0, None, None, None))

    # 第二天同步时，今天变成「过去的一天」，带着真的上下行回来 ⇒ 算更新
    added, updated = H.merge(store, "A", [day("2026-10-12", 88.0, 11.0, 77.0, 6.0)])
    check("昨天的 None 被真的上下行补上 ⇒ 算「更新」", (added, updated), (0, 1))
    check("补上之后的值", H.days_of(store, "A")[-1],
          day("2026-10-12", 88.0, 11.0, 77.0, 6.0))


def test_merge_isolation(tmp):
    print("\n⑤ 账号之间必须隔离")
    store = H.empty_store()
    H.merge(store, "A", [day("2026-10-01", 100.0)])
    H.merge(store, "B", [day("2026-10-01", 999.0)])

    check("A 的数据是 A 的", H.days_of(store, "A"), [day("2026-10-01", 100.0)])
    check("B 的数据是 B 的", H.days_of(store, "B"), [day("2026-10-01", 999.0)])
    check("库里有两个账号", H.accounts_in(store), ["A", "B"])
    check("没同步过的账号 → 空列表", H.days_of(store, "C"), [])

    H.clear(store, "A")
    check("清掉 A 之后 A 是空的", H.days_of(store, "A"), [])
    check("清掉 A 不影响 B", H.days_of(store, "B"), [day("2026-10-01", 999.0)])
    check("清全部", H.clear(store), 1)
    check("清完全部就空了", H.accounts_in(store), [])


def test_merge_dirty(tmp):
    print("\n⑥ 脏数据：只丢坏的那条，不影响别的")
    store = H.empty_store()
    added, updated = H.merge(store, "A", [
        day("2026-10-01", 100.0),
        {"date": "", "flow": 5.0},              # 日期空 → 丢
        {"date": "2026-10-02", "flow": "abc"},  # 合计不是数 → 丢
        {"date": "2026-10-03"},                 # 连 flow 都没有 → 丢
        "这不是 dict",                           # → 丢
        {"date": "2026-10-04", "flow": 7.0, "up": "x", "down": True,
         "hours": 2.0, "恶意多余字段": 1},
    ])
    check("只留下 2 条好的", added, 2)
    check("好的那两条", [d["date"] for d in H.days_of(store, "A")],
          ["2026-10-01", "2026-10-04"])
    last = H.days_of(store, "A")[-1]
    check("读不动的 up 变成 None（不是 0）", last["up"], None)
    check("布尔值不算流量数字 → None", last["down"], None)
    check("正常字段留着", (last["flow"], last["hours"]), (7.0, 2.0))
    check_true("多余字段被丢掉（白名单）", "恶意多余字段" not in last, "")


def test_prune(tmp):
    print("\n⑦ 上限：超过 MAX_DAYS 丢最老的")
    store = H.empty_store()
    days = []
    for i in range(H.MAX_DAYS + 5):
        days.append(day("2020-%02d-%02d" % (1 + i // 28, 1 + i % 28), float(i)))
    H.merge(store, "A", days)
    got = H.days_of(store, "A")
    check("条数被压到上限", len(got), H.MAX_DAYS)
    check_true("丢掉的是最老的（最早那天不在了）", got[0]["date"] != "2020-01-01", "")


# ---------------------------------------------------------------- ⑧ 汇总

def test_summarize():
    print("\n⑧ 汇总：上下行用「知道的那几天」的和，并把天数报出来")
    days = [day("2026-10-01", 100.0, 20.0, 80.0, 3.0),
            day("2026-10-02", 200.0, 30.0, 170.0, 4.0)]
    s = H.summarize(days)
    check("共计", s["flow"], 300.0)
    check("上行", s["up"], 50.0)
    check("下行", s["down"], 250.0)
    check("总时长", s["hours"], 7.0)
    check("天数", s["n"], 2)
    check("日均 = 共计 ÷ 有记录的天数", s["avg"], 150.0)
    check("全部都有值时天数就是总天数", (s["up_days"], s["down_days"]), (2, 2))

    # 🔴 今天那条 up/down 是 None（服务端不给，实测）。**不能因此就不显示上下行** ——
    #    那样界面上永远显示「—」，等于把用户要看的数藏起来。
    #    正确做法：给「知道的那几天」的和，并把天数一起报出来（口径写清）。
    days2 = days + [day("2026-10-12", 50.0)]
    s2 = H.summarize(days2)
    check("共计照样算（合计是知道的）", s2["flow"], 350.0)
    check("上行 = 知道的那两天之和（不是 None、也不是 50）", s2["up"], 50.0)
    check("并把算了几天的报出来（界面要写「2/3 天」）", s2["up_days"], 2)
    check("总天数", s2["n"], 3)
    check("日均按 3 天算", s2["avg"], 350.0 / 3)
    # ⚠️ 但「一天都不知道」仍然是 None —— 「没有信息」≠「算了 0 天得到 0」
    only_today = H.summarize([day("2026-10-12", 50.0)])
    check("一天都不知道 ⇒ 上行给 None（不是 0）", only_today["up"], None)
    check("并报出 0 天", only_today["up_days"], 0)
    check_true("None ≠ 0（这条错了界面会显示一个编出来的 0）",
               only_today["up"] != 0.0, "")

    check("空列表不崩", H.summarize([]),
          {"flow": 0.0, "up": None, "down": None, "hours": None, "n": 0,
           "up_days": 0, "down_days": 0, "hours_days": 0, "avg": 0.0})
    check("None 也不崩", H.summarize(None)["n"], 0)


def test_group_by_month():
    print("\n⑨ 按月聚合")
    days = [day("2026-09-29", 10.0, 1.0, 9.0, 1.0),
            day("2026-09-30", 20.0, 2.0, 18.0, 2.0),
            day("2026-10-01", 30.0, 3.0, 27.0, 3.0)]
    g = H.group_by_month(days)
    check("分成了 2 个月", [x["month"] for x in g], ["2026-09", "2026-10"])
    check("9 月合计", (g[0]["flow"], g[0]["n"]), (30.0, 2))
    check("10 月合计", (g[1]["flow"], g[1]["n"]), (30.0, 1))
    check("9 月上行", g[0]["up"], 3.0)

    g2 = H.group_by_month([day("2026-10-01", 30.0, 3.0, 27.0, 3.0),
                           day("2026-10-12", 5.0)])
    check("这个月有一天不知道上行 ⇒ 上行只算知道的那天", g2[0]["up"], 3.0)
    check("并报出算了几天的", g2[0]["up_days"], 1)
    check("但合计照样是准的", g2[0]["flow"], 35.0)


def main():
    print("=" * 78)
    print("self_history.py 自检（不联网、不碰真实数据）")
    print("=" * 78)
    tmp = os.path.join(tempfile.gettempdir(), "self_history_test")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        test_io(tmp)
        test_save_failure(tmp)
        test_corrupt(tmp)
        test_merge(tmp)
        test_merge_isolation(tmp)
        test_merge_dirty(tmp)
        test_prune(tmp)
        test_summarize()
        test_group_by_month()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("=" * 78)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
