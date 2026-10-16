#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""郑航自助服务（/self/）**写操作**层 —— 踢设备下线 / 解绑 MAC / 停机复机 / 改密码。

**为什么单独一个模块**（而不是塞进 `self_service.py`）：

  · `self_service.py` 的契约是「**只读**」，而且**有测试扫源码钉着**
    （`self_service_test.py` 第 5 段：代码里出现 `kickonline` / `clearusermac` /
    `changepassword` 等字样就直接报失败）。写操作进去会把那条契约毁掉 ——
    而那条契约保护的是「这个模块不会在你不注意的时候动你的账号」。
  · 两类操作的**风险等级完全不同**：只读挂了顶多没数据；写操作挂了可能
    **断网、掉线、把密码改掉**。混在一个文件里，改 A 的时候很容易碰到 B。
  · 分开之后能**单独测**（而且必须单独测 —— 写操作不能拿真账号试）。

🔴 **六条不可破的规矩**：

  1. **每个写动作都不可逆。** 调用方（界面）必须**先二次确认**再调这里的方法。
     本模块**不做确认** —— 它假设「你已经问过用户了」。

  2. **失败一律不重试。** 写操作的失败可能是「其实成功了，只是响应超时」，
     重试等于**做第二遍**（踢两次 / 停机两次 / 改两次密码）。
     所以这里所有方法都是**一次调用**，不循环、不重发。
     ⚠️ 和 `self_service` 那边正相反：那边读失败了可以再读一次，这边绝对不行。

  3. **不碰磁盘、不写日志。** 改密码之后要把新密码写回本机凭据 ——
     那是**调用方**的事（`campus_login.py` 里的凭据存储），不是这里。
     本模块只负责「把请求发出去、把结果说出来」。
     （`self_actions_test.py` 里有断言钉着「没用到 os / pathlib」。）

  4. 🔴 **改密码要短信验证码时，直接拒绝，不发短信。**
     `getcodeparameter.do` 的 `codeFlag == "1"` 表示这个账号改密码要短信码，
     而拿短信码要调 `getcode.do`（**发真短信**）。发短信有副作用
     （骚扰用户、可能触发风控），所以这种情况我们**不做**，
     明确告诉用户「去学校网页改」。

  5. 🔴 **踢人必须显式传「踢哪一个」。** 本模块**不提供**「给个下标就踢」的接口 ——
     `kick()` 要的是 `online_targets()` 返回的**那一条完整记录**。
     理由：`self_service.online()` **故意丢掉了** `billingId` / `serverIp`
     （那两个正是踢人必需的参数），就是为了让只读模块**结构上凑不出**踢人请求。
     这里重新拉一份带参数的清单，但**参数必须由调用方从清单里挑出来传进来**，
     不存在「内部自己拉一次然后踢」的捷径 —— 那种写法迟早会踢错设备。

  6. 🔴 **参数里的密码是明文发出去的。** 实测 `changepassword.do` 的前端
     **不做任何加密**（登录用的 MD5 只用在 `login.do` 上），
     而服务器是 **HTTP**（不是 HTTPS）。这是**学校的设计**，不是我们选的。
     界面在改密码前必须把这件事告诉用户。

⚠️ **本模块所有接口的参数结构都是从 `app.js` 逆出来的前端逻辑**（2026-10-16），
   **没有用真账号实测过**（写操作不能拿来试）。所以：
   · 参数错了会拿到学校的报错（`errcode != "0"`），我们会**原样显示**给用户；
   · **不会静默失败** —— 见 `_post()` 的错误分类。
