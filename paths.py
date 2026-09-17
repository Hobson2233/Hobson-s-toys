# -*- coding: utf-8 -*-
"""统一的路径解析 —— **不要在脚本里写死 `C:\\Users\\<某人>\\...`**。

写死绝对路径有三个毛病，每个都真栽过：
  1. 换台机器/改个目录名，所有脚本一起失效（实测：把 exe 从桌面文件夹挪到桌面
     根目录，10 个脚本全部指向不存在的路径）
  2. 把构建者的用户名写进了公开仓库
  3. 别人 clone 下来直接跑不起来，得先全文替换一遍路径

所以统一走这里。优先级：
  1. 调用方显式传的路径
  2. 环境变量 `CAMPUS_EXE`
  3. 本仓库 `dist1/`（本地刚构建的产物）
  4. 桌面的常见交付位置

找不到就**明确报错并列出试过的位置**，不要静默退回一个不存在的路径
—— 那样错误会在很远的地方才炸，且看不出原因。
"""
import os
import sys

APP_NAME = "校园网自动登录"
EXE_NAME = APP_NAME + ".exe"


def repo_dir():
    """本文件所在目录（= 仓库根目录）。"""
    return os.path.dirname(os.path.abspath(__file__))


def desktop_dir():
    return os.path.join(os.path.expanduser("~"), "Desktop")


def exe_candidates(explicit=None):
    """按优先级返回候选路径列表（含不存在的，方便报错时全列出来）。"""
    out = []
    if explicit:
        out.append(explicit)
    env = os.environ.get("CAMPUS_EXE")
    if env:
        out.append(env)
    out.append(os.path.join(repo_dir(), "dist1", EXE_NAME))
    # 交付位置：桌面根目录，以及以前用过的桌面子文件夹
    out.append(os.path.join(desktop_dir(), EXE_NAME))
    out.append(os.path.join(desktop_dir(), APP_NAME, EXE_NAME))
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def find_exe(explicit=None):
    """找到就返回绝对路径，找不到返回 None。"""
    for p in exe_candidates(explicit):
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def exe_or_die(explicit=None):
    """找不到就打印「试过哪些位置」并退出。

    退出码用 2，和「检查不通过(1)」区分开 —— 否则分不清是「工具没找到」
    还是「找到了但没通过」。
    """
    p = find_exe(explicit)
    if p:
        return p
    print("!! 找不到 %s" % EXE_NAME)
    print("   试过这些位置：")
    for c in exe_candidates(explicit):
        print("     %s" % c)
    print("   指定方式：命令行传路径，或设环境变量 CAMPUS_EXE=<exe 的完整路径>")
    sys.exit(2)


def default_exe():
    """给脚本的默认值用：找到就给路径，找不到给「首选候选」好让报错信息可读。"""
    return find_exe() or exe_candidates()[0]


if __name__ == "__main__":
    found = find_exe()
    print("仓库目录 :", repo_dir())
    print("桌面目录 :", desktop_dir())
    print()
    for c in exe_candidates():
        print("  [%s] %s" % ("有" if os.path.isfile(c) else "  ", c))
    print()
    print("找到的 exe:", found or "（没有）")
    sys.exit(0 if found else 2)
