# -*- coding: utf-8 -*-
"""「上网数据」的**本地流量历史** —— 存本机、按账号分开、以后不登录也能看。

为什么要单独一个模块（2026-10-12 用户提的需求）：
    服务端只给**最近一段**的每日流量（实测是 30 天窗口 / 24 条记录），
    而且**今天不在里面**。所以「看过去每天的流量」这件事，只能靠本机
    **一次一次同步、把数据攒下来**。攒下来的东西必须有地方放。

🔴 为什么**不**放进 `self_service.py`：
    那个模块有一条被测试钉死的性质 —— **它不碰磁盘**（`self_service_test.py`
    里断言「没用到 os / pathlib」、没有 `open()`）。那条性质保护的是
    「这个模块不会把响应偷偷落盘」，而响应里**带身份证号和明文密码**。
    所以：**取数归取数，存盘归存盘**，两个模块各管一头。

🔴 这个模块的几条规矩：

  1. **纯逻辑 + 显式传路径。** 所有函数都接收 `path` / `store`，
     不读全局常量、不猜数据目录在哪。这样它能脱离 `campus_login.py` 单独测，
     也才能被 `datasafe` 沙箱正确重定向。
  2. **坏了不崩。** 文件不存在 / 不是 JSON / 结构不认识 ⇒ 返回空库，
     **不抛异常**。这是给人看的历史数据，为了一条脏记录让整个界面打不开
     是最差的处理方式。
  3. **坏文件不销毁。** 读到坏文件时把它改名成 `<path>.bad` 留着
     （人可能想自己看一眼），而不是下次同步时直接覆盖掉。
  4. **原子写。** 先写 `.tmp` 再 `os.replace` —— 中途断电/被杀不会留下
     半个文件（那会让下次 `load()` 直接判坏）。
  5. 🔴 **只存流量数字，不存凭据。** 库里除了「账号 + 每天几个数字」之外
     **什么都不许有**：没有密码、没有 cookie、没有接口原始返回。
     账号本身是个键（要按账号分开存），这是它能存的前提。
"""

import json
import os

# 存盘格式版本。以后要改结构就升它，`load()` 遇到不认识的版本当空库处理
# （**不是**当坏文件 —— 那是「新版本写的文件被老版本读到」，改名成 .bad 会
# 把人家的数据搞乱）。
STORE_VERSION = 1

# 最多留多少天。一天一条，2000 天 ≈ 5 年半，单个账号撑死几百 KB。
# ⚠️ 不是「怕文件大」，是**防一个永远涨的东西**：万一哪天日期解析出 bug
#    生成了无数条垃圾日期，没有上限就是无限增长。
MAX_DAYS = 2000

# 一天的记录里有哪几个字段。**白名单**：多余字段一律丢掉 ——
# 免得哪天有人手改文件塞进来别的东西，又跟着存下去。
_FIELDS = ("flow", "up", "down", "hours")


# ---------------------------------------------------------------- 读 / 写

def empty_store():
    """一个空库。"""
    return {"version": STORE_VERSION, "accounts": {}}


def load(path):
    """读库。返回 `(store, why)`；`why` 非空表示**这次没读到**（原因）。

    ⚠️ **任何异常都不往外抛** —— 调用方拿到空库照常画界面，把 `why` 记一行日志。
    """
    if not path or not os.path.isfile(path):
        return empty_store(), ""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        _quarantine(path)
        return empty_store(), "本地流量历史读不出来（%s），已把它改名成 .bad 留着" % type(e).__name__
    if not isinstance(raw, dict):
        _quarantine(path)
        return empty_store(), "本地流量历史的格式不认识，已把它改名成 .bad 留着"

    ver = raw.get("version")
    if ver != STORE_VERSION:
        # 版本不认识 ⇒ 当空库（**不改名**：那是版本差异，不是文件坏了）
        return empty_store(), "本地流量历史的版本是 %r，这个版本的程序读不了" % (ver,)

    accounts = raw.get("accounts")
    store = empty_store()
    if not isinstance(accounts, dict):
        return store, ""
    for acct, one in accounts.items():
        if not isinstance(acct, str) or not acct.strip():
            continue
        store["accounts"][acct] = _clean_account(one)
    return store, ""


