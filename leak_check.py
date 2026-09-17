# -*- coding: utf-8 -*-
"""检查打包后的 exe 里到底装了什么，以及有没有夹带账号密码。

为什么不能直接 grep exe：
    PyInstaller 把 Python 字节码和数据文件都**压缩**进了 CArchive，
    直接对 exe 二进制 grep 是搜不到的 —— 实测连 "CampusLogin" 都是 0 次，
    说明那种检查**永远不会报警**，等于没查。必须把归档解开再搜。

阳性对照（用来证明这个检查真的能报警）：
    leak_check.py <exe> webauth.do CampusLogin
    这两个串确实在 campus_login 模块里，必须能搜到；搜不到就说明检查本身坏了。

用法：
    python leak_check.py [exe] [要搜的敏感串...]
    python -c "import leak_check; print(leak_check.scan(exe))"
"""
import os
import sys

import paths

APP_NAME = "CampusLogin"


def fallback_needles():
    """兜底搜索词：读不到本机数据时，还能拿这些已知串去搜。

    **不能把真实账号密码写在这里** —— 这个仓库是要公开的。
    来源按优先级：
      1. 环境变量 `CAMPUS_SENSITIVE`（逗号分隔）
      2. 本目录的 `local_secrets.py`（已在 .gitignore 里）的 `NEEDLES`
      3. 空列表 —— 此时只剩「从数据目录读」这一条路

    正常情况下第 3 条也够用：`sensitive_from_data_dir()` 会直接从
    `C:\\ProgramData\\CampusLogin\\accounts.json` 里把真账号密码读出来。
    兜底只是防「本机数据被清过」这种情况。
    """
    out = []
    env = os.environ.get("CAMPUS_SENSITIVE", "")
    out += [s.strip() for s in env.split(",") if s.strip()]
    try:
        import local_secrets
        out += [str(s) for s in getattr(local_secrets, "NEEDLES", []) if s]
    except Exception:
        pass
    # 去重且保序
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


SENSITIVE_FALLBACK = []          # 兼容旧引用；真实值改由 fallback_needles() 提供


def _mask_preview(needles, n=3):
    """打码预览：只留首字符和长度，例如 `2*******`。

    目的只是让人一眼看出「确实有搜索词、没退化成一串空值」，
    同时不把真值写进日志。
    """
    out = []
    for s in needles[:n]:
        s = str(s)
        out.append((s[0] + "*" * (len(s) - 1)) if s else "")
    if len(needles) > n:
        out.append("…共 %d 个" % len(needles))
    return "[%s]" % ", ".join(out)


def _data_dirs():
    """本机上可能存着账号密码的目录（新位置 + 老位置）。"""
    out = []
    try:
        import ctypes
        for csidl in (0x0023, 0x001A):          # COMMON_APPDATA, APPDATA
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                if buf.value:
                    out.append(os.path.join(buf.value, APP_NAME))
    except Exception:
        pass
    out.append(r"C:\tools")
    seen, uniq = set(), []
    for d in out:
        k = os.path.abspath(d).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def sensitive_from_data_dir():
    """从本机各数据目录里读出真实的账号密码，作为搜索词。"""
    import json
    out = []
    for d in _data_dirs():
        for fn in ("accounts.json", "config.json"):
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    s = json.load(f)
            except Exception:
                continue
            if fn == "accounts.json":
                for uid, a in (s.get("accounts") or {}).items():
                    out.append(uid)
                    if isinstance(a, dict) and a.get("passwd"):
                        out.append(a["passwd"])
            else:
                if s.get("userId"):
                    out.append(s["userId"])
                if s.get("passwd"):
                    out.append(s["passwd"])
    return out


def iter_archive_blobs(exe):
    """依次产出 (条目名, 解压后的字节)，覆盖 CArchive 顶层和嵌套的 PYZ。

    为什么顶层不够：主脚本 campus_login 是顶层条目，而且存的是 marshal 字节码 ——
    marshal 把字符串常量按明文 UTF-8 存，所以直接搜得到，我们自己的代码/文案都在
    这里。但标准库等模块在 PYZ 里，属于第二层，不解就查不到。两层都搜才叫「查过」。

    PYZ 为什么必须落成临时文件：CArchiveReader.toc 给的是**压缩后**数据的偏移，
    拿它去喂 ZlibArchiveReader 会报 "PYZ magic pattern mismatch!"。得先 extract()
    拿到解压后的 PYZ（头是 b"PYZ\\x00"），写成文件再解析。

    两层 API 返回值不一样，这里最容易踩空：CArchiveReader.extract() 给的是 bytes，
    而 ZlibArchiveReader.extract() 给的是**反序列化好的 code 对象** —— 对它调
    bytes() 会抛 TypeError，要是被 except 吞掉，就会「看起来查过了，其实一条没查」。
    所以 code 对象要 marshal.dumps() 回字节再搜。
    """
    import marshal
    import tempfile
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    r = CArchiveReader(exe)
    pyz_raw = []
    for name, _ent in r.toc.items():
        try:
            data = r.extract(name)
        except Exception:
            continue
        if not isinstance(data, (bytes, bytearray)):
            continue
        data = bytes(data)
        yield name, data
        if name.lower().endswith(".pyz"):
            pyz_raw.append((name, data))

    for name, raw in pyz_raw:
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".pyz")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            z = ZlibArchiveReader(tmp)
            for m in z.toc:
                try:
                    obj = z.extract(m)
                except Exception:
                    continue
                if isinstance(obj, (bytes, bytearray)):
                    yield "%s:%s" % (name, m), bytes(obj)
                elif obj is not None:
                    try:
                        yield "%s:%s" % (name, m), marshal.dumps(obj)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if tmp and os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


