# -*- coding: utf-8 -*-
"""门户表单「逐键不漂移」测试。

为什么要单独一个测试（2026-10-03）：
    `_auth_page`（取认证页）和 `portal_logout`（下线）要 POST 的表单里，
    有 20 个字段是**同一套门户模板参数**。2026-10-03 之前这三处
    （`LOGIN_EXTRA` / `_auth_page` / `portal_logout`）是各抄一份的 ——
    改一处漏两处就会出现「能登录但取不到 distoken」这类**只有逐键对比表单
    才看得出来**的怪现象（认证逻辑本身完全正常，日志里也一片祥和）。

    所以这里把**重构前的字面量**固化成金标准（GOLD_AUTH / GOLD_LOGOUT），
    断言构造函数产出的表单与它逐键相同：既证明那次重构没改行为，
    也防止以后有人只改一处。

⚠️ 金标准是「当时发给门户的真实内容」，不是理想值。
   真要改门户参数时，**先确认门户那边确实变了**，再同步更新这里 ——
   这个测试失败是在提醒你「你动了要发给门户的东西」，别顺手改金标准把它弄绿。

金标准怎么来的：用 AST 从**收敛前**的 campus_login.py 里，把 `_auth_page` /
`portal_logout` 两处字面量逐键提取出来固化的 —— 机器抄的，不是手抄（手抄 44 个
键值对必然出错）。生成脚本是一次性的，没留在仓库：它只对「收敛前」的源码有意义。

用法：
    python form_drift_test.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C

FAILS = []


def check(name, got, want):
    ok = got == want
    print("  [%s] %-40s -> %r%s" % ("OK" if ok else "失败", name, got,
                                    "" if ok else "   (期望 %r)" % (want,)))
    if not ok:
        FAILS.append(name)


# 重构前 _auth_page 的表单（22 键）
GOLD_AUTH = {
    "scheme":              'http',
    "serverIp":            'tomcat_server:80',
    "hostIp":              'http://127.0.0.1:8080/',
    "loginType":           'auth_type',
    "isBindMac1":          '0',
    "pageid":              '5',
    "templatetype":        '1',
    "listbindmac":         '0',
    "recordmac":           '0',
    "isRemind":            '1',
    "loginTimes":          '',
    "groupId":             '',
    "distoken":            '',
    "echostr":             '',
    "isautoauth":          '',
    "mobile":              '',
    "notice_pic_loop1":    '/portal/uploads/pc/demo3/images/logo.jpg',
    "notice_pic_loop2":    '/portal/uploads/pc/demo3/images/rrs_bg.jpg',
    "userId":              "U1",
    "passwd":              "P1",
    "remInfo":             'on',
    "desc_lb":             'on',
}


# 重构前 portal_logout 的表单（22 键）
GOLD_LOGOUT = {
    "scheme":              'http',
    "serverIp":            'tomcat_server:80',
    "hostIp":              'http://127.0.0.1:8080/',
    "loginType":           'auth_type',
    "auth_type":           '0',
    "isBindMac1":          '0',
    "pageid":              '5',
    "templatetype":        '1',
    "listbindmac":         '0',
    "recordmac":           '0',
    "isRemind":            '1',
    "loginTimes":          '',
    "groupId":             '',
    "distoken":            "T1",
    "echostr":             '',
    "isautoauth":          '',
    "mobile":              '',
    "notice_pic_loop1":    '/portal/uploads/pc/demo3/images/logo.jpg',
    "notice_pic_loop2":    '/portal/uploads/pc/demo3/images/rrs_bg.jpg',
    "url":                 'http://1.1.1.1/',
    "userId":              "U1",
    "other1":              'disconn',
}



def main():
    print("=== 1. auth_form() 与重构前的字面量逐键一致 ===")
    got = C.auth_form("U1", "P1")
    check("键集合", sorted(got), sorted(GOLD_AUTH))
    for k in sorted(GOLD_AUTH):
        check("auth_form[%r]" % k, got.get(k, "<缺失>"), GOLD_AUTH[k])

    print()
    print("=== 2. logout_form() 与重构前的字面量逐键一致 ===")
    got2 = C.logout_form("U1", "T1")
    check("键集合", sorted(got2), sorted(GOLD_LOGOUT))
    for k in sorted(GOLD_LOGOUT):
        check("logout_form[%r]" % k, got2.get(k, "<缺失>"), GOLD_LOGOUT[k])

    print()
    print("=== 3. 三个入口的公共字段确实同源 ===")
    a, l = C.auth_form("U1", "P1"), C.logout_form("U1", "T1")
    shared = list(C.LOGIN_EXTRA)
    same = [k for k in shared if a.get(k) == C.LOGIN_EXTRA[k]]
    check("auth_form 覆盖了 LOGIN_EXTRA 的全部键", len(same), len(shared))
    # 下线表单**故意**改掉三类键：去掉两个「登录页显示开关」、distoken 换成真 token。
    # ⚠️ distoken 也算「故意覆盖」—— 第一版断言漏了它，才误报成 17/18。
    dropped = {"remInfo", "desc_lb"}
    overridden = {"distoken"}
    kept = [k for k in shared if k not in dropped and k not in overridden]
    same2 = [k for k in kept if l.get(k) == C.LOGIN_EXTRA[k]]
    check("logout_form 保留其余全部键（只改开关 + distoken）",
          len(same2), len(kept))
    check("logout_form 的 distoken 确实是传进去的 token", l.get("distoken"), "T1")

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 三个入口的表单与重构前逐键一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
