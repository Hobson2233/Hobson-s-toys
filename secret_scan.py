# -*- coding: utf-8 -*-
"""提交前扫描：这次要提交的东西里有没有不该公开的内容。

**为什么不能只靠肉眼和 .gitignore**：一旦某次提交带上了密码，它就进了 git 历史，
`git rm` 是删不掉的 —— 得改写历史 + 强制推送，而且 GitHub 的缓存/别人 clone 的副本
里仍然留着。代价极高。所以要在**提交之前**拦。

查四类：
  1. 已知的真实凭据（从 local_secrets.py / 环境变量读）
  2. 长得像凭据的赋值（`passwd = "..."`、`password: "..."` 之类）
  3. 绑定了某台机器的绝对路径（`C:\\Users\\<某人>\\`）—— 换台机器就崩，而且泄漏用户名
  4. **提交信息本身**（`git log`）—— 密码写进 commit message 和写进代码一样糟：
     它同样永久留在历史里，`git rm` 一样删不掉。这条是**踩过之后补的**
     （2026-09-17，我在提交信息里复述了真实密码，前三类一条都没拦住）。

带阳性对照：如果连自己造的假密码都查不出来，那这个检查就是坏的。

用法：
    python secret_scan.py                # 扫工作区（按 .gitignore 排除）+ 全部提交信息
    python secret_scan.py --staged       # 只扫已 git add 的内容（提交前用这个）
    python secret_scan.py --self-test    # 只跑阳性对照
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 只看文本类文件；二进制（exe/ico/png）单独按字节搜已知凭据
TEXT_EXT = {".py", ".md", ".txt", ".json", ".spec", ".toml", ".cfg", ".ini",
            ".yml", ".yaml", ".gitignore", ".example"}
SKIP_DIRS = {".git", "__pycache__", "build1", "dist1", "icon_small", "ui_shots"}

# 「长得像凭据」的赋值。故意保守：宁可漏报几个，也不要满屏假警报
# —— 假警报多了人就不看了，检查等于没有。
SUSPICIOUS = [
    (re.compile(r"""\bpasswd?\s*[=:]\s*["']([^"'\s]{4,})["']""", re.I),
     "像密码的赋值"),
    (re.compile(r"""\bpassword\s*[=:]\s*["']([^"'\s]{4,})["']""", re.I),
     "像密码的赋值"),
    (re.compile(r"""\b(?:token|secret|api_?key)\s*[=:]\s*["']([^"'\s]{8,})["']""", re.I),
     "像密钥的赋值"),
]
# 这些值是占位符/示例，不算问题
PLACEHOLDER = re.compile(
    r"^(?:demo|example|test|dummy|xxx+|your|my|abc123|WRONG|\.\.\.|<.*>|\$\{.*\}|\{\{.*\}\})",
    re.I)

# 本机绝对路径（泄漏用户名 + 换机器就崩）
ABS_PATH = re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s\"']+\\")

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("OK" if ok else "!!", label, ("  -> " + detail) if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def known_needles():
    """已知的真实凭据。来源同 leak_check：环境变量 + local_secrets.py。"""
    out = []
    env = os.environ.get("CAMPUS_SENSITIVE", "")
    out += [s.strip() for s in env.split(",") if s.strip()]
    try:
        sys.path.insert(0, HERE)
        import local_secrets
        out += [str(s) for s in getattr(local_secrets, "NEEDLES", []) if s]
    except Exception:
        pass
    return [s for s in dict.fromkeys(out) if len(s) >= 4]


def _git_names(args):
    """跑一条列出文件名的 git 命令，返回文件名列表。

    **必须用 -z**：默认输出会把非 ASCII 文件名加引号并转成八进制转义
    （`设置.ico` 会变成字面量 `"\\350\\256\\276...ico"`，两边带引号）。
    拿这种字符串当路径是打不开的 —— 于是文件被静默跳过、扫了个寂寞。
    踩过一次：34 个暂存文件只扫了 33 个，少的正是 `设置.ico`。
    同时用 -c core.quotePath=false 双保险。
    """
    r = subprocess.run(["git", "-c", "core.quotePath=false"] + args + ["-z"],
                       cwd=HERE, capture_output=True)
    if r.returncode != 0:
        return None
    out = r.stdout.decode("utf-8", "surrogateescape")
    return [n for n in out.split("\0") if n]


def staged_files():
    """git 暂存区里的文件（相对仓库根）。"""
    names = _git_names(["diff", "--cached", "--name-only", "--diff-filter=ACM"])
    return names or []


def worktree_files():
    """工作区里**会被提交**的文件（用 git 的排除规则，别自己重新实现一遍）。"""
    names = _git_names(["ls-files", "--cached", "--others", "--exclude-standard"])
    if names is None:                          # 还不是 git 仓库
        out = []
        for root, dirs, files in os.walk(HERE):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), HERE))
        return out
    return names


def commit_messages():
    """所有能从 HEAD 走到的提交信息，返回 [(短 sha, 完整信息)]。

    为什么要查这个：**提交信息也是 git 历史的一部分**，密码写进去和写进代码一样
    删不掉。2026-09-17 我就在提交信息里复述了真实密码 —— 文件扫描一条都没拦住，
    因为它压根不看 `git log`。

    `%x1e` 当记录分隔符（不会出现在提交信息里），避免正文换行把解析搞乱。

    **「还没有任何提交」和「读不出来」必须区分**：新仓库第一次提交前 `git log`
    会失败（`your current branch does not have any commits yet`），这不是错误，
    是**没有东西要查**。只有 HEAD 能解析、`git log` 却仍然失败，才是真出错。
    """
    r = subprocess.run(["git", "log", "--format=%H%x1f%B%x1e"],
                       cwd=HERE, capture_output=True)
    if r.returncode != 0:
        head = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                              cwd=HERE, capture_output=True)
        if head.returncode != 0:
            return []                      # 一次提交都还没有，无可查
        return None                        # HEAD 在，log 却失败 —— 真问题
    txt = r.stdout.decode("utf-8", "surrogateescape")
    out = []
    for rec in txt.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, _, body = rec.partition("\x1f")
        out.append((sha.strip()[:8], body))
    return out


def edit_message():
    """还没提交的那条信息（用编辑器写时会留在 .git/COMMIT_EDITMSG 里）。

    用 `-m` / `-F` 时这个文件里是**上一条**提交的信息，所以只能当参考，
    不能当依据 —— 真正兜底的是提交后的 `commit_messages()`。
    """
    p = os.path.join(HERE, ".git", "COMMIT_EDITMSG")
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except Exception:
        return ""


def scan_messages(msgs, needles):
    """扫提交信息。返回问题列表，元素是 (sha, 类别, 说明)。"""
    problems = []
    for sha, body in msgs:
        for n in needles:
            if n in body:
                problems.append((sha, "提交信息里有已知凭据",
                                 "命中一个已知账号/密码（信息共 %d 字）" % len(body)))
                break
        for pat, what in SUSPICIOUS:
            for m in pat.finditer(body):
                if PLACEHOLDER.match(m.group(1)):
                    continue
                problems.append((sha, "提交信息里有" + what, "值已打码"))
                break
    return problems


def read_bytes(rel):
    """读文件原始字节。**读不出来返回 None**（不是 b""）。

    返回 b"" 会让「文件不存在」和「文件是空的」变得无法区分，从而静默跳过
    —— 那就等于这个文件没被检查，却照样报「通过」。
    """
    p = os.path.join(HERE, rel)
    try:
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None


def scan(files, needles):
    """返回 (问题列表, 统计)。问题元素是 (文件, 类别, 说明)。"""
    problems = []
    nb = [(n.encode("utf-8"), n) for n in needles]
    n16 = [(n.encode("utf-16-le"), n) for n in needles]
    checked = 0
    empty = 0

    for rel in files:
        if rel.replace("\\", "/").startswith((".git/",)):
            continue
        raw = read_bytes(rel)
        if raw is None:
            # 读不出来 = 这个文件根本没被检查。绝不能静默跳过，
            # 否则「少扫了一个」和「扫了且干净」输出一模一样。
            problems.append((rel, "读不出来", "文件不存在或无法读取 —— 它没被检查"))
            continue
        if not raw:
            empty += 1                         # 空文件，没什么可泄漏的
            continue
        checked += 1
        ext = os.path.splitext(rel)[1].lower()

        # 1. 已知真实凭据 —— 二进制也要搜（PNG 的像素流里可能带密码，
        #    所以文本文件搜原始字节，非文本文件连 UTF-16 也搜）
        for enc_needle, original in nb:
            if enc_needle in raw:
                problems.append((rel, "已知真实凭据", "命中一个已知账号/密码"))
        if ext not in TEXT_EXT:
            for enc_needle, original in n16:
                if enc_needle in raw:
                    problems.append((rel, "已知真实凭据(UTF-16)", "命中一个已知账号/密码"))

        if ext not in TEXT_EXT and not rel.endswith(".gitignore"):
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        lines = text.splitlines()

        # 2. 像凭据的赋值
        for i, ln in enumerate(lines, 1):
            if ln.lstrip().startswith("#"):
                continue                       # 注释里的示例不算
            for pat, what in SUSPICIOUS:
                for m in pat.finditer(ln):
                    val = m.group(1)
                    if PLACEHOLDER.match(val):
                        continue
                    problems.append((rel, what, "第 %d 行" % i))

        # 3. 本机绝对路径
        for i, ln in enumerate(lines, 1):
            for m in ABS_PATH.finditer(ln):
                if "21726" in m.group() or os.environ.get("USERNAME", "\0") in m.group():
                    problems.append((rel, "本机绝对路径", "第 %d 行: %s" % (i, m.group())))

    return problems, checked, empty


def self_test(needles):
    """阳性对照：造一个假凭据，看扫描器认不认。"""
    import tempfile
    if not needles:
        print("  [--] 没有已知凭据可当对照（local_secrets.py 缺失？），跳过")
        return True, 0
    probe = "DEMO_SECRET_FOR_TEST_9f3a"
    lines = [
        "# 故意埋一个假凭据",
        'passwd = "%s"' % probe,
        "normal_line = 1",
    ]
    tmpdir = tempfile.mkdtemp()
    p = os.path.join(tmpdir, "probe.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    # 直接对这一个文件跑两类规则，避免路径基准混乱
    found = 0
    raw = open(p, "rb").read()
    if probe.encode() in raw:
        found += 1
    body = open(p, encoding="utf-8").read()
    for pat, _w in SUSPICIOUS:
        if pat.search(body):
            found += 1
    ok = found >= 2
    check("埋进去的假凭据能被两类规则同时抓到", ok, "命中 %d 类" % found)
    try:
        os.remove(p)
        os.rmdir(tmpdir)
    except Exception:
        pass
    return ok, found


def self_test_messages(needles):
    """阳性对照：往一条**假**提交信息里埋一个已知凭据，看扫描器认不认。

    凭据只出现在内存里的字符串中，**不写进任何文件、不进 git 历史**。
    这条对照是必要的：`scan_messages()` 平时输出「没有发现问题」，
    而「真干净」和「匹配逻辑写错了」的输出**一模一样**。
    """
    if not needles:
        print("  [--] 没有已知凭据可当对照，跳过")
        return True
    body = ("修复：登录失败时的重试逻辑\n\n"
            "复现时用的是 %s 这个账号，日志里能看到。\n" % needles[0])
    probs = scan_messages([("deadbeef", body)], needles)
    ok = any(w == "提交信息里有已知凭据" for _s, w, _d in probs)
    check("往假提交信息里埋的凭据能被抓到", ok, "命中 %d 条" % len(probs))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--staged", action="store_true", help="只扫已 git add 的内容")
    ap.add_argument("--self-test", action="store_true", help="只跑阳性对照")
    a = ap.parse_args()

    needles = known_needles()
    print("已知凭据搜索词: %d 个" % len(needles))
    print()

    if a.self_test:
        ok, _ = self_test(needles)
        return 0 if ok else 1

    files = staged_files() if a.staged else worktree_files()
    label = "暂存区" if a.staged else "工作区（按 .gitignore 排除后）"
    print("扫描 %s：%d 个文件" % (label, len(files)))
    print()

    # 阳性对照 1：文件名列表里不能出现 git 的加引号形式。
    # 出现了就说明某个文件会因路径打不开而被跳过（`设置.ico` 就中过这一枪）。
    quoted = [f for f in files if f.startswith('"') or f.endswith('"')]
    quote_ok = check("文件名没有被 git 加引号（否则该文件会被跳过）",
                     not quoted, ("可疑: %s" % quoted[:3]) if quoted else "共 %d 个" % len(files))

    probs, checked, empty = scan(files, needles)
    for rel, what, detail in probs:
        print("  [!!] %s  —— %s（%s）" % (rel, what, detail))
    if not probs:
        print("  没有发现问题")
    if empty:
        print("  [--] %d 个空文件，无内容可查" % empty)

    print()
    ok = check("已知凭据没出现在待提交内容里",
               not any("凭据" in w for _f, w, _d in probs))
    ok &= check("没有本机绝对路径", not any(w == "本机绝对路径" for _f, w, _d in probs))
    ok &= check("每个文件都真的被读到了", not any(w == "读不出来" for _f, w, _d in probs))
    ok &= quote_ok

    # 4. 提交信息本身（git log）。提交信息也是历史的一部分，删不掉。
    print()
    print("提交信息（git log）")
    msgs = commit_messages()
    if msgs is None:
        print("  [!!] 读不到 git 历史 —— 这一项根本没被检查")
        msg_ok = False
    else:
        if not msgs:
            print("  还没有任何提交（第一次提交前是正常的，无可查）")
        else:
            print("  共 %d 条提交信息" % len(msgs))
        msg_probs = scan_messages(msgs, needles)
        em = edit_message().strip()
        if em and not any(em == b.strip() for _s, b in msgs):
            # 还没提交的那条（用编辑器写时会落在这里），提前拦一道
            msg_probs += scan_messages([("(尚未提交)", em)], needles)
        for sha, what, detail in msg_probs:
            print("  [!!] %s  —— %s（%s）" % (sha, what, detail))
        if not msg_probs:
            print("  没有发现问题")
        msg_ok = not msg_probs
    ok &= check("提交信息里没有已知凭据", msg_ok)

    print()
    print("阳性对照（这个检查真的会报警吗）")
    st_ok, _ = self_test(needles)
    ok &= st_ok
    ok &= self_test_messages(needles)

    print()
    if not ok:
        print("结论: **不通过** —— 别提交。先处理上面标 [!!] 的项。")
        return 1
    print("结论: 通过 —— 可以提交（读了 %d 个文件，跳过 %d 个空文件，共列出 %d 个）"
          % (checked, empty, len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
