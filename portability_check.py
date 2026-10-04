# -*- coding: utf-8 -*-
"""可移植性检查：这个 exe 拷到**没装任何开发环境的 Windows** 上能不能跑。

回答的问题：用户说「我把 exe 发到别的 Win11 电脑上就能用吗」。
不能靠「应该能行」——要真的查外部依赖。

检查四件事：
  1. exe 的 PE 导入表（启动器本身依赖哪些 DLL）—— 必须全是 Windows 自带的
  2. 归档里**打包进来的** DLL —— 凡是运行时需要的都得在里面
  3. 归档里有没有**绝对路径**（`C:\\Users\\<某人>\\...`）—— 有就是换台机器会崩
     · 顶层**数据文件** —— `scan_abs_paths()`（跳过 PE 二进制，理由见那里的说明）
     · PYZ 里的源码文件名 —— `scan_pyz_filenames()`（2026-10-03 加，原来这层没查）
  4. 数据目录在 ProgramData 写不进去时会不会退回用户目录
     （**这一条是环境感知的**：本环境用不了 ProgramData 时，正确的期望是「退回」，
       不是「用 ProgramData」。见 check_data_dir_fallback 的说明。）

用法：
    python portability_check.py [exe]
    python portability_check.py --selftest      # 只验门槛自己会不会拦人
"""
import os
import re
import sys
import contextlib

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


# 「某个人的机器上才有的绝对路径」长什么样。
#
# ⚠️ 提成模块级常量是为了**能被测试直接引用**（2026-10-03）。
#    测试里手抄一份正则看着更省事，但只要抄错一个反斜杠，就会得到一个
#    「永远通过」的假测试 —— 而假测试比没有测试更糟，它会给你虚假的信心。
ABS_USER_PATH = re.compile(
    rb"[A-Za-z]:\\+(?:Users|Documents and Settings)\\+[A-Za-z0-9_.\- ]{2,32}\\+")

# PE 二进制的扩展名。这些文件里的路径**不是我们写进去的** —— 见 scan_abs_paths。
BINARY_EXT = (".dll", ".pyd", ".exe")


def is_binary_entry(name):
    """这个归档条目是不是 PE 二进制。"""
    return str(name).lower().endswith(BINARY_EXT)


def scan_abs_paths(path, limit=12):
    """在归档**顶层的数据文件**里找「某个人的机器上才有的绝对路径」。

    ⚠️ 为什么**跳过 PE 二进制**（.dll/.pyd/.exe）（2026-10-03 改）：

        那些二进制里确实嵌着 `C:\\Users\\<某人>\\...`，但**全是上游构建机上的**，
        分两类：
          · PE 调试目录（CodeView）里的 PDB 路径 —— `RSDS` + GUID + `...\\_ctypes.pdb`
          · C 源码的 `__FILE__` 断言字符串 —— `...\\Python-3.12.14\\Modules\\_ssl.c`
        实测新 exe 共 41 处：python312.dll 32、libcrypto-3-x64.dll 6、几个 .pyd 各 1。

        它们**换一份 CPython 就全变**（官方构建机 → uv 的构建机），
        而 exe 的行为一个字节都没变。所以这不是「绑定了本机」，
        是「上游二进制自带了自己的来历」。旧版 exe 里同样有这类字符串，
        只是它的 CPython 来自另一条构建链、路径里恰好没有用户名而已。

        这条检查的**意图**是「换台机器会不会崩」。二进制里的调试/断言路径
        不会让任何东西崩；会崩的是数据文件里指向某人目录的绝对路径
        （比如默认配置被写成 `C:\\Users\\Bob\\config.ini`）。所以只查数据文件。

    我们自己的代码在 **PYZ**（压缩字节码）里，那层由 scan_pyz_filenames() 负责 ——
    它才是「我们的东西有没有泄漏路径」的答案。
    """
    from PyInstaller.archive.readers import CArchiveReader
    car = CArchiveReader(path)
    hits = {}
    for name in car.toc:
        nm = str(getattr(name, "name", None) or name)
        if is_binary_entry(nm):
            continue
        try:
            data = car.extract(name)
        except Exception:
            continue
        if not isinstance(data, (bytes, bytearray)):
            continue
        for m in ABS_USER_PATH.finditer(bytes(data)):
            s = m.group().decode("utf-8", "replace")
            hits[s] = hits.get(s, 0) + 1
    return sorted(hits.items(), key=lambda kv: -kv[1])[:limit]


