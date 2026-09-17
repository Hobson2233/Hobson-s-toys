# -*- coding: utf-8 -*-
"""重新打包 exe。

用法（**必须用带 tkinter 的解释器** —— managed Python 没带 tcl/tk，
打包出来会缺 tkinter）：
    python build.py

什么解释器行？自己 `python -c "import tkinter"` 试一下，不报错就行。
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "campus_login.py")
ICON = os.path.join(HERE, "设置.ico")
NAME = "CampusLogin"
# 交付目录 = 桌面根目录（exe 直接放桌面上，方便找到和发出去）。
# 以前是桌面的一个子文件夹，改过一次 —— 挪动交付位置会**让开机自启失效**，
# 因为启动文件夹里的 .lnk 存的是绝对路径。改这里就要重新设置一次自启。
DESKTOP_DIR = os.path.join(os.path.expanduser("~"), "Desktop")

# 接线自检的失败标识：从主程序导入，**不在这里再写一份字面量**。
# 两处硬编码同一个字符串迟早会漂移，那时构建门槛就永远搜不到、静默失效。
# campus_login 顶层不 import tkinter（都是延迟导入），所以这里导入是安全的。
sys.path.insert(0, HERE)
from campus_login import WIRING_FAIL_PHRASE  # noqa: E402
import proc_tree  # noqa: E402  —— 收进程树（onefile 是父子两个进程，别只杀父）

# 从包里剔掉的模块。每一项都有具体理由，别凭感觉往里加。
#   ssl / _ssl / _hashlib
#       本程序只用 http://（portal、1.1.1.1、msftconnecttest 全是明文），
#       用不到 TLS。剔掉它们，PyInstaller 就不会再打进
#       libcrypto-3.dll + libssl-3.dll —— 解压后合计约 5.8MB，是最大的一块可省项。
#       已实测：urllib.request 在没有 ssl 时会把 _have_ssl 置 False，HTTP 照常工作。
#   requests / urllib3 / idna / charset_normalizer / certifi / chardet
#       已改用标准库 urllib，这些一个都不再 import。
#   其余
#       本程序从不使用的开发工具与可选模块（编译/测试/文档/数据库/压缩格式等）。
EXCLUDES = (
    "ssl", "_ssl", "_hashlib",
    "requests", "urllib3", "idna", "charset_normalizer", "certifi", "chardet",
    "xml", "pyexpat",
    "sqlite3", "_sqlite3",
    "curses",
    "unittest", "pydoc", "doctest", "difflib", "lib2to3",
    "distutils", "setuptools", "pkg_resources", "pip", "wheel",
    "turtle", "turtledemo", "idlelib", "test",
    "multiprocessing", "asyncio",
    "decimal", "_decimal",
    "compression.zstd", "_zstd",
    "lzma", "_lzma", "bz2", "_bz2",
)


def _data_dir():
    """和 campus_login.py 里 data_dir() 保持一致：优先 C:\\ProgramData\\CampusLogin，
    写不进去才退回 %APPDATA%\\CampusLogin。

    坑：本环境没有 APPDATA 环境变量，只靠它取会退化成用户主目录，
    所以主路径走 SHGetFolderPathW。两处逻辑必须同步，否则这里会去错地方找
    冒烟测试的标记文件，把「没找到标记」误判成「构建失败」。
    """
    import ctypes

    def shell(csidl):
        try:
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                return buf.value or ""
        except Exception:
            pass
        return ""

    def writable(d):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_probe")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return True
        except Exception:
            return False

    common = shell(0x0023) or os.environ.get("ProgramData") or ""   # CSIDL_COMMON_APPDATA
    roaming = shell(0x001A) or os.environ.get("APPDATA") or \
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    for base in (common, roaming):
        if base:
            d = os.path.join(base, "CampusLogin")
            if writable(d):
                return d
    return os.path.join(roaming, "CampusLogin")


def _small_icon(src, dst, sizes):
    """从 src 里挑出指定尺寸，另存成一个小得多的 .ico。

    图标在包里会出现两份：一份是 exe 自身的图标资源（--icon），一份是给界面
    调 iconbitmap 用的数据文件（--add-data）。后者只要够画标题栏/任务栏即可，
    没必要把 256x256 那张（占了整个文件的三分之二）也带上。
    只做挑选、不重新编码，所以是无损的。
    """
    import struct
    with open(src, "rb") as f:
        d = f.read()
    if len(d) < 6:
        return None
    _, typ, cnt = struct.unpack("<HHH", d[:6])
    if typ != 1:
        return None
    picked = []
    for i in range(cnt):
        off = 6 + i * 16
        if off + 16 > len(d):
            break
        w, h, cc, rv, planes, bpp, size, offset = struct.unpack("<BBBBHHII",
                                                                d[off:off + 16])
        if (w or 256) in sizes:
            picked.append((w, h, cc, rv, planes, bpp, d[offset:offset + size]))
    if not picked:
        return None
    head = bytearray(struct.pack("<HHH", 0, 1, len(picked)))
    body = bytearray()
    base = 6 + 16 * len(picked)
    for w, h, cc, rv, planes, bpp, blob in picked:
        head += struct.pack("<BBBBHHII", w, h, cc, rv, planes, bpp,
                            len(blob), base + len(body))
        body += blob
    with open(dst, "wb") as f:
        f.write(bytes(head + body))
    return len(head) + len(body)


def main():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("当前解释器没有 tkinter，换一个（见本文件顶部的用法说明）")
        return 1

    # 注意：不要用 --clean，也不要 rmtree/os.remove —— 会触发安全策略的
    # 「批量删除」保护（按 turn 累计计数，删 50 个就拦）。
    # 这里每次自动挑一个**全新的**目录，PyInstaller 全程没有可删的东西。
    n = 1
    while True:
        work = os.path.join(HERE, "build%d" % n)
        dist = os.path.join(HERE, "dist%d" % n)
        if not os.path.exists(work) and not os.path.exists(dist):
            break
        n += 1
    print("构建目录:", work)

    # 界面用的图标不需要 256x256 那张，裁一份小的塞进包里。
    # 注意必须落在子目录里**仍叫 设置.ico** —— 程序是在 _MEIPASS 里按
    # 这个名字找图标文件的，改名字会让界面退回 Tk 默认图标。
    icon_dir = os.path.join(HERE, "icon_small")
    os.makedirs(icon_dir, exist_ok=True)
    small_icon = os.path.join(icon_dir, "设置.ico")
    n_icon = _small_icon(ICON, small_icon, (16, 24, 32, 48, 64, 128))
    if n_icon:
        print("运行时图标: %d 字节（原图标 %d 字节）"
              % (n_icon, os.path.getsize(ICON)))
    else:
        small_icon = ICON
        print("图标裁剪失败，改用原图标")

    hooks_dir = os.path.join(HERE, "hooks")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--onefile", "--windowed", "--name", NAME,
           "--icon", ICON, "--add-data", "%s%s." % (small_icon, os.pathsep),
           "--distpath", dist,
           "--workpath", work,
           "--specpath", work,
           "--additional-hooks-dir", hooks_dir]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(APP)
    print(" ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print("打包失败")
        return r.returncode

    src = os.path.join(dist, NAME + ".exe")
    print("产物:", src, os.path.getsize(src), "字节")

    # 冒烟测试。跑的是刚构建出来的产物本身，不依赖桌面副本是否已就位 ——
    # 桌面那份可能正被打开着的程序占着，而复制是唯一会因此失败的一步，
    # 不该把冒烟测试和隐私检查一起跳过（复制挪到文件末尾了）。
    # 坑 1：--windowed 打包出来的 exe 没有 stdout（sys.stdout 是 None），
    #       靠捕获输出来判断是没用的，只能看它落盘的标记文件。
    # 坑 2：--guitest 会真的建一个 Tk 窗口。在受控/无人值守会话里，
    #       建窗口可能又慢又飘（实测同一个 exe 有时 0.9s、有时 14s、有时不返回），
    #       源码版也一样飘 —— 所以它只能当参考，不能当构建成败的判据。
    #       --selftest 不碰 Tk，才是可靠的那条。
    # 坑 3（最坑）：**绝对不要用 capture_output**。onefile 的 bootloader 父进程会
    #       再起一个子进程，子进程会继承 stdout/stderr 的管道句柄。父进程被 kill 之后
    #       子进程还活着，管道写端就一直不关；而 subprocess.run 在超时分支里会
    #       调 process.communicate() 去收尾，于是**永远等不到 EOF**——设了 timeout
    #       也照样挂死（实测整个构建卡了 5 分钟以上且毫无输出）。
    #       用 DEVNULL 就没有管道可等，进程一退就返回。
    exe = src
    started = time.time()
    data = _data_dir()
    print("数据目录:", data)

    for flag, marker, want, must in (
            ("--selftest", "selftest.txt", "版本", True),
            ("--guitest", "guitest.txt", "GUI_OK", False)):
        t0 = time.time()
        # 坑 4：**不要用 subprocess.run(timeout=...)**。它超时后内部只 kill 启动器
        #   父进程，真正跑代码、持有窗口的子进程会变成孤儿留在用户桌面上。
        #   run_exe 会把整棵进程树收掉（job object + taskkill /T 兜底）。
        rc, timed_out = proc_tree.run_exe(exe, [flag], timeout=45)
        elapsed = time.time() - t0
        f = os.path.join(data, marker)
        text = ""
        fresh = False
        if os.path.isfile(f):
            fresh = os.path.getmtime(f) >= started - 1
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        ok = fresh and (want in text)
        # 接线自检失败是**确定性**的代码错误，和环境性的 Tk 卡住不是一回事：
        # 它表现为干净退出 + GUI_FAIL，而不是超时。所以单独拎出来硬性拦截 ——
        # 这条缝一旦漏过去，用户看到的是「密码框里字看着对、点登录却失败」。
        # 标识串从 campus_login 导入，不在这里再写一遍：两处硬编码同一个字符串
        # 迟早会漂移，那时这里就永远搜不到、门槛静默失效。
        wiring_bad = WIRING_FAIL_PHRASE in text
        # 超时也要读标记文件！实测 --guitest 会先把 GUI_OK 写进 guitest.txt，
        # 然后才卡在退出上 —— 只看「超时」会把「界面其实建成功了」误判成失败。
        if timed_out:
            note = "必须通过" if must else "环境相关，不算失败"
            print("%s -> **超时 45s 未退出**（%s）/ 标记文件 %s / 界面%s"
                  % (flag, note, "新且为 " + want if ok else "旧或缺失",
                     "正常构建" if ok else "可能有问题"))
            if must or wiring_bad:
                if wiring_bad:
                    print(text[:500])
                    print("!! 密码框接线自检失败 —— 构建判为失败")
                return 1
            continue
        print("%s -> 退出码 %s / %.1fs / 标记文件 %s / %s"
              % (flag, rc, elapsed, "新" if fresh else "旧或缺失",
                 "OK" if ok else "失败"))
        if not ok:
            print(text[:500])
            if must:
                return 1
        if wiring_bad:
            print("!! 密码框接线自检失败 —— 构建判为失败")
            return 1

    # 隐私检查（构建门槛）：确认 exe 里没夹带本机的账号密码。
    # 必须解开 PyInstaller 的压缩归档再搜 —— 直接对 exe grep 是搜不到的，
    # 那种检查永远不会报警，等于没查。
    # 检查本身跑不起来也判失败：宁可不出包，也不要出一个没验过的包。
    print()
    try:
        import leak_check
        ok_st, lines_st = leak_check.self_test(exe)
        for ln in lines_st:
            print(ln)
        print()
        ok_leak, lines = leak_check.scan(exe)
    except Exception as e:
        print("!! 隐私检查无法执行: %s: %s" % (type(e).__name__, e))
        return 1
    for ln in lines:
        print(ln)
    if not ok_st:
        print("!! 阳性对照失效 —— 上面的「通过」不可信，构建判为失败")
        return 1
    if not ok_leak:
        print("!! 隐私检查不通过 —— 构建判为失败")
        return 1

    # 可移植性检查（构建门槛）：「拷到别的 Windows 上就能跑」是这个程序的核心卖点，
    # 一旦有人加了个依赖系统 DLL 的东西，只有换台机器才会暴露 —— 那太晚了。
    # 检查项：PE 导入表全是系统核心 DLL / VC++ 运行库和解释器都打进去了 /
    #         归档里没有绑定本机用户目录的绝对路径 / 数据目录的回退链还通。
    # 传 `exe`（刚构建的产物）而不是让它用默认值 —— 默认值指向桌面交付副本，
    # 那可能是上一版留下的，查它等于没查。
    print()
    try:
        import portability_check
        rc_port = portability_check.run(exe)   # 0 = 通过
    except Exception as e:
        print("!! 可移植性检查无法执行: %s: %s" % (type(e).__name__, e))
        return 1
    if rc_port:
        print("!! 可移植性检查不通过 —— 构建判为失败")
        return 1

    # 最后一步：复制到桌面交付目录。
    # 放最后是因为它是唯一会被「程序正开着」影响的一步 —— 正在运行的 exe
    # 无法被覆盖（WinError 32），而前面的检查不该陪着一起失败。
    # 只交付 exe 一个文件：使用说明已内置在程序里（界面右上角「使用说明」），
    # 不再附带 .txt。
    print()
    os.makedirs(DESKTOP_DIR, exist_ok=True)
    dst = os.path.join(DESKTOP_DIR, "校园网自动登录.exe")
    try:
        shutil.copy2(src, dst)
    except PermissionError:
        print("!! 无法写入 %s" % dst)
        print("   该文件正在使用中（程序还开着？）。关闭「校园网自动登录」后重新构建，")
        print("   或手动把下面这个文件复制过去：")
        print("   %s" % src)
        return 1
    print("已复制到:", DESKTOP_DIR)

    return 0


if __name__ == "__main__":
    sys.exit(main())