SUSPECT_KEYS = ("accounts", "config.json", "campus-login.log",
                "last-result", "selftest", "guitest")


def self_test(exe):
    """阳性对照：证明这个检查**真的能报警**。

    为什么必须有：这个脚本只输出「通过」，而「搜不到」既可能是真干净，也可能是
    检查本身坏了（归档没解开、API 换了返回类型、搜索词编码写错…）。这两种情况
    在输出上完全一样。所以先拿几个**一定在包里**的串去搜，搜不到就说明检查坏了，
    此时那个「通过」毫无意义 —— 宁可判失败。

    历史教训：ZlibArchiveReader.extract() 返回的是 code 对象而不是 bytes，
    对它调 bytes() 抛 TypeError 被 except 吞掉，检查就静默退化成「只查了顶层」
    却照样报「通过」。

    返回 (ok, 报告行列表)。
    """
    must_hit = [
        ("webauth.do", "portal 认证地址，一定在 campus_login 模块里"),
        ("CampusLogin", "程序名 / 数据目录名"),
    ]
    blob = b""
    for _name, data in iter_archive_blobs(exe):
        blob += data
    lines = ["阳性对照（搜不到就说明检查本身坏了，不是「干净」）:"]
    bad = 0
    for s, note in must_hit:
        got = s.encode("utf-8") in blob
        if not got:
            bad += 1
        lines.append("  [%s] %-16s 命中=%-5s  %s"
                     % ("OK " if got else "BAD", s, got, note))
    if not blob:
        bad += 1
        lines.append("  [BAD] 归档一个字节都没解出来")
    lines.append("阳性对照: %s" % ("正常 —— 检查具备报警能力" if bad == 0
                                  else "**失效** —— 上面的「通过」不可信"))
    return bad == 0, lines


def scan(exe, needles=None):
    """解开 exe 的 PyInstaller 归档，检查有没有夹带敏感串。

    返回 (ok, 报告行列表)。

    只走一遍 iter_archive_blobs：条目名和内容都要，分两遍就得解压两次，
    而且容易一遍查了 PYZ、另一遍没查，对不上。
    """
    lines = []
    needles = [n for n in dict.fromkeys(needles or sensitive_from_data_dir()
                                        or fallback_needles()) if n]
    lines.append("exe: %s  %s 字节" % (exe, os.path.getsize(exe)))
    # **不要把搜索词原样打出来** —— 那是真实账号密码，构建日志会被看到/被贴出来。
    # 只报个数和一个打码预览，够确认「确实搜了东西」就行。
    lines.append("搜索词: %d 个 %s" % (len(needles), _mask_preview(needles)))

    hits, scanned, suspect, top = [], 0, [], 0
    for n, data in iter_archive_blobs(exe):
        scanned += 1
        if ":" not in n:                     # 顶层条目（PYZ 内的写成 "PYZ.pyz:模块名"）
            top += 1
        if any(k in os.path.basename(n).lower() for k in SUSPECT_KEYS):
            suspect.append(n)
        for nd in needles:
            if nd.encode("utf-8") in data or nd.encode("utf-16-le") in data:
                hits.append((n, nd))

    lines.append("归档内顶层条目数: %d" % top)
    lines.append("已解压检查条目数: %d（含 PYZ 内模块）" % scanned)
    lines.append("疑似用户数据条目: %s" % (suspect or "无"))

    ok = not suspect and not hits
    if suspect:
        lines.append("!! 归档里夹带了用户数据文件: %s" % suspect)
    if hits:
        lines.append("!! 发现敏感信息:")
        for n, nd in hits:
            # 同样打码：报告本身也不能成为新的泄漏源（这个文件会被贴到 issue 里）
            lines.append("     %s  ->  %s" % (n, _mask_preview([nd], 1)))
    lines.append("结论: %s" % ("通过 —— 解压后的内容里没有账号/密码，也没有用户数据文件"
                              if ok else "**不通过** —— exe 夹带了隐私数据"))
    return ok, lines


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else \
        paths.default_exe()
    # 先证明检查能报警，再看它报了什么
    ok_st, lines_st = self_test(exe)
    for ln in lines_st:
        print(ln)
    print()
    ok, lines = scan(exe, sys.argv[2:] or None)
    for ln in lines:
        print(ln)
    return 0 if (ok and ok_st) else 1


if __name__ == "__main__":
    sys.exit(main())