def scan_pyz_filenames(path):
    """PYZ 里各模块的 `co_filename` 有没有带盘符的绝对路径。

    ⚠️ 为什么必须单独查这一层（2026-10-03 加）：
        原实现只遍历顶层条目，而纯 Python 模块都在 **PYZ**（压缩的 ZlibArchive）里，
        在那儿按字符串搜**什么也搜不到**。也就是说「源码路径有没有泄漏」这件事，
        这道门槛**从来没真正查过** —— 而它恰恰是最该查的：co_filename 里若是
        `C:\\Users\\<某人>\\...`，才是真的把构建者的目录结构随 exe 发了出去。

    实测（PyInstaller 6.22.3）：128 个模块的 co_filename 全被重写成相对路径
    （`tkinter\\__init__.py` 这种），带盘符的 0 个 —— 所以这条现在是干净的。

    返回 [(模块名, 文件名)]，空列表表示干净。
    """
    import os as _os
    import tempfile
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    car = CArchiveReader(path)
    pyz = None
    for nm in car.toc:
        if str(getattr(nm, "name", None) or nm).endswith(".pyz"):
            pyz = nm
            break
    if pyz is None:
        return []

    tmp = tempfile.NamedTemporaryFile(suffix=".pyz", delete=False)
    tmp.close()
    bad = []
    try:
        with open(tmp.name, "wb") as f:
            f.write(car.extract(pyz))
        z = ZlibArchiveReader(tmp.name)

        def walk(code, out):
            out.append(code.co_filename)
            for c in code.co_consts:
                if hasattr(c, "co_filename"):
                    walk(c, out)

        absdrive = re.compile(r"[A-Za-z]:\\")
        for key in list(z.toc):
            try:
                obj = z.extract(key)
            except Exception:
                continue
            if not hasattr(obj, "co_filename"):
                continue
            files = []
            walk(obj, files)
            hit = [f for f in files if absdrive.match(f)]
            if hit:
                bad.append((str(key), hit[0]))
    finally:
        # 临时文件清不掉就算了（Windows 上偶发占用）。用 suppress 而不是
        # `try/except: pass` —— 前者是**声明式**的「我知道、且有意忽略」，
        # 不会被误读成漏写的异常处理。
        with contextlib.suppress(OSError):
            _os.unlink(tmp.name)
    return bad


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

    # ⚠️ 这里**不再要求** UCRT 被打包（2026-10-03 改）。
    #
    #    改之前它要求 `ucrtbase.dll` 在包里 —— 但那和第 1 节自相矛盾：
    #    CORE_SYSTEM_DLLS 里就列着 ucrtbase.dll，理由正是「Win10/11 必然存在，
    #    不需要随程序发布」。既然系统自带，第 2 节凭什么还要求打包它？
    #
    #    新版 PyInstaller 也是这个立场（depend/dylib.py 原话）：
    #      "UCRT is now a system component in Windows 10 and later, managed by
    #       Windows Update"
    #      "Windows prefers system-installed version over the bundled one, anyway"（#6326）
    #    它因此把 ucrtbase / api-ms-win-* 放进了排除列表 —— 打包了也白增体积，
    #    Windows 会优先用系统那份。
    #
    #    改法：从「必须打包」降级成「只报事实」。**门槛的意图没丢** ——
    #    「用户不用自己装运行库」仍由上面 vcruntime140 那条把守（那才是用户机器上
    #    真正不一定有的）。代价是这条不再覆盖 Win7/8，所以把要求写进提示里，
    #    别让「也支持 Win7」变成一句想当然的话。
    print("     UCRT（ucrtbase / api-ms-win-*）随包分发: %s"
          % ("是" if has_ucrt else
             "否 —— Win10+ 系统自带，不影响运行；Win7/8 需另装 UCRT"))
    check("CPython 解释器已打包（python3xx.dll）",
          any(d.startswith("python3") and d.endswith(".dll") for d in dlls))

    print()
    print("=== 3. 归档里有没有别人机器上才有的绝对路径 ===")
    print("  -- 顶层数据文件（PE 二进制已跳过，理由见 scan_abs_paths）--")
    hits = scan_abs_paths(exe)
    if hits:
        for s, n in hits:
            print("     %d 次  %s" % (n, s))
    else:
        print("     （无）")
    check("顶层没有绑定本机用户目录的绝对路径", not hits)

    print("  -- PYZ 里的源码文件名 --")
    pyz_bad = scan_pyz_filenames(exe)
    if pyz_bad:
        for k, f in pyz_bad[:8]:
            print("     %-30s %s" % (k, f))
    else:
        print("     （无 —— 各模块的 co_filename 都是相对路径）")
    check("源码文件名没带本机绝对路径", not pyz_bad)

    print()
    print("=== 4. 数据目录：ProgramData 写不进去时会不会退回 ===")
    check_data_dir_fallback()

    print()
    if FAILS:
        print("结论: **不通过** —— %d 项有问题: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("结论: 通过 —— 运行库齐全，无外部依赖，可直接拷贝运行")
    return 0


def _roundtrip_ok(d):
    """这个目录在本环境能不能完成「建目录 → 写文件 → 删文件」的往返。

    ⚠️ **故意不复用 campus_login 的 `_writable()`**（2026-10-03）。
       用它就成了「拿被测对象证明被测对象」—— `_writable` 正是要检验的东西，
       它说「不行」的时候，我们没法判断是环境真的不行，还是它自己有 bug。
       这里独立写一遍，得到的是**环境**的答案，可以拿去和 `_writable` 的答案对照。

    返回 (能不能, 原因)。
    """
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".pc_roundtrip")
        with open(p, "w", encoding="utf-8") as f:
            f.write("1")
    except Exception as e:
        return False, "写不进: %s: %s" % (type(e).__name__, e)
    try:
        os.remove(p)
    except Exception as e:
        # ⚠️ 走到这里 = 「写得进、删不掉」。这**正是** `_writable()` 会判 False 的情形
        #    （实测：沙箱把删除重定向到回收站、回收站操作被中止 → OSError；
        #     另一次是直接 PermissionError）。
        #    这种环境下 d 里会留下一个 1 字节的 `.pc_roundtrip` —— 删不掉嘛。
        #    没法避免：**「删得掉吗」只能靠真的删一次来回答**。那个残留本身就是
        #    这个环境限制的证据，不是这段代码粗心。
        return False, "删不掉探测文件: %s: %s" % (type(e).__name__, e)
    return True, ""


