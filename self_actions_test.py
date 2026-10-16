#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`self_actions.py` 自检。**全程不联网、不碰真账号。**

🔴 **为什么这个测试比别的更重要**：`self_actions.py` 里全是**不可逆的写操作**
（踢下线 / 解绑 MAC / 停机 / 改密码）。别的模块测漏了顶多是「显示不对」，
这里测漏了是**「把用户的网断了」**。所以这个文件要钉住四件事：

  1. **参数结构**：每个方法的请求体必须**逐字段**对上前端 `app.js` 逆出来的结构。
     多一个字段、少一个字段、类型不对（`3` vs `"3"`）都要报出来。
  2. **失败分类**：`net` / `session` / `denied` / `refused` / `other` 五类不能串。
     尤其是「网络不通（可以重试）」和「学校明确拒绝（不许重试）」必须分得开。
  3. **绝不重试**：每个方法跑完，假服务端的调用记录必须**只有一条**。
  4. **主动不做的**：改密码要短信码时**必须拒绝**，而且**一个字节都不能发**
     （`getcode.do` 是发真短信的）。

⚠️ 这里**没有**、也**不该有**「拿真账号跑一遍」的测试 —— 那是用户自己点的事。
"""

import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import self_actions as A       # noqa: E402
import self_service as S       # noqa: E402

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


class FakeSvc(object):
    """假的 `SelfService`：记下每次调用，按预设返回或抛异常。**不联网。**"""

    def __init__(self, ret=None, raise_on=None):
        self.calls = []
        self.ret = ret or {}
        self.raise_on = raise_on or {}

    def _post(self, path, payload=None):
        self.calls.append((path, payload))
        if path in self.raise_on:
            raise self.raise_on[path]
        if path in self.ret:
            return self.ret[path]
        raise AssertionError("测试没准备这个接口的假数据：%s" % path)

    def paths(self):
        return [c[0] for c in self.calls]


ONLINE_ROW = {
    "accountId": "2025000000",
    "accountMac": "84:9E:56:40:69:1D",
    "accountIp": "10.15.116.156",
    "onlineTime": "2026-10-12 09:37:49",
    "osInfo": "Windows",
    "nasId": "ZZHK-ZAX-BRAS",
    "terminalType": "a",
    "billingId": "bee1f27f",
    "serverIp": "10.0.1.10",
}


def _err(kind, msg="boom"):
    return S.SelfError(kind, msg)


# --------------------------------------------------------------- 1. 踢下线

def test_kick():
    print("\n--- 1. 踢设备下线 ---")
    svc = FakeSvc({A.EP_ONLINE: {"rows": [ONLINE_ROW]}})
    act = A.SelfActions(svc)

    targets = act.online_targets("2025000000")
    check("在线设备 1 条", len(targets), 1)
    # 🔴 这两个是踢人必需的 —— 和 self_service.online() 故意不同
    check("保留 billing_id", targets[0]["billing_id"], "bee1f27f")
    check("保留 server_ip", targets[0]["server_ip"], "10.0.1.10")

    svc.calls = []
    svc.ret[A.EP_KICK] = {"errmsg": "操作成功"}
    r = act.kick("2025000000", targets[0])
    check("kick 请求体（四个字段全部来自 rows）", svc.calls[0][1],
          {"accountId": "2025000000", "accountIp": "10.15.116.156",
           "billingId": "bee1f27f", "serverIp": "10.0.1.10"})
    check("kick 只调一次（绝不重试）", len(svc.calls), 1)
    check("kick 返回 ok", r["ok"], True)

    # 缺字段 → 直接拒，不发请求
    svc2 = FakeSvc()
    for bad in ({}, {"ip": "1.2.3.4"}, {"ip": "1.2.3.4", "billing_id": "x"},
                None, "不是字典"):
        try:
            A.SelfActions(svc2).kick("2025000000", bad)
            check_true("缺字段 %r 应当被拒" % (bad,), False, "")
        except A.ActionError as e:
            check_true("缺字段 %r 被拒" % (bad,), e.kind == "other", "")
    check("缺字段时一个请求都没发", svc2.calls, [])


# --------------------------------------------------------------- 2. 解绑 MAC

def test_clear_mac():
    print("\n--- 2. 解绑 MAC ---")
    svc = FakeSvc({A.EP_CLEAR_MAC: {"errmsg": "操作成功"}})
    act = A.SelfActions(svc)

    act.clear_mac("2025000000", "3")
    check("clear_mac 请求体", svc.calls[0][1],
          {"accountId": "2025000000", "clearType": "3"})
    check("clear_mac 只调一次", len(svc.calls), 1)

    # 数字 3 也要能发（实测 clearType 可能是数字）
    svc.calls = []
    act.clear_mac("2025000000", 3)
    check("数字槽位也转成字符串", svc.calls[0][1]["clearType"], "3")

    # 槽位号只认 1~6
    # ⚠️ "1 "（带尾空格）**不在**这个列表里 —— 它会被 .strip() 成合法的 "1"，
    #    下面单独有一条正向断言钉住这个行为（用户从表格里复制 clearType 常带空格）。
    svc2 = FakeSvc()
    for bad in ("0", "7", "9", "", "   ", None, "abc", "3.5", "-1", 0, 7, 1.5):
        try:
            A.SelfActions(svc2).clear_mac("2025000000", bad)
            check_true("槽位 %r 应当被拒" % (bad,), False, "")
        except A.ActionError as e:
            check_true("槽位 %r 被拒" % (bad,), e.kind == "other", "")
    check("非法槽位时一个请求都没发", svc2.calls, [])

    # 正向：带空格的 "1 " 会被 strip 成合法槽位（这是有意的，不是漏网）
    svc3 = FakeSvc({A.EP_CLEAR_MAC: {"errmsg": "操作成功"}})
    A.SelfActions(svc3).clear_mac("2025000000", " 1 ")
    check('带空格的 " 1 " 被 strip 成合法槽位', svc3.calls[0][1]["clearType"], "1")


# --------------------------------------------------------------- 3. 停机复机

def test_stop_recover():
    print("\n--- 3. 停机 / 复机 / 取消预约 ---")
    svc = FakeSvc({A.EP_STOP: {"errmsg": "操作成功"},
                   A.EP_RECOVER: {"errmsg": "操作成功"},
                   A.EP_CANCEL: {"errmsg": "操作成功"}})
    act = A.SelfActions(svc)

    act.stop_now("2025000000", "暑假不用")
    check("stop_now 请求体（operType=1 那条分支）", svc.calls[-1][1],
          {"accountId": "2025000000", "shutdownReason": "暑假不用"})

    act.stop_scheduled("2025000000", "2026-11-01", "寒假")
    check("stop_scheduled 请求体（operType=2 那条分支）", svc.calls[-1][1],
          {"accountId": "2025000000", "stopDate": "2026-11-01",
           "shutdownReason": "寒假"})

    act.recover("2025000000")
    check("recover 请求体", svc.calls[-1][1], {"accountId": "2025000000"})

    act.cancel_scheduled("2025000000")
    check("cancel_scheduled 请求体", svc.calls[-1][1], {"accountId": "2025000000"})

    # 空原因 / 空日期（前端自己也校验这两条）
    svc2 = FakeSvc()
    a2 = A.SelfActions(svc2)
    for fn, args in ((a2.stop_now, ("2025000000", "")),
                     (a2.stop_now, ("2025000000", "   ")),
                     (a2.stop_now, ("2025000000", None)),
                     (a2.stop_scheduled, ("2025000000", "", "理由")),
                     (a2.stop_scheduled, ("2025000000", "2026-11-01", ""))):
        try:
            fn(*args)
            check_true("空参数 %r 应当被拒" % (args[1:],), False, "")
        except A.ActionError as e:
            check_true("空参数 %r 被拒" % (args[1:],), e.kind == "other", "")
    check("空参数时一个请求都没发", svc2.calls, [])


# --------------------------------------------------------------- 4. 失败分类

def test_error_kinds():
    print("\n--- 4. 🔴 失败分类：可以重试 vs 不许重试 ---")
    cases = (("session", "session", "会话过期"),
             ("net", "net", "连不上"),
             ("other", "denied", "账号未停机"),
             ("captcha", "denied", "别的"))
    for src_kind, want_kind, msg in cases:
        svc = FakeSvc(raise_on={A.EP_RECOVER: _err(src_kind, msg)})
        try:
            A.SelfActions(svc).recover("2025000000")
            check_true("SelfError(%s) 应当转成 ActionError" % src_kind, False, "")
        except A.ActionError as e:
            check("SelfError(%s) → ActionError(%s)" % (src_kind, want_kind),
                  e.kind, want_kind)
            check("  原文保留（%s）" % src_kind, e.msg, msg)
        check("失败时也只调一次", len(svc.calls), 1)

    # 🔴 `net` 和 `denied` 必须分得开 —— 界面靠这个决定要不要给「重试」按钮
    check_true("net 和 denied 是不同的 kind", "net" != "denied", "")


# --------------------------------------------------------------- 5. 改密码

def test_change_password():
    print("\n--- 5. 🔴 改密码（含「要短信码就拒绝」）---")
    # codeFlag=0 ⇒ 能改
    svc = FakeSvc({A.EP_PWD_POLICY: {"data": {"codeFlag": "0", "qiangPwd": "0"}},
                   A.EP_CHANGE_PWD: {"errmsg": "修改成功"}})
    act = A.SelfActions(svc)
    pol = act.pwd_policy("2025000000")
    check("codeFlag=0 → 可以改", pol["can_change"], True)

    r = act.change_password("2025000000", "old123", "new456")
    check("改密码请求体（明文，和前端一致）", svc.calls[-1][1],
          {"accountId": "2025000000", "oldPassword": "old123",
           "newPassword": "new456"})
    # 调用序列是：策略（上面第 227 行显式那次）→ 策略（change_password 内部自查）→ 改密码。
    # ⚠️ 所以总调用是 3，不是 2 —— 这里要断言的重点是「**改密码这个写接口只发一次**」，
    #    数总次数会把「策略查询」也算进来，一旦以后策略查询多调一次就会误报。
    check("改密码这个写接口只发一次",
          len([p for p in svc.paths() if p == A.EP_CHANGE_PWD]), 1)
    check("返回 ok", r["ok"], True)

    # 🔴 codeFlag=1 ⇒ 拒绝，且**一个字节都不发**
    svc2 = FakeSvc({A.EP_PWD_POLICY: {"data": {"codeFlag": "1"}}})
    act2 = A.SelfActions(svc2)
    try:
        act2.change_password("2025000000", "old", "new")
        check_true("要短信码时应当拒绝", False, "")
    except A.ActionError as e:
        check("kind 是 refused（我们主动不做）", e.kind, "refused")
        check_true("原因里提到短信", "短信" in e.msg, e.msg)
    check("只调了策略接口", svc2.paths(), [A.EP_PWD_POLICY])
    # ⚠️ 判据**必须**是精确的 "getcode.do"。
    #    写成 `"getcode" in p` 会**永远为假** —— 因为 `getcodeparameter.do`
    #    本身就含 "getcode" 这串（子串匹配冒充精确匹配，属于静默 bug）。
    check_true("🔴 没有调 getcode.do（不发短信）",
               "getcode.do" not in svc2.paths(), "")

    # 🔴 没见过的 codeFlag ⇒ 也拒绝（红线 51：不许往「能改」上兜底）
    for unknown in ("2", "9", "", None):
        svc3 = FakeSvc({A.EP_PWD_POLICY: {"data": {"codeFlag": unknown}}})
        pol3 = A.SelfActions(svc3).pwd_policy("x")
        check("未知 codeFlag=%r → can_change" % (unknown,), pol3["can_change"],
              False)

    # 新旧一样 / 空
    svc4 = FakeSvc({A.EP_PWD_POLICY: {"data": {"codeFlag": "0"}}})
    a4 = A.SelfActions(svc4)
    for args in (("x", "same", "same"), ("x", "", "new"), ("x", "old", "")):
        try:
            a4.change_password(*args)
            check_true("非法 %r 应当被拒" % (args[1:],), False, "")
        except A.ActionError as e:
            check_true("非法 %r 被拒" % (args[1:],), e.kind == "other", "")
    check("非法参数时一个请求都没发", svc4.calls, [])


# --------------------------------------------------------------- 6. 源码扫描

def _literal_strings(path):
    """取源码里**非 docstring** 的字符串字面量。

    🔴 **为什么要排除 docstring**：本模块的文档里**故意**写着
       「绝不调 `getcode.do`」这类警告。把警告当违规，就变成
       「越写清楚越报错」，反而逼人删掉警告。
       判据是「**代码里实际出现的**接口名」，不是「文件里出现过这几个字」。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr):
                v = body[0].value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    doc_ids.add(id(v))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in doc_ids:
                out.append(node.value)
    return out