def save(path, store):
    """原子写。返回 `(ok, why)`；**不抛异常**。

    先写 `<path>.tmp` 再 `os.replace()` —— 同目录下的 replace 是原子的，
    所以「写到一半被杀」只会留下一个 `.tmp`，不会把好文件毁成半个。
    """
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(store, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True, ""
    except Exception as e:
        return False, "本地流量历史写不进去（%s: %s）" % (type(e).__name__, e)


def _quarantine(path):
    """把读不出来的文件改名成 `.bad` 留着（**不删**）。已经有一份 `.bad` 就不动。"""
    try:
        bad = path + ".bad"
        if not os.path.exists(bad):
            os.replace(path, bad)
    except Exception:
        pass


def _clean_record(rec):
    """把一条记录规整成白名单字段；**用不了就返回 `None`**。

    🔴 **合并和读盘必须共用这一个。** 两处各写一遍清洗逻辑的话迟早漂移 ——
    2026-10-12 的第一版就是「读盘时清洗、合并时不清洗」，于是
    `{"date": "2026-10-02", "flow": "abc"}` 这种脏数据被**原样存进了库**，
    下次打开面板读出来就是「流量 abc」。而且当场同步时看不出来，
    要等下次打开才发现 —— 典型的「静默说谎」。

    判据（`_FIELDS` 里的每一个）：
      · `None` ⇒ 保持 `None`（「不知道」，不是 0）
      · `bool` ⇒ `None`（`True`/`False` 是 `int` 的子类，不拦住就会变成 1/0）
      · 数字 ⇒ `float`
      · 其它（字符串、dict…）⇒ `None`
    连 `flow` 都拿不到 ⇒ 整条丢掉（一条连合计都没有的记录画不出柱子）。
    """
    if not isinstance(rec, dict):
        return None
    out = {}
    for k in _FIELDS:
        v = rec.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            out[k] = None
        else:
            out[k] = float(v)
    if out.get("flow") is None:
        return None
    return out


def _clean_account(one):
    """规整一个账号的记录：只留白名单字段，坏的整条丢掉。"""
    out = {"updated": "", "days": {}}
    if not isinstance(one, dict):
        return out
    up = one.get("updated")
    out["updated"] = up if isinstance(up, str) else ""
    days = one.get("days")
    if not isinstance(days, dict):
        return out
    for date, rec in days.items():
        if not isinstance(date, str) or not date.strip():
            continue
        clean = _clean_record(rec)
        if clean is not None:
            out["days"][date] = clean
    return out


# ---------------------------------------------------------------- 合并

def _same(a, b):
    """两个数「算不算同一个」。`None` 只等于 `None`。

    ⚠️ 浮点用容差比：服务端同一天的值一般逐位相同，但没必要为了
    第 15 位小数判定成「变了」而多写一次盘、还多报一次「更新了 N 天」。
    """
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def merge(store, account, days, today=None):
    """把服务端拿到的明细合进库里。返回 `(新增天数, 更新天数)`。

    🔴 合并规则（**用户 2026-10-12 明确要求的**）：
        · 同一天、数据不同 ⇒ **远端覆盖本地**（远端是权威）；
        · 本地没有的日期 ⇒ 直接存进去；
        · 数据相同 ⇒ 什么都不做（不写盘、也不报「更新了」）。

    `today` 是「今天那条」（`parse_days` 给的 `summary["today"]`）——
    它**不在**服务端的日期数组里（实测），所以单独合一次。
    ⚠️ 今天那条的 `up`/`down` 是 `None`（服务端不给）——
    等明天再同步时它会从 `dates` 里带着真的上下行回来，**覆盖规则天然处理**。
    """
    if not isinstance(store, dict):
        store = empty_store()
    accounts = store.setdefault("accounts", {})
    acct = accounts.setdefault(account, {"updated": "", "days": {}})
    bucket = acct.setdefault("days", {})

    added = updated = 0
    batch = list(days or [])
    if isinstance(today, dict) and today.get("date"):
        batch.append(today)

    for rec in batch:
        if not isinstance(rec, dict):
            continue
        date = str(rec.get("date") or "").strip()
        if not date:
            continue
        # 🔴 和读盘**共用**同一个清洗函数 —— 别在这里另写一份（会漂移，
        #    第一版就是这里漏了清洗，脏数据被原样存进了库）。
        new = _clean_record(rec)
        if new is None:
            continue
        old = bucket.get(date)
        if old is None:
            bucket[date] = new
            added += 1
        elif any(not _same(old.get(k), new.get(k)) for k in _FIELDS):
            bucket[date] = new
            updated += 1
        # 完全相同 ⇒ 不动（保持旧对象，连盘都不用写）
    _prune(bucket)
    return added, updated


def _prune(bucket):
    """超过 `MAX_DAYS` 就把最老的丢掉（日期是 `YYYY-MM-DD`，字典序即时间序）。"""
    if len(bucket) <= MAX_DAYS:
        return
    for date in sorted(bucket)[:len(bucket) - MAX_DAYS]:
        bucket.pop(date, None)


def mark_updated(store, account, stamp):
    """记下「这个账号上次同步成功是 `stamp`」（一个给人看的时间串）。"""
    if isinstance(store, dict) and account in store.get("accounts", {}):
        store["accounts"][account]["updated"] = stamp or ""


# ---------------------------------------------------------------- 查询

def days_of(store, account):
    """某个账号的每日明细，**按日期升序**的列表。"""
    try:
        bucket = store["accounts"][account]["days"]
    except (KeyError, TypeError):
        return []
    if not isinstance(bucket, dict):
        return []
    out = []
    for date in sorted(bucket):
        rec = bucket[date]
        if isinstance(rec, dict):
            one = {"date": date}
            one.update({k: rec.get(k) for k in _FIELDS})
            out.append(one)
    return out


def updated_at(store, account):
    """某个账号上次同步成功的时间串（没同步过就是空串）。"""
    try:
        v = store["accounts"][account]["updated"]
    except (KeyError, TypeError):
        return ""
    return v if isinstance(v, str) else ""


def accounts_in(store):
    """库里有哪些账号（升序）。"""
    if not isinstance(store, dict):
        return []
    a = store.get("accounts")
    return sorted(a) if isinstance(a, dict) else []


def clear(store, account=None):
    """清掉某个账号的历史；`account=None` 清全部。返回清掉的账号数。"""
    if not isinstance(store, dict):
        return 0
    accounts = store.get("accounts")
    if not isinstance(accounts, dict):
        return 0
    if account is None:
        n = len(accounts)
        store["accounts"] = {}
        return n
    return 1 if accounts.pop(account, None) is not None else 0


# ---------------------------------------------------------------- 汇总（纯函数）

def group_by_month(days):
    """按「月」聚合。返回 `[{"month": "2026-10", "flow":…, "up":…, "down":…,
    "hours":…, "n": 天数, "up_days":…, "down_days":…, "hours_days":…}, …]`，
    按月份升序。

    ⚠️ 上下行/时长是**「知道的那几天」的和**，同时把天数也返回（口径要能说清）。
       理由见 `summarize()` —— 今天的上下行服务端不给，而今天几乎总在明细里；
       写成「一天不知道就整月给 None」的话，界面上永远显示「—」。
       一天都不知道时仍然给 `None`（「完全没有信息」≠「算了 0 天」）。
    """
    buckets = {}
    for d in days or []:
        if not isinstance(d, dict):
            continue
        date = str(d.get("date") or "")
        if len(date) < 7:
            continue
        m = date[:7]
        b = buckets.setdefault(m, {"flow": 0.0, "up": 0.0, "down": 0.0,
                                   "hours": 0.0, "n": 0,
                                   "up_days": 0, "down_days": 0,
                                   "hours_days": 0})
        b["flow"] += float(d.get("flow") or 0.0)
        b["n"] += 1
        for key in ("up", "down", "hours"):
            v = d.get(key)
            if v is not None:
                b[key] += float(v)
                b[key + "_days"] += 1

    out = []
    for m in sorted(buckets):
        b = buckets[m]
        out.append({
            "month": m,
            "flow": b["flow"],
            "up": b["up"] if b["up_days"] else None,
            "down": b["down"] if b["down_days"] else None,
            "hours": b["hours"] if b["hours_days"] else None,
            "n": b["n"],
            "up_days": b["up_days"],
            "down_days": b["down_days"],
            "hours_days": b["hours_days"],
        })
    return out


def summarize(days):
    """一段明细的汇总。

    返回 `{"flow", "up", "down", "hours", "n", "avg",
           "up_days", "down_days", "hours_days"}`。

    🔴 **上下行/时长给的是「知道的那几天」的和，并把天数一起返回。**
       为什么不是「一天不知道就整月给 None」：**今天的上下行服务端不给**
       （实测，见 `self_service.parse_days`），而今天几乎总在明细里 ——
       于是那种写法会让界面上**永远显示「—」**，等于把用户要看的数藏起来了。
       现在的口径是「给部分和 + 说清算了几天的」：数字是真的，口径也写明白了。
       ⚠️ 但**一天都不知道**时仍然给 `None` —— 那是「完全没有信息」，
          和「算了 0 天得到 0」是两回事。
    ⚠️ `avg` = 共计 ÷ **有记录的天数**（不是自然日天数）—— 界面上要写清楚，
       否则「日均」这个数会被理解成另一种口径。
    """
    days = [d for d in (days or []) if isinstance(d, dict)]
    flow = sum(float(d.get("flow") or 0.0) for d in days)
    up = down = hours = 0.0
    up_days = down_days = hours_days = 0
    for d in days:
        for key in ("up", "down", "hours"):
            v = d.get(key)
            if v is None:
                continue
            if key == "up":
                up += float(v)
                up_days += 1
            elif key == "down":
                down += float(v)
                down_days += 1
            else:
                hours += float(v)
                hours_days += 1
    n = len(days)
    return {
        "flow": flow,
        "up": up if up_days else None,
        "down": down if down_days else None,
        "hours": hours if hours_days else None,
        "n": n,
        "up_days": up_days,
        "down_days": down_days,
        "hours_days": hours_days,
        "avg": (flow / n) if n else 0.0,
    }
