# -*- coding: utf-8 -*-
"""可移植性检查：这个 exe 拷到**没装任何开发环境的 Windows** 上能不能跑。

回答的问题：用户说「我把 exe 发到别的 Win11 电脑上就能用吗」。
不能靠「应该能行」——要真的查外部依赖。

检查三件事：
  1. exe 的 PE 导入表（启动器本身依赖哪些 DLL）—— 必须全是 Windows 自带的
  2. 归档里**打包进来的** DLL —— 凡是运行时需要的都得在里面
  3. 归档里有没有**绝对路径**（`C:\\Users\\<某人>\\...`）—— 有就是换台机器会崩

用法：
    python portability_check.py [exe]
"""
import os
import re
import sys

import paths

EXE = sys.argv[1] if len(sys.argv) > 1 else \
    paths.default_exe()

# Windows 从 XP 起就有的核心系统 DLL —— 任何 Win10/11 上必然存在，
# 不需要随程序发布，也不需要装任何运行库。
CORE_SYSTEM_DLLS = {
    "kernel32.dll", "user32.dll", "advapi32.dll", "shell32.dll", "shlwapi.dll",
    "ole32.dll", "oleaut32.dll", "gdi32.dll", "gdi32full.dll", "msvcrt.dll",
    "ntdll.dll", "comdlg32.dll", "comctl32.dll", "ws2_32.dll", "winmm.dll",
    "imm32.dll", "version.dll", "netapi32.dll", "userenv.dll", "psapi.dll",
    "rpcrt4.dll", "crypt32.dll", "secur32.dll", "bcrypt.dll", "dwmapi.dll",
    "uxtheme.dll", "winspool.drv", "mpr.dll", "setupapi.dll", "iphlpapi.dll",
    "dnsapi.dll", "wtsapi32.dll", "powrprof.dll", "sechost.dll", "kernelbase.dll",
    "msimg32.dll", "opengl32.dll", "wininet.dll", "urlmon.dll", "wldap32.dll",
    "normaliz.dll", "mswsock.dll", "authz.dll", "cfgmgr32.dll", "ncrypt.dll",
    "propsys.dll", "win32u.dll", "ucrtbase.dll",
}