"""

import self_service

# ---- 写接口（全部实测「存在」，参数从 app.js 逆出）----
EP_KICK = "kickonline.do"        # 踢设备下线
EP_CLEAR_MAC = "clearusermac.do"  # 解绑 MAC
EP_STOP = "selfstopping.do"      # 停机（立即 / 预约）
EP_RECOVER = "selfrecovery.do"   # 复机
EP_CANCEL = "canceluse.do"       # 取消预约停机
EP_CHANGE_PWD = "changepassword.do"   # 改密码

# ---- 只读接口，但只服务于本模块的功能 ----
EP_ONLINE = "getonline.do"       # 要保留 kick 需要的 billingId / serverIp
EP_PWD_POLICY = "getcodeparameter.do"  # 改密码的策略（codeFlag / qiangPwd）

# MAC 绑定槽位号。实测 `clearType` 是 `"1"`~`"6"`（`getbaseinfo.do` 里 6 个槽）。
_CLEAR_TYPES = ("1", "2", "3", "4", "5", "6")


class ActionError(Exception):
    """写操作失败。`kind` 决定界面该怎么提示：

    · `"net"`     网络不通 / 超时 → **可以稍后重试**（这次没发出去）
    · `"session"` 会话过期 → 要重新登录（重新读一次验证码）
    · `"denied"`  学校那边明确拒绝了（`errcode != "0"`）→ 🔴 **不要再试**
                  （可能是「设备已离线」「槽位没绑」「密码不对」这类业务原因）
    · `"refused"` 我们**主动不做**（比如改密码要短信码）→ 界面要说清原因
    · `"other"`   其它（参数不合法等）

    ⚠️ `"net"` 和 `"denied"` 的区别很重要：前者是「不知道成没成」，
       后者是「明确没成」。**只有前者**才允许用户再点一次。
    """

    def __init__(self, kind, msg):
        Exception.__init__(self, msg)
        self.kind = kind
        self.msg = msg


class SelfActions(object):
    """一个写操作会话。**复用 `SelfService` 已经登录好的那个会话。**

    用法：
        svc = self_service.SelfService()
        ...（走完验证码 + login）...
        act = SelfActions(svc)
        act.kick(account, target)          # ← 调用方负责先二次确认
    """

    def __init__(self, svc):
        # 🔴 传进来的必须是一个**已经登录**的 SelfService。
        #    不自己 new 一个 —— 那样要再读一次验证码，而且会开第二条会话。
        self.svc = svc

    # ------------------------------------------------------------ 底层

    def _post(self, path, payload):
        """发一个写请求。**只调一次，绝不重试**（模块规矩 2）。

        借用 `self_service.SelfService._post` 是为了**复用同一条会话**
        （cookie 在那个实例的内存 jar 里）。⚠️ 这是**有意的跨模块调用**：
        被调的那个方法是通用的「发个请求」，它本身**不含任何写接口名** ——
        所以 `self_service.py` 的「只读」契约在**结构上**依然成立
        （测试扫源码也扫不到写接口名）。
        """
        try:
            j = self.svc._post(path, payload)
        except self_service.SelfError as e:
            if e.kind == "session":
                raise ActionError("session", e.msg)
            if e.kind == "net":
                raise ActionError("net", e.msg)
            # 走到这里 = 服务器**返回了内容但 errcode != "0"** ⇒ 明确被拒。
            # ⚠️ 也包括「返回的不是 JSON」那种 —— 同样归到「没成功」，
            #    宁可让用户看到原文，也不要假装成功。
            raise ActionError("denied", e.msg)
        # 写接口的返回里，`errmsg` 是给人看的结果说明（前端就是 `$alert(e.errmsg)`）
        return {"ok": True, "msg": str(j.get("errmsg") or "操作成功")}

    # ------------------------------------------------------------ 在线设备

    def online_targets(self, account):
        """在线设备清单，**保留踢人需要的 `billing_id` / `server_ip`**。

        🔴 **和 `self_service.online()` 故意不同** —— 那个**故意丢掉**这两个字段，
           就是为了「结构上凑不出踢人的请求」。本模块是踢人的模块，所以要它们。
           ⚠️ 但也**只在这里**出现：界面拿到之后应该只用来展示，
              用户点了「下线」才把**那一条**原样传给 `kick()`。

        返回的每条：`{ip, mac, since, os, kind, nas, billing_id, server_ip}`
        """
        j = self.svc._post(EP_ONLINE, {"accountId": account})
        rows = j.get("rows")
        rows = rows if isinstance(rows, list) else []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "ip": str(r.get("accountIp") or ""),
                "mac": str(r.get("accountMac") or ""),
                "since": str(r.get("onlineTime") or ""),
                "os": str(r.get("osInfo") or ""),
                "kind": str(r.get("terminalType") or ""),
                "nas": str(r.get("nasId") or ""),
                # 🔴 下面两个是**踢人必需**的，别删
                "billing_id": str(r.get("billingId") or ""),
                "server_ip": str(r.get("serverIp") or ""),
            })
        return out

    def kick(self, account, target):
        """🔴 **踢一个在线设备下线。不可逆 —— 调用方必须先二次确认。**

        `target` 必须是 `online_targets()` 返回的**那一条**（原样传回来）。
        参数结构来自前端 `kick(e)`：`{accountId, accountIp, billingId, serverIp}`
        四个全部来自 `getonline.do` 的 rows。

        ⚠️ **误踢自己会断网**（这是官方确认框的原话：
           「此操作将下线该设备, 是否继续?」）。
        ⚠️ 缺少任一参数就直接拒绝 —— 发一个半截请求过去，
           学校的报错信息看不出是哪儿不对（实测过类似情况）。
        """
        t = target if isinstance(target, dict) else {}
        ip = str(t.get("ip") or "")
        bid = str(t.get("billing_id") or "")
        sip = str(t.get("server_ip") or "")
        if not (ip and bid and sip):
            raise ActionError("other", "这条设备记录缺少必要字段，不能下线"
                                       "（请重新同步一次在线设备）")
        return self._post(EP_KICK, {"accountId": account, "accountIp": ip,
                                    "billingId": bid, "serverIp": sip})

    # ------------------------------------------------------------ MAC 绑定

    def clear_mac(self, account, clear_type):
        """🔴 **解绑一个 MAC 槽位。不可逆 —— 调用方必须先二次确认。**

        参数来自前端 `deleteMac(e)`：`{accountId, clearType}`，
        其中 `clearType` 直接取 `getbaseinfo.do` 的 `bindMsgVo[i].clearType`。

        ⚠️ **解绑后那台设备要重新认证**才能上网。
        ⚠️ 槽位号**只认 1~6**（实测就是 6 个槽）。别的值直接拒绝 ——
           前端是 `e.clearType` 原样传的，我们这里多一道校验，
           因为「传个 7 过去」在学校那边的表现是**未知的**。
        """
        ct = str(clear_type if clear_type is not None else "").strip()
        if ct not in _CLEAR_TYPES:
            raise ActionError("other", "绑定槽位号不对（只能是 1~6）")
        return self._post(EP_CLEAR_MAC, {"accountId": account, "clearType": ct})

    # ------------------------------------------------------------ 停机 / 复机

    def stop_now(self, account, reason):
        """🔴 **立即停机。不可逆（要复机才能上网）—— 调用方必须先二次确认。**

        参数来自前端 `stopSubmit`（`operType == "1"` 那条分支）：
        `{accountId, shutdownReason}`。

        ⚠️ 前端自己校验「停机原因不能为空」，这里照做 ——
           空原因发过去学校多半会拒，但报错信息不好懂。
        ⚠️ **停机之后立刻断网**（这台机器如果正连着校园网，会掉线）。
        """
        r = str(reason or "").strip()
        if not r:
            raise ActionError("other", "停机原因不能为空")
        return self._post(EP_STOP, {"accountId": account, "shutdownReason": r})

    def stop_scheduled(self, account, date, reason):
        """🔴 **预约停机。** 参数来自前端 `stopSubmit`（`operType == "2"` 分支）：
        `{accountId, stopDate, shutdownReason}`。

        ⚠️ 前端校验 `stopDate` 和 `shutdownReason` **都不能为空**（两条 `$alert`），
           这里照做。
        ⚠️ `date` 的格式前端没写死（是个日期选择器），实测**没测过**。
           按 `stopDate` 这个名字和 `getshutdownformation.do` 里回显的
           `stopDate`（形如 `"2026-10-01"`）推断是 `YYYY-MM-DD`。
        """
        d = str(date or "").strip()
        r = str(reason or "").strip()
        if not d:
            raise ActionError("other", "停机时间不能为空")
        if not r:
            raise ActionError("other", "停机原因不能为空")
        return self._post(EP_STOP, {"accountId": account, "stopDate": d,
                                    "shutdownReason": r})

    def recover(self, account):
        """🔴 **复机（复通）。** 参数来自前端 `after()`：`{accountId}`。

        ⚠️ 只有在**停机中**才有意义。没停机时学校会拒（`denied`）。
        """
        return self._post(EP_RECOVER, {"accountId": account})

    def cancel_scheduled(self, account):
        """🔴 **取消预约停机。** 参数来自前端 `resetStop()`：`{accountId}`。

        ⚠️ 这个接口**在 2026-10-15 的盘点里没记到**（当时只记了
           `selfstopping` / `selfrecovery`），是 2026-10-16 逆 `app.js` 才发现的。
           前端方法名叫 `resetStop`，挂在「停用复通」页上。
        """
        return self._post(EP_CANCEL, {"accountId": account})

    # ------------------------------------------------------------ 改密码

    def pwd_policy(self, account):
        """查这个账号**能不能**在自助服务里改密码。**只读，不产生副作用。**

        返回 `{"code_flag": str, "strong": str, "can_change": bool, "why": str}`。

        · `code_flag == "0"` ⇒ 只要旧密码 + 新密码，**可以做**
        · `code_flag == "1"` ⇒ 还要**短信验证码** ⇒ `can_change=False`
          （拿码要调 `getcode.do`，那是**发真短信**，模块规矩 4 说不行）
        · 其它值 ⇒ **不认**，也归到 `can_change=False`（红线 51：
          枚举值没见过的取值绝不能往「可以」上兜底 —— 那会把「改不了」
          显示成「能改」，用户填半天再被拒）
        """
        j = self.svc._post(EP_PWD_POLICY, {"accountId": account})
        d = j.get("data")
        d = d if isinstance(d, dict) else {}
        cf = str(d.get("codeFlag") if d.get("codeFlag") is not None else "")
        strong = str(d.get("qiangPwd") if d.get("qiangPwd") is not None else "")
        if cf == "0":
            return {"code_flag": cf, "strong": strong, "can_change": True,
                    "why": ""}
        if cf == "1":
            return {"code_flag": cf, "strong": strong, "can_change": False,
                    "why": "这个账号改密码需要短信验证码。"
                           "我们不代发短信 —— 请到学校自助服务网页上改。"}
        return {"code_flag": cf, "strong": strong, "can_change": False,
                "why": "学校没有说明这个账号的改密码方式（codeFlag=%s），"
                       "为稳妥起见不在软件里改。" % (cf or "空")}

    def change_password(self, account, old_password, new_password):
        """🔴🔴 **改认证密码。三个后果，调用方必须都处理：**

          1. **不可逆**（改完就是新密码了）⇒ 调用方必须先二次确认。
          2. **自助服务的会话会失效** —— 前端改完是直接 `exit()` 登出 /
             跳回 `http://1.1.1.1` 的。所以改完之后这个会话基本就废了。
          3. **本机存的凭据必须同步更新**，否则**下次开机自动登录会失败**
             —— 这是三个后果里最容易漏的，也是唯一必须由**调用方**做的
             （模块规矩 3：本模块不碰磁盘）。

        🔴 **参数是明文密码**（见模块规矩 6）：实测前端 `changePassword` 里
           `oldPassword` / `newPassword` 是**原样**传的，
           **没有**走登录那个 MD5。所以这里也不加密 ——
           加密了反而对不上。

        ⚠️ 需要短信码的账号会抛 `ActionError("refused", ...)`，**不会发短信**。
        """
        old = str(old_password if old_password is not None else "")
        new = str(new_password if new_password is not None else "")
        if not old or not new:
            raise ActionError("other", "旧密码和新密码都不能为空")
        if old == new:
            # 前端没有这条校验，但「改成一样的」在学校那边多半是报错，
            # 而且报错信息大概率看不出原因。在这里先拦掉。
            raise ActionError("other", "新密码和旧密码一样，不用改")
        pol = self.pwd_policy(account)
        if not pol["can_change"]:
            raise ActionError("refused", pol["why"])
        return self._post(EP_CHANGE_PWD, {"accountId": account,
                                          "oldPassword": old,
                                          "newPassword": new})
