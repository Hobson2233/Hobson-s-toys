# -*- coding: utf-8 -*-
"""把 campus_login.py / run_tests.py 的未提交改动拆成两份补丁（**按行拆**）。

为什么要拆：
    工作区里混着两批改动 —— ① 0.5.4（嘉兴大学 Srun 适配 + 换学校）；② 郑航
    「上网数据」（自助服务）。同一个文件里都有，所以要拆成两个提交。

🔴 为什么必须**按行**拆，不能按 hunk 拆（2026-10-12 踩过）：
    `import self_service`（自助服务）和 `WARN, WARN_SOFT`（0.5.4 换学校要用的）
    只隔了几行，被 git 并进了**同一个 hunk**。整块划给谁都不对：
    划给自助服务 ⇒ 0.5.4 那个提交里的 `campus_login.py` **引用了没 import 的
    `WARN_SOFT`**（跨校标签那行会 NameError）；划给 0.5.4 ⇒ 0.5.4 带上了自助服务。
    ⇒ 判据必须细到「这一**行**是干什么的」。

判据：一行里出现下面任何标记 → 归「上网数据」。以「连续的一串增删行」为一组。
      （纯上下文行不参与判定，也不单独成组。）

🔴 拆完必须验证：`HEAD 版本 + 补丁A + 补丁B` 要和**当前工作区的文件逐字节相同**。
   不验的话，「两份补丁各看各的都对、合起来少了一块」完全发现不了 ——
   而它的表现是「提交上去的 0.5.4 少了一段代码」，极难查。
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("TEMP", "/tmp")

MARKERS = ("self_service", "SelfService", "CAPTCHA_W", "CAPTCHA_H",
           "CAPTCHA_ZOOM", "SELF_LOGIN_MAX", "上网数据", "btn_self",
           "on_self_service", "selfwin_test", "self_probe")

HDR = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse(diff_text):
    lines = diff_text.splitlines(keepends=True)
    head, i = [], 0
    while i < len(lines) and not lines[i].startswith("@@"):
        head.append(lines[i])
        i += 1
    hunks, cur = [], None
    for ln in lines[i:]:
        if ln.startswith("@@"):
            cur = [ln]
            hunks.append(cur)
        elif cur is not None:
            cur.append(ln)
    return head, hunks


def walk(hunk):
    """把 hunk 拆成带行号的条目：[(prefix, text, old_ln, new_ln)]。

    old_ln/new_ln 是这一行**在各自文件里的行号**（上下文行两边都有）。
    """
    m = HDR.match(hunk[0])
    old, new = int(m.group(1)), int(m.group(3))
    out = []
    for ln in hunk[1:]:
        p, text = ln[0], ln[1:]
        if p == " ":
            out.append((" ", text, old, new))
            old += 1
            new += 1
        elif p == "-":
            out.append(("-", text, old, new))
            old += 1
        elif p == "+":
            out.append(("+", text, old, new))
            new += 1
        elif p == "\\":
            out.append(("\\", text, None, None))   # "No newline at end of file"
        else:
            out.append((" ", text, old, new))      # 空行兜底
    return out


def regroup(items):
    """把条目切成「一组连续的增删行 + 前后各 3 行上下文」。"""
    groups = []
    i = 0
    while i < len(items):
        if items[i][0] not in "+-":
            i += 1
            continue
        j = i
        while j < len(items) and items[j][0] in "+-":
            j += 1
        # `\ No newline` 跟着它前面那组
        k = j
        while k < len(items) and items[k][0] == "\\":
            k += 1
        before = [x for x in items[max(0, i - 3):i] if x[0] == " "]
        after = [x for x in items[k:k + 3] if x[0] == " "]
        groups.append(before + items[i:k] + after)
        i = k
    return groups


def emit(group, tag_trailer):
    """按条目重新算 hunk 头。"""
    old_lines = [x for x in group if x[0] in " -"]
    new_lines = [x for x in group if x[0] in " +"]
    old_start = old_lines[0][2] if old_lines else 0
    new_start = new_lines[0][3] if new_lines else 0
    header = "@@ -%d,%d +%d,%d @@%s\n" % (
        old_start, len(old_lines), new_start, len(new_lines), tag_trailer)
    body = []
    for p, text, _o, _n in group:
        body.append(p + text)
    return header, body


def split(path, tag):
    diff = subprocess.run(["git", "diff", "--", path], cwd=REPO,
                          capture_output=True, text=True, check=True).stdout
    if not diff.strip():
        print("%-20s 没有改动，跳过" % path)
        return None, None
    head, hunks = parse(diff)

    a, b = [], []          # a = 0.5.4 那批；b = 上网数据那批
    n_groups = 0
    for h in hunks:
        trailer = HDR.match(h[0]).group(5)
        for g in regroup(walk(h)):
            n_groups += 1
            text = "".join(t for _p, t, _o, _n in g)
            (b if any(mk in text for mk in MARKERS) else a).append(
                emit(g, trailer))

    def write(part, name):
        p = os.path.join(OUT, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.writelines(head)
            for hdr, body in part:
                f.write(hdr)
                f.writelines(body)
        return p

    pa, pb = write(a, "split_%s_054.patch" % tag), write(b, "split_%s_self.patch" % tag)
    print("%-20s %d 个 hunk → %d 组；0.5.4 用 %d 组 / 上网数据用 %d 组"
          % (path, len(hunks), n_groups, len(a), len(b)))
    return pa, pb


def verify(path, pa, pb):
    """HEAD 版本 + 补丁A + 补丁B 必须和当前文件逐字节相同。"""
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "split_verify")
    subprocess.run(["rm", "-rf", d])
    os.makedirs(d)
    base = subprocess.run(["git", "show", "HEAD:%s" % path], cwd=REPO,
                          capture_output=True, check=True).stdout
    with open(os.path.join(d, path), "wb") as f:
        f.write(base)
    for p in (pa, pb):
        # ⚠️ 必须显式 `-c core.autocrlf=false`：验证目录**不是** git 仓库，
        #    于是 `.gitattributes` 的 `* text=auto eol=lf` 不生效，而本机
        #    全局配置是 `core.autocrlf=true` —— `git apply` 会把每个 LF 写成 CRLF，
        #    逐字节比对必然失败（差值正好等于换行数，2026-10-12 踩过）。
        r = subprocess.run(["git", "-c", "core.autocrlf=false",
                            "-c", "core.eol=lf", "apply", "-p1", p], cwd=d,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  🔴 打补丁失败 %s:\n%s" % (os.path.basename(p), r.stderr))
            return False
    with open(os.path.join(d, path), "rb") as f:
        got = f.read()
    with open(os.path.join(REPO, path), "rb") as f:
        want = f.read()
    ok = got == want
    print("  验证 %-20s %s（%d 字节 vs %d 字节）"
          % (path, "✅ 逐字节相同" if ok else "🔴 不一样", len(got), len(want)))
    if not ok:
        n = min(len(got), len(want))
        for k in range(n):
            if got[k] != want[k]:
                print("     第一处不同在第 %d 字节（第 %d 行）"
                      % (k, got[:k].count(b"\n") + 1))
                print("       得到: %r" % got[max(0, k - 20):k + 30])
                print("       期望: %r" % want[max(0, k - 20):k + 30])
                break
        else:
            print("     前 %d 字节相同，长度不同" % n)
    return ok


def main():
    ok = True
    for path, tag in (("campus_login.py", "cl"), ("run_tests.py", "rt")):
        pa, pb = split(path, tag)
        if pa is None:
            continue
        ok = verify(path, pa, pb) and ok
    print()
    print("拆分%s" % ("全部通过 ✅" if ok else "有问题 🔴"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