def check_data_dir_fallback():
    """实测 `data_dir()` 的回退链。返回 True/False，失败记进 FAILS。

    **坑 1**：`data_dir()` 把结果缓存在模块级 `_DATA_DIR` 里，而且**模块导入时**
    （`CONFIG_FILE = os.path.join(data_dir(), ...)`）就已经填过一次了。
    不清缓存就测，测的是缓存、patch 根本不生效 —— 会得到「回退失效」的假警报。
    我第一版就栽在这，差点去改没坏的代码。

    **坑 2（2026-10-03 踩到，比坑 1 阴）**：这条门槛原来是写死的
    「正常时必须返回 ProgramData」，结果在**某些环境**里必然失败，而产物本身没问题。
    实测根因：`_writable()` 要求「写得进 **且** 删得掉」，而本机某些上下文里
    `C:\\ProgramData\\CampusLogin` 的 `os.remove` 会失败（沙箱把删除重定向到
    回收站，回收站操作被中止 → OSError）。于是 `_writable()` 返回 False，
    `data_dir()` **正确地**退回了 `%APPDATA%\\CampusLogin` —— 是门槛把
    「环境限制」误判成了「产物缺陷」。旁证：真实数据目录里留下过一个 1 字节的
    `.write_probe`，正是那次失败的 `os.remove` 没删掉的。

    所以改成**环境感知**的断言：
      · 本环境能往返  → 要求首选 ProgramData（此时选错就是真缺陷）
      · 本环境不能往返 → 要求**确实退回了**用户级目录（此时用 ProgramData 才是缺陷）
    再补一条**与环境无关的正向对照**（把 `_writable` 打桩成一律 True）来证明
    优先级逻辑本身是对的 —— 否则上面那个「环境不允许」的分支有可能是在掩盖
    「优先级逻辑坏了」，两者输出一模一样。
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

    # 先独立量一遍环境：**这一步不碰 campus_login**，所以它的答案可以当基准。
    common = real(C.CSIDL_COMMON_APPDATA) or os.environ.get("ProgramData") or ""
    common_dir = os.path.join(common, C.APP_NAME) if common else ""
    env_ok, env_why = (_roundtrip_ok(common_dir) if common_dir
                       else (False, "取不到 ProgramData 路径"))

    normal = probe(None)
    # 指向一个**文件**：绝对不可能往里写东西。比造 ACL 干净。
    blocked = probe(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "notepad.exe"))
    nodrive = probe("Z:\\nope")

    # 与环境无关的正向对照：优先级逻辑。
    # 打桩让「任何目录都可写」，那么 `data_dir()` 必须把 ProgramData 排在最前。
    # 这条在任何机器上都能跑，用来证明下面那个「跳过」不是因为逻辑坏了才跳的。
    #
    # ⚠️ 必须**先**把 `_shell_folder` 还原成真的（2026-10-03 第一版就栽在这）：
    #    上面 probe("Z:\\nope") 把 `_shell_folder` 打桩成了「永远返回 Z:\\nope」，
    #    不还原就直接跑对照，拿到的是 `Z:\nope\CampusLogin` —— 对照报「失败」，
    #    但坏的是这段测试代码，不是 data_dir()。
    #    写成显式还原而不是靠后面的 finally 顺序，免得以后有人调换顺序又踩一遍。
    C._shell_folder = real
    real_writable = C._writable
    try:
        C._writable = lambda d: True
        C._DATA_DIR.clear()
        forced = C.data_dir()
    finally:
        C._writable = real_writable
        C._DATA_DIR.clear()

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

    print("     本环境能否用 ProgramData -> %s（%s）"
          % ("能" if env_ok else "不能", env_why or common_dir))
    print("     正常        -> %s" % normal)
    print("     不可写      -> %s" % blocked)
    print("     盘符不存在  -> %s" % nodrive)
    print("     回退目录    -> %s（%s）" % (fallback, cleaned))
    ok = True

    if env_ok:
        ok &= check("正常时用机器级 ProgramData", "ProgramData" in normal, normal)
    else:
        # 本环境用不了 ProgramData —— 此时**退回用户级才是对的**。
        # 不写成「无条件通过」：这里要求的恰恰是「必须不在 ProgramData」，
        # 如果 data_dir() 硬要用一个用不了的目录，这条会失败。
        print("  [跳过] 正常时用机器级 ProgramData")
        print("         本环境 %s 无法完成「写+删」往返（%s）" % (common_dir, env_why))
        print("         —— 用不了就退回，是**正确行为**，不是缺陷；"
              "此项改判为「必须退回」。")
        ok &= check("环境用不了 ProgramData 时确实退回了用户级目录",
                    "AppData" in normal and "ProgramData" not in normal, normal)

    ok &= check("不可写时退回用户级目录",
                "AppData" in blocked and "ProgramData" not in blocked, blocked)
    ok &= check("盘符不存在也能退回",
                "AppData" in nodrive and "ProgramData" not in nodrive, nodrive)
    # 正向对照（与环境无关）：可写时首选顺序必须是 ProgramData 打头。
    ok &= check("优先级逻辑：可写时首选机器级 ProgramData",
                "ProgramData" in forced, forced)
    ok &= check("检查没有在用户机器上留下痕迹",
                fallback_existed or not os.path.isdir(fallback), cleaned)
    return ok


def self_test_data_dir(ck):
    """把 `data_dir()` 故意弄坏两个方向，验证第 4 节**都会报失败**。

    为什么不留成独立脚本（2026-10-03）：独立脚本得靠人记得跑。并进 --selftest 里，
    「这条门槛是不是空转」就在同一个地方回答了。

    两个方向：
      A. 永远返回 ProgramData（哪怕本环境用不了）→ 必须报「该退回却没退回」
      B. 永远返回 AppData（哪怕能用）           → 必须报「优先级不对」

    为什么 A 也要测：新加的「环境用不了 → 跳过 + 要求退回」是个**条件分支**，
      条件分支很容易被写成永远走的那一支（那就等于「永远通过」）。
      只有真的把坏行为喂进去、看它报不报，才知道这一支还活着。
    """
    import io
    from contextlib import redirect_stdout

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import campus_login as C
    except Exception as e:
        ck("第 4 节判据（无法导入 campus_login，跳过）", True, str(e))
        return

    real = C.data_dir
    common = C._shell_folder(C.CSIDL_COMMON_APPDATA) or os.environ.get("ProgramData") or ""
    prog = os.path.join(common, C.APP_NAME) if common else r"C:\ProgramData\CampusLogin"
    roam = os.path.join(C.roaming_dir(), C.APP_NAME)

    def run_with(fake):
        """把 data_dir() 换成恒定返回 fake，看第 4 节会不会记失败。"""
        saved = FAILS[:]          # 别污染主流程的 FAILS
        try:
            del FAILS[:]
            C._DATA_DIR.clear()
            C.data_dir = lambda: fake
            try:
                with redirect_stdout(io.StringIO()):
                    check_data_dir_fallback()
                return bool(FAILS)
            finally:
                C.data_dir = real
                C._DATA_DIR.clear()
        finally:
            del FAILS[:]
            FAILS.extend(saved)

    ck("坏成「永远用 ProgramData」时会报失败", run_with(prog), prog)
    ck("坏成「永远退回 AppData」时会报失败", run_with(roam), roam)


def self_test():
    """门槛自检：证明这几条检查**真的会拦人**，不是空转。

    为什么门槛要自带这个（2026-10-03 加）：
        放宽一条门槛是危险动作 —— 一不小心就把它改成「永远通过」，
        而「永远通过」和「通过」在输出上长得**一模一样**。
        所以这里用构造出来的样本做正反对照：该报的必须报，不该报的必须不报。

    用法：
        python portability_check.py --selftest
    """
    fails = []

    def ck(name, ok, detail=""):
        print("  [%s] %s%s" % ("OK" if ok else "失败", name,
                               ("  " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    print("=== 1. 绝对路径检查：该抓的抓、该放过的放过 ===")
    ck("裸的本机用户路径能匹配",
       bool(ABS_USER_PATH.search(b"x=C:\\Users\\Bob\\proj\\a.ini")))
    ck("Documents and Settings 老路径也能匹配",
       bool(ABS_USER_PATH.search(b"x=C:\\Documents and Settings\\Bob\\a.ini")))
    ck("相对路径不误报（PYZ 里的 co_filename 长这样）",
       not ABS_USER_PATH.search(b"tkinter\\__init__.py"))

    print()
    print("=== 2. PE 二进制要跳过、数据文件要查 ===")
    # 为什么这条也要自检：跳过 PE 二进制是**放宽**，放宽必须证明没放宽过头。
    # 上游二进制里的 PDB / __FILE__ 路径是噪声；数据文件里出现本机路径才是事故。
    ck("python312.dll 算二进制（跳过）", is_binary_entry("python312.dll"))
    ck("_ssl.pyd 算二进制（跳过）", is_binary_entry("_ssl.pyd"))
    ck("大写扩展名也认（FOO.PYD）", is_binary_entry("FOO.PYD"))
    ck("config.json 不算二进制（要查）", not is_binary_entry("config.json"))
    ck("设置.ico 不算二进制（要查）", not is_binary_entry("设置.ico"))

    print()
    print("=== 3. 环境探测本身会不会误判（_roundtrip_ok）===")
    # 为什么必须自检这一条（2026-10-03）：第 4 节现在会**根据** _roundtrip_ok 的
    # 结果决定「要求用 ProgramData」还是「要求退回」。所以 _roundtrip_ok 一旦
    # 恒返回 False，第 4 节就永远走「退回」分支 —— 又是「永远通过」。
    # 必须证明它在该说「能」的时候真的说「能」。
    import tempfile
    td = tempfile.mkdtemp(prefix="pc_selfcheck_")
    try:
        ck("可写的临时目录判为「能用」", _roundtrip_ok(td)[0], td)
        ck("指向文件的路径判为「不能用」",
           not _roundtrip_ok(os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                          "notepad.exe"))[0])
        ck("不存在的盘符判为「不能用」", not _roundtrip_ok("Z:\\nope\\x")[0])
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(td)

    print()
    print("=== 4. 第 4 节的判据本身会不会被绕过 ===")
    # 为什么必须自检（2026-10-03）：第 4 节刚改成**条件分支**（环境能用就要求
    # ProgramData，不能用就要求退回），而条件分支最容易被写成「永远走同一支」。
    # 这里把 data_dir() 故意弄坏两个方向，看第 4 节报不报。
    self_test_data_dir(ck)

    print()
    if fails:
        print("自检失败 %d 项: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("自检通过 —— 这几条检查确实会拦人")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(self_test())
    sys.exit(main())