def test_source_scan():
    print("\n--- 6. 🔴 源码里只许出现白名单里的接口 ---")
    src = os.path.join(HERE, "self_actions.py")
    joined = "\n".join(_literal_strings(src))
    import re
    found = sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9]*\.do", joined)))

    # 允许的：6 个写接口 + 2 个只读（一个要保留踢人参数、一个查改密码策略）
    allowed = ["canceluse.do", "changepassword.do", "clearusermac.do",
               "getcodeparameter.do", "getonline.do", "kickonline.do",
               "selfrecovery.do", "selfstopping.do"]
    check("实际用到的接口集合", found, allowed)

    # 🔴 逐个点名「绝对不许出现」的
    banned = ["getcode.do", "onlinePay", "cardcharge", "moneytransfer",
              "buybusinesspackage", "userdatachange", "register.do",
              "uploadimg", "userpwdreset", "modifyoperators",
              "usergroupchange", "subscribeOrder", "mealTrnCheckOrder",
              "billdetailexport", "serviceHallOder", "payway", "wxcodepay",
              "getqrAuth", "surfinternet"]
    for b in banned:
        check_true("字面量里没有 %s" % b, b not in joined, "")

    # 不碰磁盘（模块规矩 3）
    mods = set()
    for node in ast.walk(ast.parse(open(src, encoding="utf-8").read())):
        if isinstance(node, ast.Import):
            for n in node.names:
                mods.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module.split(".")[0])
    check_true("没有用到 os / pathlib / shutil（不碰文件系统）",
               not ({"os", "pathlib", "shutil"} & mods), sorted(mods))
    check_true("没有引入 logging", "logging" not in mods, "")

    # 不许有 open/print/exec/eval
    bad = []
    for node in ast.walk(ast.parse(open(src, encoding="utf-8").read())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("open", "print", "exec", "eval", "compile"):
                bad.append(node.func.id)
    check("没有 open/print/exec/eval", sorted(set(bad)), [])


# --------------------------------------------------------------- 7. 不重试

def test_no_retry():
    print("\n--- 7. 🔴 每个写方法都只发一次请求（不重试）---")
    # 让**每个**写接口都失败一次，看调用次数
    ret = {A.EP_ONLINE: {"rows": [ONLINE_ROW]},
           A.EP_PWD_POLICY: {"data": {"codeFlag": "0"}}}
    raise_on = {A.EP_KICK: _err("net", "超时"),
                A.EP_CLEAR_MAC: _err("net", "超时"),
                A.EP_STOP: _err("net", "超时"),
                A.EP_RECOVER: _err("net", "超时"),
                A.EP_CANCEL: _err("net", "超时"),
                A.EP_CHANGE_PWD: _err("net", "超时")}
    svc = FakeSvc(ret, raise_on)
    act = A.SelfActions(svc)
    targets = act.online_targets("2025000000")

    calls = [
        (lambda: act.kick("x", targets[0]), A.EP_KICK),
        (lambda: act.clear_mac("x", "1"), A.EP_CLEAR_MAC),
        (lambda: act.stop_now("x", "理由"), A.EP_STOP),
        (lambda: act.stop_scheduled("x", "2026-11-01", "理由"), A.EP_STOP),
        (lambda: act.recover("x"), A.EP_RECOVER),
        (lambda: act.cancel_scheduled("x"), A.EP_CANCEL),
        (lambda: act.change_password("x", "old", "new"), A.EP_CHANGE_PWD),
    ]
    for fn, ep in calls:
        svc.calls = []
        try:
            fn()
            check_true("%s 失败时应当抛" % ep, False, "")
        except A.ActionError as e:
            check_true("%s 失败归到 net（可重试）" % ep, e.kind == "net", e.kind)
        n = len([p for p in svc.paths() if p == ep])
        check("%s 只发一次请求" % ep, n, 1)


def main():
    print("=" * 78)
    print("self_actions.py 自检（不联网、不碰真账号）")
    print("=" * 78)
    test_kick()
    test_clear_mac()
    test_stop_recover()
    test_error_kinds()
    test_change_password()
    test_source_scan()
    test_no_retry()
    print()
    print("=" * 78)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    sys.exit(main())