# `api-ms-win-*` 是 API Set 虚拟 DLL，**从 Win10 起就是系统自带**（UCRT 随 OS 发布）。
# 不列进来会误报 —— 阳性对照时 `python.exe` 的那 5 个 api-ms-win-crt-* 就是这么冒出来的。
CORE_SYSTEM_PREFIXES = ("api-ms-win-",)

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("OK" if ok else "!!", label, ("  -> " + detail) if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def pe_imports(path):
    """exe 启动器自身静态导入的 DLL 名（小写）。"""
    import pefile
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    out = set()
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        out.add(entry.dll.decode("ascii", "replace").lower())
    pe.close()
    return out


def is_pyinstaller(path):
    """是不是 PyInstaller 归档。用 CArchive 的魔数判断，别靠文件名猜。"""
    try:
        from PyInstaller.archive.readers import CArchiveReader
        CArchiveReader(path)
        return True
    except Exception:
        return False


def archive_dlls(path):
    """归档里打包进来的所有 .dll / .pyd / .so 文件名（小写）。"""
    from PyInstaller.archive.readers import CArchiveReader
    car = CArchiveReader(path)
    out = set()
    for name in car.toc:
        base = name.replace("\\", "/").split("/")[-1].lower()
        if base.endswith((".dll", ".pyd")):
            out.add(base)
    return out


def scan_abs_paths(path, limit=12):
    """在归档里找「某个人的机器上才有的绝对路径」。"""
    from PyInstaller.archive.readers import CArchiveReader
    car = CArchiveReader(path)
    pat = re.compile(
        rb"[A-Za-z]:\\+(?:Users|Documents and Settings)\\+[A-Za-z0-9_.\- ]{2,32}\\+")
    hits = {}
    for name in car.toc:
        try:
            data = car.extract(name)
        except Exception:
            continue
        if isinstance(data, (bytes, bytearray)):
            blob = bytes(data)
        else:
            continue
        for m in pat.finditer(blob):
            s = m.group().decode("utf-8", "replace")
            hits[s] = hits.get(s, 0) + 1
    return sorted(hits.items(), key=lambda kv: -kv[1])[:limit]


def main():
    return run(EXE)


def run(exe):
    """检查指定的 exe。返回 0 通过 / 非 0 不通过。

    **从 build.py 调用时要传刚构建出来的产物路径**，别用默认值 ——
    默认值是桌面交付副本，那个可能是上一版留下来的，查它等于没查。
    """
    global FAILS
    FAILS = []
    exe_size = os.path.getsize(exe)
    print("exe: %s  %d 字节" % (exe, exe_size))
    print()

    print("=== 1. 启动器静态导入的系统 DLL（换台电脑必须存在）===")
    imports = pe_imports(exe)
    missing_core = sorted(
        d for d in imports - CORE_SYSTEM_DLLS
        if not d.startswith(CORE_SYSTEM_PREFIXES))
    for d in sorted(imports):
        print("     %s%s" % (d, "" if (d in CORE_SYSTEM_DLLS
                                    or d.startswith(CORE_SYSTEM_PREFIXES))
                             else "   <-- 非核心，要留意"))
    print()
    check("导入的全是 Windows 自带的核心 DLL", not missing_core,
          ("多出来的: " + ", ".join(missing_core)) if missing_core else "")

    print()
    print("=== 2. 打包进来的运行库（决定要不要装 VC++ / .NET）===")
    if not is_pyinstaller(exe):
        # 不是 PyInstaller 产物 —— 后面两节都没法查，但**不能装作通过**。
        # 一个在非目标输入上「静默通过」的检查，和坏掉的检查长得一模一样。
        print("     **这不是 PyInstaller 归档** —— 第 2/3/4 节无法检查。")
        check("目标是 PyInstaller 单文件 exe", False, "换一个 exe 再跑，或确认打包方式变了")
        print()
        print("结论: **无法判定** —— 只完成了第 1 节，其余检查不适用")
        return 1
    dlls = archive_dlls(exe)
    for d in sorted(dlls):
        print("     %s" % d)
    print()
    has_vcruntime = any("vcruntime" in d for d in dlls)
    has_ucrt = any(d.startswith("ucrtbase") for d in dlls)
    check("VC++ 运行库已打包（vcruntime140）", has_vcruntime)
    check("UCRT 已打包（ucrtbase）", has_ucrt)
    check("CPython 解释器已打包（python314.dll）",
          any(d.startswith("python3") and d.endswith(".dll") for d in dlls))

    print()
    print("=== 3. 归档里有没有别人机器上才有的绝对路径 ===")
    hits = scan_abs_paths(exe)
    if hits:
        for s, n in hits:
            print("     %d 次  %s" % (n, s))
    else:
        print("     （无）")
    check("没有绑定到本机用户目录的绝对路径", not hits)

    print()
    print("=== 4. 数据目录：ProgramData 写不进去时会不会退回 ===")
    check_data_dir_fallback()

    print()
    if FAILS:
        print("结论: **不通过** —— %d 项有问题: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("结论: 通过 —— 运行库齐全，无外部依赖，可直接拷贝运行")
    return 0


def check_data_dir_fallback():
    """实测 `data_dir()` 的回退链。返回 True/False，失败记进 FAILS。

    **坑**：`data_dir()` 把结果缓存在模块级 `_DATA_DIR` 里，而且**模块导入时**
    （`CONFIG_FILE = os.path.join(data_dir(), ...)`）就已经填过一次了。
    不清缓存就测，测的是缓存、patch 根本不生效 —— 会得到「回退失效」的假警报。
    我第一版就栽在这，差点去改没坏的代码。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import campus_login as C
    except Exception as e:
        print("     （跳过：无法导入 campus_login —— %s）" % e)
        return True
    real = C._shell_folder

    # **检查不该改用户的机器**：`_writable()` 里是 `os.makedirs(d, exist_ok=True)`，
    # 所以「逼出回退」这一步会顺手把 `%APPDATA%\CampusLogin` 建出来（实测：
    # 2026-09-17 发现该目录又出现了，空的，时间戳正好是上一次构建）。
    # 这里记下它原本存不存在，跑完把**自己建出来的空目录**收掉。
    fallback = os.path.join(C.roaming_dir(), C.APP_NAME)
    fallback_existed = os.path.isdir(fallback)

    def probe(common):
        C._DATA_DIR.clear()
        C._shell_folder = (real if common is None
                           else (lambda csidl: common
                                 if csidl == C.CSIDL_COMMON_APPDATA else real(csidl)))
        return C.data_dir()

    normal = probe(None)
    # 指向一个**文件**：绝对不可能往里写东西。比造 ACL 干净。
    blocked = probe(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "notepad.exe"))
    nodrive = probe("Z:\\nope")
    C._DATA_DIR.clear()
    C._shell_folder = real

    # 收尾：只删「本来不存在 + 现在是空的」那个目录，绝不碰有内容的
    cleaned = "无需清理"
    if not fallback_existed and os.path.isdir(fallback):
        try:
            if not os.listdir(fallback):
                os.rmdir(fallback)
                cleaned = "已删除本次检查建出来的空目录"
            else:
                cleaned = "目录非空，保留不动"
        except Exception as e:
            cleaned = "清理失败: %s" % e

    print("     正常        -> %s" % normal)
    print("     不可写      -> %s" % blocked)
    print("     盘符不存在  -> %s" % nodrive)
    print("     回退目录    -> %s（%s）" % (fallback, cleaned))
    ok = True
    ok &= check("正常时用机器级 ProgramData", "ProgramData" in normal, normal)
    ok &= check("不可写时退回用户级目录",
                "AppData" in blocked and "ProgramData" not in blocked, blocked)
    ok &= check("盘符不存在也能退回",
                "AppData" in nodrive and "ProgramData" not in nodrive, nodrive)
    ok &= check("检查没有在用户机器上留下痕迹",
                fallback_existed or not os.path.isdir(fallback), cleaned)
    return ok


if __name__ == "__main__":
    sys.exit(main())
