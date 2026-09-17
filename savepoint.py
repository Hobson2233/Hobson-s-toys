# -*- coding: utf-8 -*-
"""本地存档：改文件之前先打一个快照，搞砸了能整体回退。

为什么不直接 `git commit` 完事
    那会往正常历史里塞一堆「改到一半」的提交，把真正的历史淹掉，而且半成品状态
    进了历史也不好清理。这里用的是**另一套引用** `refs/savepoints/*`，指向用
    **临时索引**造出来的完整快照提交：

    - 不碰工作区、不碰真正的暂存区（`GIT_INDEX_FILE` 指向临时文件）
    - 不碰 HEAD、不影响 `git log`（快照不属于分支历史）
    - **不会被 `git push --tags` 带出去**（它不是 tag）—— 仓库要公开时这点很重要
    - 因为一直被引用着，所以永远不会被 gc 收走

    「快照 = 那一刻工作区的完整状态」，包含未提交的修改、新增文件、删除动作，
    但**遵守 .gitignore**（所以 `local_secrets.py`、`build*/`、`dist*/` 不会进快照）。

回退是可撤销的
    `restore` 之前会**自动再打一个快照**，所以「回退」这个动作本身也能再回退。
    回退时会删掉快照里没有的新文件 —— 它们同样已经躺在那个自动快照里，不会丢。

⚠️ 比较快照时必须拿**树**比，不能拿 `git diff <快照>`
    `git diff <commit>` 看不见未跟踪文件（它们不在索引里），于是会把「快照里有、
    索引里没有」的文件误报成「已删除」。本项目里 `savepoint.py` 自己就是未跟踪文件，
    第一版就把自己报成了 310 行删除。所有比较都改成 `diff-tree` 比两棵树。

⚠️ 本脚本**绝不调用** `git prune` / `git gc`。
   实测：在这个工作区里跑 `git prune`（任何形式）会把整个对象库清空，`git status`
   报 `fatal: bad object HEAD`，连初始提交都没了。原因见
   `.workbuddy-ai/memory/MEMORY.md` 第九节第一条。

用法：
    python savepoint.py save [标签]      打一个快照（改文件之前跑）
    python savepoint.py list             列出所有快照
    python savepoint.py show <快照>      这个快照相对上一个改了什么
    python savepoint.py diff [快照]      工作区相对快照有什么不同
    python savepoint.py restore <快照>   回退到该快照（回退前会自动再存档）
    python savepoint.py drop <快照>      删掉一个快照引用
    python savepoint.py tidy [N]         只保留最近 N 个快照（默认 20）
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REF_NS = "refs/savepoints/"
# 仓库根。先给一个初值，别留成「未定义」—— 踩过「模块级变量在赋值前被使用」
# 导致的 NameError。
ROOT = os.getcwd()


def run(args, env=None, cwd=None):
    """跑一条 git 命令，返回 (退出码, stdout, stderr)。"""
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(["git"] + args, cwd=cwd or ROOT, capture_output=True, env=e)
    return (r.returncode,
            r.stdout.decode("utf-8", "surrogateescape"),
            r.stderr.decode("utf-8", "surrogateescape"))


def git(args, env=None):
    rc, out, err = run(args, env=env)
    if rc != 0:
        raise RuntimeError("git %s 失败：\n%s" % (" ".join(args), err.strip()))
    return out


def repo_root():
    rc, out, err = run(["rev-parse", "--show-toplevel"], cwd=os.getcwd())
    if rc != 0:
        sys.exit("这里不是 git 仓库（先 git init）：\n%s" % err.strip())
    return out.strip()


def head_commit():
    rc, out, _ = run(["rev-parse", "--verify", "--quiet", "HEAD"])
    return out.strip() if rc == 0 and out.strip() else None


def safe_name(label):
    """把标签变成合法的 ref 名。ref 里不能有空格、~^:?*[\\ 和 '..'。"""
    s = re.sub(r"[\s~^:?*\[\\]+", "-", (label or "").strip())
    s = s.replace("..", ".").strip(".-/")
    return s[:40]


def worktree_tree():
    """把**当前工作区**写成一棵树，返回 tree sha。

    关键是用**临时索引**：真正的索引和 HEAD 一个字节都不动，
    所以「打快照」这个动作对仓库是只读的。
    从空索引开始（`read-tree --empty`），这样得到的树就是工作区本身，
    而不是「HEAD + 已暂存」的混合体。
    """
    tmpdir = tempfile.mkdtemp(prefix="savepoint-")
    idx = os.path.join(tmpdir, "index")
    env = {"GIT_INDEX_FILE": idx}
    try:
        git(["read-tree", "--empty"], env=env)
        git(["add", "-A"], env=env)
        return git(["write-tree"], env=env).strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def tree_paths(treeish):
    """某个树/提交里所有文件的路径集合。"""
    return set(p for p in git(["ls-tree", "-r", "--name-only", treeish]).splitlines() if p)


def snapshot(label=""):
    """打一个快照。返回 dict(name, sha, tree, parent, same_as_head)。"""
    tree = worktree_tree()
    parent = head_commit()
    same = False
    if parent:
        rc, out, _ = run(["rev-parse", parent + "^{tree}"])
        same = (rc == 0 and out.strip() == tree)

    msg = "savepoint: %s\n\n自动存档，不是正常提交。" % (label or "(无标签)")
    args = ["commit-tree", tree, "-m", msg]
    if parent:
        args += ["-p", parent]
    sha = git(args).strip()

    name = time.strftime("%Y-%m-%d_%H%M%S") + (
        ("-" + safe_name(label)) if safe_name(label) else "")
    git(["update-ref", REF_NS + name, sha])
    return {"name": name, "sha": sha, "tree": tree, "parent": parent, "same_as_head": same}


def all_savepoints():
    """返回 [(短名, sha, 日期, 标签)]，新的在前。"""
    rc, out, _ = run(["for-each-ref", "--sort=-refname",
                      "--format=%(refname)|%(objectname:short)|%(creatordate:short)|%(subject)",
                      REF_NS])
    if rc != 0:
        return []
    rows = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        parts = (ln.split("|", 3) + ["", "", ""])[:4]
        ref, sha, when, subj = parts
        name = ref[len(REF_NS):] if ref.startswith(REF_NS) else ref
        rows.append((name, sha, when, subj.replace("savepoint:", "").strip()))
    return rows


def resolve(ref):
    """允许写短名、完整名或唯一前缀。返回 (短名, sha)。"""
    rows = all_savepoints()
    if not rows:
        sys.exit("还没有任何快照，先跑：python savepoint.py save")
    exact = [r for r in rows if r[0] == ref]
    if exact:
        return exact[0][0], exact[0][1]
    pref = [r for r in rows if r[0].startswith(ref)]
    if len(pref) == 1:
        return pref[0][0], pref[0][1]
    if len(pref) > 1:
        sys.exit("前缀 %r 不唯一，匹配到 %d 个：%s"
                 % (ref, len(pref), ", ".join(r[0] for r in pref[:5])))
    sys.exit("找不到快照 %r。用 list 看看有哪些。" % ref)


def print_diff(a_tree, b_tree, limit=12):
    """打印两棵树之间的差异（含新增/删除文件）。"""
    rc, out, _ = run(["diff-tree", "-r", "--stat", a_tree, b_tree])
    lines = [l for l in out.strip().splitlines() if l.strip()]
    if not lines:
        print("  （没有差异）")
        return False
    for l in lines[:limit]:
        print("  " + l.strip())
    if len(lines) > limit:
        print("  ...还有 %d 行" % (len(lines) - limit))
    return True


def cmd_save(a):
    s = snapshot(a.label)
    print("已存档  %s  (%s)" % (s["name"], s["sha"][:8]))
    if s["same_as_head"]:
        print("  注意：内容和 HEAD 完全一样，没什么新东西可存。")
    else:
        print("  这份快照相对 HEAD 的差异：")
        print_diff(s["parent"] + "^{tree}" if s["parent"] else "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
                   s["tree"])
    print("  回退：python savepoint.py restore %s" % s["name"])
    return 0


def cmd_list(a):
    rows = all_savepoints()
    if not rows:
        print("还没有任何快照。改文件之前先跑：python savepoint.py save 标签")
        return 0
    print("%-32s %-9s %-11s %s" % ("快照", "提交", "日期", "标签"))
    for name, sha, when, label in rows:
        print("%-32s %-9s %-11s %s" % (name, sha, when, label or "(无标签)"))
    print()
    print("共 %d 个。回退：python savepoint.py restore <快照名>" % len(rows))
    return 0


def cmd_show(a):
    name, sha = resolve(a.ref)
    print("=== %s (%s) ===" % (name, sha))
    rc, out, _ = run(["log", "-1", "--format=%ci  %s", sha])
    print(out.strip())
    print()
    print("相对上一个提交的差异：")
    print_diff(sha + "^", sha)
    return 0


def cmd_diff(a):
    if a.ref:
        name, sha = resolve(a.ref)
    else:
        rows = all_savepoints()
        if not rows:
            print("还没有任何快照。")
            return 0
        name, sha = rows[0][0], rows[0][1]
    print("工作区 相对 %s (%s) 的差异：" % (name, sha))
    print_diff(sha, worktree_tree())
    return 0


def cmd_restore(a):
    name, sha = resolve(a.ref)

    # 回退前先自动存档 —— 让「回退」这个动作本身也可撤销。
    # 标签固定用 `undo`，**不要**把目标快照名拼进去：那样撤销两次名字就会
    # 递归嵌套成长达上百字符的怪物（实测过）。
    undo = snapshot("undo")
    print("先把当前状态存了一份：%s (%s)" % (undo["name"], undo["sha"][:8]))

    # read-tree -u --reset 把索引和工作区都对齐到快照的树，并删掉
    # 「当前索引里有、快照里没有」的文件。
    git(["read-tree", "-u", "--reset", sha])

    # 但它把**真正的索引**也改成了快照的样子 —— 于是 `git status` 会显示一堆
    # 「已暂存」的改动，看着像有人替你 add 了一堆东西，一不小心就把快照内容提交了。
    # 这里把索引退回 HEAD，让「回退」表现为普通的「工作区被改回去了」。
    if head_commit():
        git(["reset", "-q"])
    else:
        git(["read-tree", "--empty"])

    # 但没进过索引的新文件它不管。判据必须是「**快照里有没有这个文件**」，
    # 不能是「索引里有没有」—— 否则会把快照里明明存在、只是从未 add 过的文件
    # （比如 savepoint.py 自己）一并删掉。
    keep = tree_paths(sha)
    rc, out, _ = run(["ls-files", "--others", "--exclude-standard"])
    removed = []
    for p in out.splitlines():
        if not p.strip() or p in keep:
            continue
        full = os.path.join(ROOT, p)
        try:
            if os.path.isfile(full) or os.path.islink(full):
                os.remove(full)
                removed.append(p)
        except OSError:
            pass

    print("已回退到：%s" % name)
    print()
    print("这次回退实际改动了什么（%s -> 现在）：" % undo["name"])
    print_diff(undo["tree"], worktree_tree())
    if removed:
        print()
        print("另外删掉了 %d 个「快照里没有」的新文件（都在 %s 里，能找回来）："
              % (len(removed), undo["name"]))
        for p in removed[:10]:
            print("  " + p)
        if len(removed) > 10:
            print("  ...还有 %d 个" % (len(removed) - 10))
    print()
    print("反悔的话：python savepoint.py restore %s" % undo["name"])
    return 0


def cmd_drop(a):
    name, sha = resolve(a.ref)
    git(["update-ref", "-d", REF_NS + name])
    print("已删除快照引用 %s（提交对象还在，下次 gc 时才可能被回收）" % name)
    return 0


def cmd_tidy(a):
    """只保留最近 N 个快照，其余的删掉引用。

    只动 `refs/savepoints/*`，**不碰任何对象** —— 更不会去调 prune/gc。
    """
    rows = all_savepoints()
    if len(rows) <= a.keep:
        print("只有 %d 个快照，不超过 %d，不用清理。" % (len(rows), a.keep))
        return 0
    old = rows[a.keep:]
    for name, _sha, _when, _lab in old:
        git(["update-ref", "-d", REF_NS + name])
    print("保留最近 %d 个，删掉 %d 个引用：" % (a.keep, len(old)))
    for name, _sha, _when, _lab in old:
        print("  " + name)
    print()
    print("只删了引用，提交对象仍在仓库里。本脚本不会调用 prune/gc。")
    return 0


def main():
    global ROOT
    ap = argparse.ArgumentParser(
        description="本地存档：改文件之前打个快照，搞砸了能回退")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("save", help="打一个快照")
    p.add_argument("label", nargs="?", default="", help="标签，比如「改密码框之前」")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("list", help="列出所有快照")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="看某个快照改了什么")
    p.add_argument("ref")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("diff", help="工作区相对快照的差异")
    p.add_argument("ref", nargs="?", default="")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("restore", help="回退到某个快照")
    p.add_argument("ref")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("drop", help="删掉一个快照引用")
    p.add_argument("ref")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("tidy", help="只保留最近 N 个快照（默认 20）")
    p.add_argument("keep", nargs="?", type=int, default=20)
    p.set_defaults(func=cmd_tidy)

    a = ap.parse_args()
    if not getattr(a, "func", None):
        ap.print_help()
        return 2

    ROOT = repo_root()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
