# -*- coding: utf-8 -*-
"""验证「裁掉 Tcl 的 tzdata/msgs/8.5」不会把界面搞坏。

做法：把系统的 tcl/tk 库目录复制两份，一份原样（对照组），一份按构建时
的规则裁掉（实验组），分别用 TCL_LIBRARY/TK_LIBRARY 指过去，再真的建一遍
界面。对照组必须过 —— 否则说明是测试本身有问题，不是裁剪有问题。

用法：
    python tcl_trim_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

SKIP_PARTS = ("/tzdata/", "/tcl8/8.5/", "/tcl8/8.4/", "/msgs/")


def find_tcl_dirs():
    """找当前解释器用的 tcl8.6 / tk8.6 目录。

    不用 tkinter.Tcl() 去问 —— 在受控会话里创建 Tcl 解释器本身就不稳。
    改用 PyInstaller 的解析器，构建时用的就是它，两边保持一致。
    """
    try:
        from PyInstaller.utils.hooks.tcl_tk import tcltk_info
        return tcltk_info.tcl_data_dir, tcltk_info.tk_data_dir
    except Exception as e:
        print("解析 tcl/tk 目录失败:", e)
        return None, None


def trim(src, dst):
    """按构建规则复制一份（剔除 SKIP_PARTS）"""
    n_skip = 0
    for root, dirs, files in os.walk(src):
        rel = "/" + os.path.relpath(root, src).replace("\\", "/").strip("/") + "/"
        if any(p in rel for p in SKIP_PARTS):
            n_skip += len(files)
            continue
        for f in files:
            r = "/" + os.path.relpath(os.path.join(root, f), src).replace("\\", "/")
            if any(p in r.lower() for p in SKIP_PARTS):
                n_skip += 1
                continue
            d = os.path.join(dst, os.path.relpath(root, src), f)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(os.path.join(root, f), d)
    return n_skip


def run_gui_with(tcl_dir, tk_dir, tag):
    env = dict(os.environ)
    if tcl_dir:
        env["TCL_LIBRARY"] = tcl_dir
    if tk_dir:
        env["TK_LIBRARY"] = tk_dir
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import campus_login as C;"
        "C.run_gui(smoke=True);"
        "print('GUI_OK')" % HERE
    )
    out = os.path.join(tempfile.gettempdir(), "tcltest_%s.txt" % tag)
    with open(out, "w", encoding="utf-8") as fh:
        try:
            subprocess.run([sys.executable, "-c", code], env=env,
                           stdout=fh, stderr=subprocess.STDOUT, timeout=60)
        except subprocess.TimeoutExpired:
            fh.write("\n[TIMEOUT]")
    # 注意：这里不要 os.remove(out)。超时被 kill 的子进程可能还攥着这个
    # 文件句柄，删它会抛 WinError 32。留着无所谓，反正每次覆盖写。
    txt = open(out, encoding="utf-8", errors="replace").read()
    return "GUI_OK" in txt, txt.strip().splitlines()[-3:]


def main():
    tcl_dir, tk_dir = find_tcl_dirs()
    print("系统 tcl_library:", tcl_dir)
    print("系统 tk_library :", tk_dir)
    if not (tcl_dir and tk_dir):
        print("找不到 tcl/tk 目录，无法测试")
        return 1

    tmp = tempfile.mkdtemp(prefix="tcltrim_")
    full_t, full_k = os.path.join(tmp, "full_tcl"), os.path.join(tmp, "full_tk")
    trim_t, trim_k = os.path.join(tmp, "trim_tcl"), os.path.join(tmp, "trim_tk")

    shutil.copytree(tcl_dir, full_t)
    shutil.copytree(tk_dir, full_k)
    n1 = trim(tcl_dir, trim_t)
    n2 = trim(tk_dir, trim_k)
    print("裁剪掉的文件数: tcl %d / tk %d" % (n1, n2))
    print()

    ok_full, tail_full = run_gui_with(full_t, full_k, "full")
    print("[对照组] 完整 tcl/tk ->", "GUI_OK" if ok_full else "失败", tail_full)
    ok_trim, tail_trim = run_gui_with(trim_t, trim_k, "trim")
    print("[实验组] 裁剪后 tcl/tk ->", "GUI_OK" if ok_trim else "失败", tail_trim)
    print()
    if not ok_full:
        print("对照组都没过 —— 是测试本身的问题，不能据此判断裁剪是否安全")
        print("临时目录保留:", tmp)
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    if ok_trim:
        print("结论: 裁剪后界面依然正常构建，tzdata/msgs/8.5 确实用不到 ✓")
        return 0
    print("结论: **裁剪把界面弄坏了** —— 不能裁")
    return 1


if __name__ == "__main__":
    sys.exit(main())
