# -*- coding: utf-8 -*-
"""深澜 Srun 适配的测试（嘉兴大学）。

## 测什么

1. **算法与门户 JS 逐字节一致** —— 把门户那份 JS 原样放进 Node 跑，
   和 `campus_login.srun_xencode` / `srun_b64` 对比同一组输入。
   （需要 Node；**没有就跳过这一节**，不判失败。）
2. **纯函数边界** —— 空串 / padding / JSONP 剥壳。
3. **学校档案与配置优先级** —— `school` 键 → 铺开档案 → 单项仍可覆盖。
4. **`portal_login` 的分派** —— `portalType=srun` 走 Srun，其余走 webauth。
5. 🔴 **阳性对照** —— 故意改坏 base64 字符表，对拍**必须**报不一致。
   （没有这一条，「对拍通过」什么都证明不了。）

⚠️ 全程**不碰真实数据目录**：第一件事就是把 `C.CONFIG_FILE` 指到临时文件，
   末尾核对真实 `config.json` 的 md5 没变（手法同 `school_profile_test.py`）。
⚠️ **不发任何网络请求。**

> REF_JS 与工作区 `_lab/srun_algo_check.py` 里的那份同源（都摘自
> `嘉兴大学-Srun算法采集.md`）。改一处记得改两处。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  [%s] %-46s got=%r want=%r" % ("OK" if ok else "!!", name, got, want))


def check_true(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %-46s %s" % ("OK" if cond else "!!", name, detail))


def section(t):
    print("\n=== %s ===" % t)


def skip(t):
    SKIP.append(t)
    print("  [--] %s" % t)


def _md5(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except Exception:
        return None


# 真实配置路径 —— 只读看一眼，用来证明这个测试没动它。
try:
    import datasafe
    _REAL_DIR = datasafe.real_dir()
except Exception:
    _REAL_DIR = C.data_dir()
_REAL_CFG = os.path.join(_REAL_DIR, "config.json")
_REAL_BEFORE = _md5(_REAL_CFG)

# 🔴 **第一件事**：把配置指向临时目录。
TMP = tempfile.mkdtemp(prefix="sruntest_")
C.CONFIG_FILE = os.path.join(TMP, "config.json")


# --------------------------------------------------------------------------
# 门户的 JS —— 原样（含那几个 `a | b` 的混淆掩码）
# --------------------------------------------------------------------------
REF_JS = r"""
function s(a, b) {
  var c = a.length;
  var v = [];
  for (var i = 0; i < c; i += 4) {
    v[i >> 2] = a.charCodeAt(i) | a.charCodeAt(i + 1) << 8 | a.charCodeAt(i + 2) << 16 | a.charCodeAt(i + 3) << 24;
  }
  if (b) v[v.length] = c;
  return v;
}
function l(a, b) {
  var d = a.length;
  var c = d - 1 << 2;
  if (b) { var m = a[d - 1]; if (m < c - 3 || m > c) return null; c = m; }
  for (var i = 0; i < d; i++) {
    a[i] = String.fromCharCode(a[i] & 0xff, a[i] >>> 8 & 0xff, a[i] >>> 16 & 0xff, a[i] >>> 24 & 0xff);
  }
  return b ? a.join('').substring(0, c) : a.join('');
}
function encode(str, key) {
  if (str === '') return '';
  var v = s(str, true);
  var k = s(key, false);
  if (k.length < 4) k.length = 4;
  var n = v.length - 1, z = v[n], y = v[0],
      c = 0x86014019 | 0x183639A0, m, e, p,
      q = Math.floor(6 + 52 / (n + 1)), d = 0;
  while (0 < q--) {
    d = d + c & (0x8CE0D9BF | 0x731F2640);
    e = d >>> 2 & 3;
    for (p = 0; p < n; p++) {
      y = v[p + 1];
      m = z >>> 5 ^ y << 2;
      m += y >>> 3 ^ z << 4 ^ (d ^ y);
      m += k[p & 3 ^ e] ^ z;
      z = v[p] = v[p] + m & (0xEFB8D130 | 0x10472ECF);
    }
    y = v[0];
    m = z >>> 5 ^ y << 2;
    m += y >>> 3 ^ z << 4 ^ (d ^ y);
    m += k[p & 3 ^ e] ^ z;
    z = v[n] = v[n] + m & (0xBB390742 | 0x44C6F8BD);
  }
  return l(v, false);
}
var _PADCHAR = "=", _ALPHA = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA";
function _getbyte(s, i) { var x = s.charCodeAt(i); if (x > 255) { throw "INVALID"; } return x; }
function b64encode(s) {
  s = String(s);
  var i, b10, x = [], imax = s.length - s.length % 3;
  if (s.length === 0) { return s; }
  for (i = 0; i < imax; i += 3) {
    b10 = (_getbyte(s, i) << 16) | (_getbyte(s, i + 1) << 8) | _getbyte(s, i + 2);
    x.push(_ALPHA.charAt(b10 >> 18));
    x.push(_ALPHA.charAt((b10 >> 12) & 63));
    x.push(_ALPHA.charAt((b10 >> 6) & 63));
    x.push(_ALPHA.charAt(b10 & 63));
  }
  switch (s.length - imax) {
    case 1:
      b10 = _getbyte(s, i) << 16;
      x.push(_ALPHA.charAt(b10 >> 18) + _ALPHA.charAt((b10 >> 12) & 63) + _PADCHAR + _PADCHAR);
      break;
    case 2:
      b10 = (_getbyte(s, i) << 16) | (_getbyte(s, i + 1) << 8);
      x.push(_ALPHA.charAt(b10 >> 18) + _ALPHA.charAt((b10 >> 12) & 63) + _ALPHA.charAt((b10 >> 6) & 63) + _PADCHAR);
      break;
  }
  return x.join("");
}
var CASES = JSON.parse(process.argv[2]);
var out = [];
for (var ci = 0; ci < CASES.length; ci++) {
  var c = CASES[ci];
  var payload = JSON.stringify({
    username: c.username, password: c.password, ip: c.ip,
    acid: c.acid, enc_ver: c.enc_ver
  });
  var enc = encode(payload, c.token);
  out.push({ payload: payload, encoded: enc, b64: b64encode(enc) });
}
console.log(JSON.stringify(out));
"""


def find_node():
    """找 Node：环境变量 `NODE` > PATH > WorkBuddy 托管运行时。

    🔴 **故意不写死本机绝对路径** —— `repo/` 里的文件要能进 git，而
       `secret_scan.py` 会把本机路径判成问题。**这个拦截是对的**：
       2026-10-12 第一次跑 fast 档就被它拦下来了（`srun_test.py` 那行
       原本硬编码了作者机器上的 Node 路径）。
       所以这里用 `~` 展开，并且**按目录扫**而不是写死版本号。

    找不到 Node 时，本测试**跳过对拍**，不判失败。
    """
    for cand in (os.environ.get("NODE"), shutil.which("node"),
                 shutil.which("node.exe")):
        if cand and os.path.isfile(cand):
            return cand
    base = os.path.join(os.path.expanduser("~"), ".workbuddy-ai",
                        "binaries", "node", "versions")
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, name, "node.exe")
            if os.path.isfile(exe):
                return exe
    return None


CASES = [
    {"username": "2023001", "password": "testpass123",
     "ip": "10.246.173.48", "acid": 1, "enc_ver": "srun_bx1",
     "token": "abc123"},
    {"username": "2023001", "password": "testpass123",
     "ip": "10.246.173.48", "acid": 1, "enc_ver": "srun_bx1",
     "token": "abcdef1234567890"},
    {"username": "s1234567890", "password": "P@ssw0rd!with-symbols",
     "ip": "10.246.1.2", "acid": 1, "enc_ver": "srun_bx1",
     "token": "0123456789abcdef0123456789abcdef01234567"},
    {"username": "u", "password": "p",
     "ip": "10.0.0.1", "acid": 1, "enc_ver": "srun_bx1", "token": "x"},
]


def py_side(case, alpha=None):
    """用 campus_login 里的真实实现算一遍。alpha 可替换字符表（阳性对照用）。"""
    old = C.SRUN_ALPHA
    if alpha is not None:
        C.SRUN_ALPHA = alpha
    try:
        payload = json.dumps({
            "username": case["username"], "password": case["password"],
            "ip": case["ip"], "acid": case["acid"],
            "enc_ver": case["enc_ver"],
        }, separators=(",", ":"), ensure_ascii=False)
        enc = C.srun_xencode(payload, case["token"])
        return payload, enc, C.srun_b64(enc)
    finally:
        C.SRUN_ALPHA = old


def run_node(js_path):
    p = subprocess.run([find_node(), js_path, json.dumps(CASES)],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        return None
    return json.loads(p.stdout)


def section_algo():
    section("1. 算法与门户 JS 对拍")
    node = find_node()
    if not node:
        skip("找不到 Node —— 跳过对拍（用 NODE=<路径> 指定，或装一个 Node）")
        return
    js_path = os.path.join(TMP, "_ref.js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(REF_JS)
    ref = run_node(js_path)
    if ref is None:
        skip("Node 执行参考 JS 失败 —— 跳过对拍")
        return

    for i, (case, r) in enumerate(zip(CASES, ref), 1):
        payload, enc, b64 = py_side(case)
        check("用例%d JSON 载荷" % i, payload, r["payload"])
        check("用例%d xEncode" % i, enc, r["encoded"])
        check("用例%d base64" % i, b64, r["b64"])

    # 🔴 阳性对照：把字符表改坏一个字符，对拍**必须**报不一致
    bad_alpha = C.SRUN_ALPHA[:-1] + ("A" if C.SRUN_ALPHA[-1] != "A" else "B")
    _, _, b64_bad = py_side(CASES[0], alpha=bad_alpha)
    check_true("阳性对照：改坏字符表后必须不一致",
               b64_bad != ref[0]["b64"], "（若一致说明对拍根本没在比）")


def section_pure():
    section("2. 纯函数边界")
    check("srun_b64('') == ''", C.srun_b64(""), "")
    check("srun_xencode('', key) == ''", C.srun_xencode("", "k"), "")
    # padding：长度 % 3 == 1 / 2 都要正确收尾
    for n in (1, 2, 3, 4, 5, 6):
        s = "".join(chr(65 + i) for i in range(n))
        got = C.srun_b64(s)
        check("b64 长度%d 的 padding" % n, got.endswith("=" * ((3 - n % 3) % 3)),
              True)
    # JSONP 剥壳
    check("_srun_jsonp 剥壳", C._srun_jsonp('x({"a":1,"b":"ok"})'), {"a": 1, "b": "ok"})
    check("_srun_jsonp 无壳也认", C._srun_jsonp('{"a":2}'), {"a": 2})


def section_school():
    section("3. 学校档案与配置优先级")
    zjxu = C.school_profile("zjxu")
    check("嘉兴档案 portalType", zjxu.get("portalType"), "srun")
    check("嘉兴档案 portalHost", zjxu.get("portalHost"), "http://wlan.zjxu.edu.cn")
    check("嘉兴档案 campusSubnets", zjxu.get("campusSubnets"), ["10.246."])
    check("认不出来的 key → 空 dict", C.school_profile("不存在"), {})
    check("None → 默认学校（郑航）", C.school_profile(None)["portalType"], "webauth")

    # --- load_config 的优先级 ---
    def write_cfg(d):
        with open(C.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)

    write_cfg({})
    check("不写 school → 郑航门户", C.load_config()["portalHost"],
          "http://202.196.169.166")

    write_cfg({"school": "zjxu"})
    cfg = C.load_config()
    check("写 school=zjxu → 嘉兴门户", cfg["portalHost"], "http://wlan.zjxu.edu.cn")
    check("写 school=zjxu → portalType", cfg["portalType"], "srun")
    check("写 school=zjxu → campusSubnets", cfg["campusSubnets"], ["10.246."])

    # 单项覆盖：选了学校之后仍能单独改一项（高级用户的路没被堵死）
    write_cfg({"school": "zjxu", "portalHost": "http://10.1.2.3"})
    check("单项覆盖压过学校档案", C.load_config()["portalHost"], "http://10.1.2.3")

    # 阳性对照：改回 zua，门户必须变回郑航
    write_cfg({"school": "zua"})
    check("阳性对照：改回 zua → 门户变回郑航", C.load_config()["portalHost"],
          "http://202.196.169.166")


def section_switch():
    """换学校必须**真的**换过去 —— 哪怕 config.json 里躺着老版本留下的化石键。

    🔴 背景（2026-10-12 实测复现，`_repro_school_switch.py`）：
        老版本的 `on_save` / `on_switch` / `set_active_account` 都是
        `save_config(load_config())`，把 DEFAULTS 的 4 个键固化进了文件。
        而学校档案的优先级**低于**「config.json 里的键」，于是这 4 个化石
        永远盖住档案 —— 「界面选了嘉兴，登录还打到郑航」，一声不吭。
        本机真实 `config.json` 里确实躺着这 4 个键，所以这是**已经在用户机器上
        发生过的**问题，不是理论风险。
    """
    section("3b. 换学校必须真的换过去（化石键回归）")

    def write_cfg(d):
        with open(C.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)

    # 照抄本机真实文件：4 个化石键 + 凭据。化石键**不是用户写的**，
    # 是老版本 load_config()→save_config() 顺手写下的。
    old_style = {"userId": "2507210230", "passwd": "fake-pw",
                 "portalHost": "http://202.196.169.166", "wlanAcIp": "10.0.1.10",
                 "wlanAcName": "ZZHK-ZAX-BRAS", "timeoutSec": 8}

    # --- 防线一：读的时候就不认化石键 ---
    # 就算有人手改 config.json 写成 school=zjxu（没走界面），也必须生效。
    write_cfg(dict(old_style, school="zjxu"))
    check("（读）化石键不许盖住学校档案：portalHost",
          C.load_config()["portalHost"], "http://wlan.zjxu.edu.cn")
    check("（读）化石键不许盖住学校档案：portalType",
          C.load_config()["portalType"], "srun")
    # 阳性对照：**不等于默认值**的单项覆盖仍然赢（高级用户的路没被堵死）
    write_cfg(dict(old_style, school="zjxu", portalHost="http://10.1.2.3"))
    check("（读）阳性对照：非默认值的覆盖仍然赢",
          C.load_config()["portalHost"], "http://10.1.2.3")

    # --- 防线二：写的时候把化石键物理清掉（文件本身也得是诚实的）---
    write_cfg(dict(old_style))
    got_key, dropped = C.set_school("zjxu")
    check("set_school 返回生效的 key", got_key, "zjxu")
    check("set_school 报告清掉了 4 个化石键", sorted(dropped),
          ["portalHost", "timeoutSec", "wlanAcIp", "wlanAcName"])
    raw = json.load(open(C.CONFIG_FILE, encoding="utf-8"))
    check("化石键真的从文件里没了",
          [k for k in ("portalHost", "wlanAcIp", "wlanAcName", "timeoutSec")
           if k in raw], [])
    check("school 写进去了", raw.get("school"), "zjxu")
    check("凭据没被动过", (raw.get("userId"), C.decrypt_secret(raw.get("passwd"))),
          ("2507210230", "fake-pw"))
    # ⚠️ 2026-10-15 改：密码现在是 DPAPI 密文，不能再拿原始文件比明文。
    #    上面那行用 decrypt_secret() 还原 —— 那才是「凭据没丢」的真实语义。
    #    顺手钉住「写盘时确实加密了」，免得哪天加密静默失效这条还是绿的。
    check("写盘时密码确实被加密了", C.is_encrypted(raw.get("passwd")), True)
    check("切完之后门户是嘉兴的", C.load_config()["portalHost"],
          "http://wlan.zjxu.edu.cn")

    # --- 用户**改过**的单项覆盖不许被当成化石误删 ---
    write_cfg(dict(old_style))
    C.update_config({"timeoutSec": 20})          # 用户手写的，值 ≠ 默认值
    C.set_school("zjxu")
    raw = json.load(open(C.CONFIG_FILE, encoding="utf-8"))
    check("手写的 timeoutSec=20 被保住（不是化石）", raw.get("timeoutSec"), 20)
    check("手写的 timeoutSec 仍然生效", C.load_config()["timeoutSec"], 20)
    check("同时 portalHost 已经换成嘉兴的", C.load_config()["portalHost"],
          "http://wlan.zjxu.edu.cn")

    # --- 写配置不许再把档案值固化回文件（否则坑会重新长出来）---
    write_cfg({"userId": "u1", "passwd": "p1"})
    C.update_config({"userId": "u2", "passwd": "p2"})
    check("update_config 只写点名的键，不注入档案值",
          sorted(json.load(open(C.CONFIG_FILE, encoding="utf-8"))),
          ["passwd", "userId"])

    # --- 访问器的兜底：认不出来必须安静退回郑航，不能让程序起不来 ---
    write_cfg({"school": "这所学校不存在"})
    check("未知 school key → school_key() 退回 zua", C.school_key(), "zua")
    check("未知 school key → portal_type() 退回 webauth",
          C.portal_type(), "webauth")
    write_cfg({})
    check("没写 school → school_key() 退回 zua", C.school_key(), "zua")
    write_cfg({"school": "ZJXU"})               # 大小写也得认
    check("school 大小写不敏感", C.school_key(), "zjxu")


def section_dispatch():
    section("4. portal_login 的分派")
    called = []
    real_srun, real_web = C.srun_login, C.webauth_login
    C.srun_login = lambda uid, pwd, cfg=None: (called.append("srun"), True)[1]
    C.webauth_login = lambda uid, pwd: (called.append("webauth"), True)[1]
    try:
        def write_cfg(d):
            with open(C.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f)

        for school, want in (("zjxu", "srun"), ("zua", "webauth")):
            called[:] = []
            write_cfg({"school": school})
            C.portal_login("u", "p")
            check("school=%s → 走 %s" % (school, want), called, [want])

        # 不写 school（老配置）→ 必须走 webauth（红线：郑航行为不能变）
        called[:] = []
        write_cfg({})
        C.portal_login("u", "p")
        check("不写 school（老配置）→ 走 webauth", called, ["webauth"])

        # portalType 直接写在 config 里（绕过 school）也要认
        called[:] = []
        write_cfg({"portalType": "srun"})
        C.portal_login("u", "p")
        check("直接写 portalType=srun 也认", called, ["srun"])
    finally:
        C.srun_login, C.webauth_login = real_srun, real_web


def section_dispatch_ops():
    """下线 / 密码校验的分派。

    ⚠️ 分派点都在**最里层**（`portal_logout` / `check_password`），
       而不是外层的 `do_logout()` —— 后者那套「锁 + 三态 + 重试 + 写结果」
       是两套协议通用的，不该被拆开。这个测试钉的就是「分派确实在最里层生效」。
    """
    section("4b. 下线 / 密码校验的分派")
    called = []
    real = (C.srun_logout, C.get_distoken, C.http_post)
    # srun 侧：直接记一笔就返回
    C.srun_logout = lambda cfg=None: (called.append("srun_logout"), True)[1]
    # webauth 侧：get_distoken 返回 None 会让 portal_logout 早退（拿不到 token）
    C.get_distoken = lambda: (called.append("webauth_get_distoken"), None)[1]
    C.http_post = lambda *a, **k: (called.append("webauth_http_post"), None)[1]

    def write_cfg(d):
        with open(C.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)

    try:
        for school, want in (("zjxu", "srun_logout"),
                             ("zua", "webauth_get_distoken")):
            called[:] = []
            write_cfg({"school": school})
            C.portal_logout()
            check("portal_logout school=%s → %s" % (school, want), called, [want])

        # 不写 school（老配置）→ 必须走 webauth（红线）
        called[:] = []
        write_cfg({})
        C.portal_logout()
        check("portal_logout 不写 school → webauth", called,
              ["webauth_get_distoken"])

        # Srun 没有独立的密码校验接口 ⇒ 返回 None 且**不发请求**
        called[:] = []
        write_cfg({"school": "zjxu"})
        check("check_password school=zjxu → None", C.check_password("u", "p"), None)
        check("check_password school=zjxu → 不发请求", called, [])

        # webauth 侧仍然照旧发请求（证明分派没把老路径掐断）
        called[:] = []
        write_cfg({"school": "zua"})
        C.check_password("u", "p")
        check("check_password school=zua → 走 http_post", called,
              ["webauth_http_post"])
    finally:
        C.srun_logout, C.get_distoken, C.http_post = real


def main():
    print("=" * 72)
    print("深澜 Srun 适配测试")
    print("=" * 72)
    section_algo()
    section_pure()
    section_school()
    section_switch()
    section_dispatch()
    section_dispatch_ops()

    # 收尾：证明没动真实配置
    section("5. 数据安全")
    check("真实 config.json 未被改动", _md5(_REAL_CFG), _REAL_BEFORE)
    shutil.rmtree(TMP, ignore_errors=True)

    print()
    print("=" * 72)
    print("通过 %d，失败 %d，跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print("  · %s" % n)
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
