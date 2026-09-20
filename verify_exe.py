# -*- coding: utf-8 -*-
"""只验证桌面上已有的 exe，不重新打包。

用法：
    python verify_exe.py                  # 默认验桌面上那个（交付副本）
    python verify_exe.py <exe路径>        # 验指定的 exe

⚠️ 为什么要能指定路径：**新加的门槛必须能拿旧版本 exe 做阳性对照** ——
   否则「它通过了」证明不了它真的会拦人（可能只是条空转的检查）。
   例：拿 0.3.2 的 exe 跑，check_update_temp_dir 必须报失败。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 数据目录逻辑只留一份（在 build.py 里），否则这里会去旧位置找标记文件，
# 把「程序换了目录」误报成「构建失败」。
from build import _data_dir as data_dir  # noqa: E402
import proc_tree  # noqa: E402
import paths

EXE = sys.argv[1] if len(sys.argv) > 1 else paths.default_exe()


def _norm(p):
    return os.path.normcase(os.path.normpath(p or ""))


def check_update_temp_dir(data):
    """--selftest 报的「更新临时目录」必须**不是** exe 同目录。

    为什么单独一条：用户 2026-09-20 反馈「检查更新并下载后，exe 那个文件夹
    残留 .old」—— 如果 exe 放在桌面，就是在污染桌面。根因正是临时文件被写到了
    exe 同目录。而 GUI 里那段路径计算是个闭包，脚本点不到，**唯一**能断言的
    通道就是 --selftest 报出来的这一行。

    ⚠️ 这条自带阳性对照：0.3.2 及之前的 exe 里根本没有「更新临时目录」这一行，
       拿老 exe 跑必然失败 —— 说明这个检查确实在看新东西，不是空转。
    """
    sel = os.path.join(data, "selftest.txt")
    text = ""
    if os.path.isfile(sel):
        with open(sel, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    line = ""
    for ln in text.splitlines():
        if ln.startswith("更新临时目录:"):
            line = ln.split(":", 1)[1].strip()
            break
    exe_dir = os.path.dirname(EXE)

    if not line:
        print("更新临时目录 -> **标记里没有这一行**（老版本 exe？）")
        print("  期望: %s（数据目录）" % data)
        return 1

    if _norm(line) == _norm(data):
        print("更新临时目录 -> %s / OK（= 数据目录，不是 exe 同目录）" % line)
        return 0

    # 跨卷时会**故意**退回 exe 同目录（os.replace 跨卷会直接失败，见
    # updater.work_dir_for）。本机数据目录和 exe 都在 C:，正常不该走到这里。
    if _norm(line) == _norm(exe_dir):
        va = os.path.splitdrive(os.path.abspath(data))[0].lower()
        vb = os.path.splitdrive(os.path.abspath(EXE))[0].lower()
        if va != vb:
            print("更新临时目录 -> %s / OK（跨卷回退，数据目录在 %s、exe 在 %s）"
                  % (line, va or "?", vb or "?"))
            return 0
        print("更新临时目录 -> %s / **失败：它等于 exe 同目录（= 会污染桌面）**" % line)
        print("  期望: %s（数据目录）" % data)
        return 1

    print("更新临时目录 -> %s / **失败：既不是数据目录，也不是 exe 同目录**" % line)
    print("  期望: %s（数据目录）" % data)
    return 1


def main():
    if not os.path.isfile(EXE):
        print("找不到 exe:", EXE)
        return 1
    print("exe:", EXE, os.path.getsize(EXE), "字节",
          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(EXE))))

    data = data_dir()
    print("数据目录:", data, "存在" if os.path.isdir(data) else "不存在")
    started = time.time()
    bad = 0

    for flag, marker, want, must in (
            ("--selftest", "selftest.txt", "版本", True),
            ("--guitest", "guitest.txt", "GUI_OK", False)):
        t0 = time.time()
        # 别用 subprocess.run(timeout=...)：超时它只杀启动器父进程，
        # 子进程会带着窗口变成孤儿。run_exe 收整棵树。
        rc, timed_out = proc_tree.run_exe(EXE, [flag], timeout=60)
        if timed_out:
            print("%s -> **超时 60s 未退出**（%s）"
                  % (flag, "必须通过" if must else "环境相关"))
            if must:
                bad += 1
            continue
        elapsed = time.time() - t0
        f = os.path.join(data, marker)
        text, fresh = "", False
        if os.path.isfile(f):
            fresh = os.path.getmtime(f) >= started - 1
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        ok = fresh and (want in text)
        print("%s -> 退出码 %s / %.1fs / 标记 %s / %s"
              % (flag, rc, elapsed, "新" if fresh else "旧或缺失",
                 "OK" if ok else "失败"))
        if not ok:
            print("  标记内容:", text[:300].replace("\n", " | "))
            if must:
                bad += 1

    bad += check_update_temp_dir(data)

    print()
    print("结论:", "通过" if bad == 0 else "%d 项失败" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
