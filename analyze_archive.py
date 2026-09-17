# -*- coding: utf-8 -*-
"""分析 exe 归档里到底什么东西占地方，按体积排序。

用法：
    python analyze_archive.py [exe]

回答「为什么我们的 exe 这么大」时看最后那段**按用途归类** —— 结论是
「自己的代码只占 1% 左右，剩下全是必须随程序走的运行环境」。
对标别的程序（比如 GHelper）时注意：**它们多半是依赖系统已装运行时的**
（C# 靠 .NET、Electron 靠系统浏览器内核等），体积不可直接比。
判断 .NET 程序是否自包含：搜 `coreclr`（自包含才有）和 `hostfxr`（apphost 有）。
"""
import os
import sys

import paths

EXE = sys.argv[1] if len(sys.argv) > 1 else \
    paths.default_exe()


def human(n):
    for u in ("B", "K", "M"):
        if n < 1024:
            return "%.1f%s" % (n, u)
        n /= 1024.0
    return "%.1fG" % n


def category(name):
    """把一个归档条目归到「用途」桶里。改分类规则时同步改这里就够了。"""
    n = name.lower().replace("\\", "/")
    if n.startswith("campus_login") or n.endswith(".ico"):
        return "A 我们的代码 + 图标"
    if n.startswith(("python3", "vcruntime", "ucrtbase", "libffi", "zlib1")):
        return "B CPython 解释器 + C 运行库"
    if n.startswith(("tcl", "tk", "_tcl_data", "_tk_data")) or n.endswith(".enc"):
        return "C Tcl/Tk 界面库（tkinter 的地基）"
    if n == "pyz.pyz" or n.startswith(("base_library", "pyimod")):
        return "D Python 标准库（含 PyInstaller 引导）"
    if n.endswith(".pyd"):
        return "E C 扩展模块（socket/ctypes/psutil…）"
    return "F 其他"


def main():
    from PyInstaller.archive.readers import CArchiveReader
    r = CArchiveReader(exe := EXE)
    print("exe: %s  %s" % (exe, human(os.path.getsize(exe))))
    print("归档条目数: %d" % len(r.toc))
    print()

    rows = []
    for name, entry in r.toc.items():
        # CArchiveReader toc: (dpos, dlen, ulen, flag, typecode, ...)
        try:
            clen, ulen = int(entry[1]), int(entry[2])
        except Exception:
            continue
        rows.append((ulen, clen, name))
    rows.sort(reverse=True)

    total_c = sum(x[1] for x in rows)
    total_u = sum(x[0] for x in rows)
    print("归档内合计: 压缩后 %s / 解压后 %s" % (human(total_c), human(total_u)))
    print()
    print("=== 解压后体积 TOP 30 ===")
    print("%10s %10s  %s" % ("解压", "压缩", "条目"))
    for u, c, n in rows[:30]:
        print("%10s %10s  %s" % (human(u), human(c), n))

    # 按顶层目录/包归类
    print()
    print("=== 按包归类（解压后体积 TOP 20） ===")
    agg = {}
    for u, c, n in rows:
        parts = n.replace("\\", "/").split("/")
        # 取第一个有意义的段
        key = parts[0]
        if key in ("", "."):
            key = parts[1] if len(parts) > 1 else "?"
        agg[key] = agg.get(key, 0) + u
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:20]:
        print("%10s  %s" % (human(v), k))

    # 按「用途」归类 —— 回答「为什么这么大」就看这段
    print()
    print("=== 按用途归类（这才是「体积为什么这样」的答案） ===")
    exe_size = os.path.getsize(exe)
    buckets = {}
    for u, c, n in rows:
        d = buckets.setdefault(category(n), [0, 0, 0])
        d[0] += 1
        d[1] += c
        d[2] += u
    print("%-36s %5s %9s %9s %7s" % ("分类", "条目", "压缩后", "解压后", "占exe"))
    for k, (cnt, c, u) in sorted(buckets.items()):
        print("%-36s %5d %9s %9s %6.1f%%"
              % (k, cnt, human(c), human(u), 100.0 * c / exe_size))
    print("-" * 70)
    print("%-36s %5d %9s %9s %6.1f%%"
          % ("归档内合计", sum(x[0] for x in buckets.values()),
             human(sum(x[1] for x in buckets.values())),
             human(sum(x[2] for x in buckets.values())),
             100.0 * sum(x[1] for x in buckets.values()) / exe_size))
    mine = buckets.get("A 我们的代码 + 图标", [0, 0, 0])[1]
    print("%-36s %5s %9s %9s %6.1f%%"
          % ("exe 文件本身", "", human(exe_size), "", 100.0))
    print()
    print(">>> 我们自己的代码占 exe 的 %.1f%%；其余 %.1f%% 是必须随程序发布的运行环境。"
          % (100.0 * mine / exe_size, 100.0 - 100.0 * mine / exe_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
