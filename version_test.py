# -*- coding: utf-8 -*-
"""版本号的测试：从「常量」到「exe 右键属性里显示的那行字」，整条链路都得对。

为什么值得单独测：
    版本号是**到处派生**的东西（界面标题、使用说明、--version、--selftest、
    exe 的 PE 版本资源、构建脚本）。任何一处写成字面量，都会在下次改版本号时
    漂移，而且**没有任何东西会报错** —— 界面上写着 v0.2.0、文件属性里还是 0.1.0，
    只有真人去点「属性」才发现。
    更隐蔽的是 `--version-file`：PyInstaller 出问题时不报错，只是资源没进 PE，
    「传了参数」和「真写进去了」在日志里一模一样。所以这里必须**从产物里读回来**。

用法：
    python version_test.py                  # 只测纯函数部分（不碰 exe）
    python version_test.py <某个.exe>        # 额外核对那个 exe：版本资源 + --version 弹窗

第 6 节会**真的启动那个 exe**（用 proc_tree 起，跑完收掉整棵进程树），
所以要么给 exe 路径、要么接受它被跳过。第 2 节依赖 PyInstaller。

退出码 0 = 全过。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build as B            # noqa: E402
import campus_login as C     # noqa: E402

FAILS = []

# 有版本资源的系统 exe —— 用来证明「读回版本号」这条路真的走得通。
# 没有阳性对照的话，_read_exe_version() 就算永远返回空串，测试也是绿的。
SYSTEM_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "notepad.exe")


def check(name, got, want):
    ok = got == want
    extra = "" if ok else "   (期望 %r)" % (want,)
    print("  [%s] %-46s -> %r%s" % ("OK" if ok else "失败", name, got, extra))
    if not ok:
        FAILS.append(name)


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else ""

    print("=== 1. 语义化版本 → Windows 的 4 段数字 ===")
    # Windows 的 filevers 只吃 4 段数字，"0.1.0-beta" 这类后缀必须丢掉，
    # 否则 PyInstaller 打包时直接抛异常。
    check('"0.1.0" -> 补成 4 段', B._ver_tuple("0.1.0"), (0, 1, 0, 0))
    check('"1" -> 只有一段', B._ver_tuple("1"), (1, 0, 0, 0))
    check('"1.2" -> 两段', B._ver_tuple("1.2"), (1, 2, 0, 0))
    check('"1.2.3.4" -> 正好四段', B._ver_tuple("1.2.3.4"), (1, 2, 3, 4))
    check('"1.2.3.4.5" -> 多出来的截掉', B._ver_tuple("1.2.3.4.5"), (1, 2, 3, 4))
    check('"0.2.0-beta" -> 后缀丢掉', B._ver_tuple("0.2.0-beta"), (0, 2, 0, 0))
    check('"0.2.0-rc1" -> 后缀里的数字不混进来', B._ver_tuple("0.2.0-rc1"), (0, 2, 0, 0))
    check('"" -> 全 0，不抛异常', B._ver_tuple(""), (0, 0, 0, 0))

    print()
    print("=== 2. 版本资源文本能被 PyInstaller 的类 eval 出来 ===")
    # 这一步是**构建前的预检**：模板里少个括号，与其等 3 分钟打包完才报错，
    # 不如在这里就炸。用 PyInstaller 自己的类来 eval，等于模拟它的读取过程。
    try:
        import PyInstaller.utils.win32.versioninfo as VI
    except ImportError as e:
        print("  [跳过] 当前解释器没有 PyInstaller: %s" % e)
        print("         （纯函数部分仍有效；要测这节请用 envs/gui314 的解释器）")
        VI = None
    if VI is not None:
        vf = os.path.join(HERE, "version_info.txt")
        B._version_file(vf)
        with open(vf, "r", encoding="utf-8") as f:
            text = f.read()
        check("生成的文件非空", len(text) > 100, True)
        check("没有 BOM（eval 会被 \\ufeff 噎住）", text[:1] != "\ufeff", True)
        ns = {k: getattr(VI, k) for k in
              ("VSVersionInfo", "FixedFileInfo", "StringFileInfo",
               "StringTable", "StringStruct", "VarFileInfo", "VarStruct")}
        try:
            obj = eval(text, ns)   # noqa: S307 —— 测的就是这个，内容是本地生成的
            check("eval 不抛异常", True, True)
        except Exception as e:
            obj = None
            check("eval 不抛异常", "%s: %s" % (type(e).__name__, e), True)

    if VI is not None and obj is not None:
        # 固定信息：4 段数字要和 VERSION 对上
        quad = B._ver_tuple(C.VERSION)
        got_quad = (obj.ffi.fileVersionMS >> 16, obj.ffi.fileVersionMS & 0xffff,
                    obj.ffi.fileVersionLS >> 16, obj.ffi.fileVersionLS & 0xffff)
        check("filevers == _ver_tuple(VERSION)", got_quad, quad)
        got_prod = (obj.ffi.productVersionMS >> 16, obj.ffi.productVersionMS & 0xffff,
                    obj.ffi.productVersionLS >> 16, obj.ffi.productVersionLS & 0xffff)
        check("prodvers == _ver_tuple(VERSION)", got_prod, quad)
        check("fileType == 1（应用程序）", obj.ffi.fileType, 1)

        # 字符串信息：这里才是用户在「属性 → 详细信息」里看到的东西
        tables = obj.kids[0].kids
        check("只有一个 StringTable", len(tables), 1)
        table = tables[0]
        check("StringTable 键 = 语言+代码页 080404b0", table.name, "080404b0")
        fields = {s.name: s.val for s in table.kids}
        check("FileVersion 就是 VERSION 原文", fields.get("FileVersion"), C.VERSION)
        check("ProductVersion 就是 VERSION 原文", fields.get("ProductVersion"), C.VERSION)
        check("FileDescription = 程序名", fields.get("FileDescription"), C.APP_TITLE)
        check("ProductName = 程序名", fields.get("ProductName"), C.APP_TITLE)
        check("OriginalFilename 带 .exe",
              fields.get("OriginalFilename"), C.APP_TITLE + ".exe")
        check("八个字段一个不少", len(fields), 8)

        # Translation 必须和 StringTable 的键一致 —— 对不上，属性页里读不出字符串，
        # 但打包过程**一点错都不报**。
        tr = obj.kids[1].kids[0].kids
        check("VarFileInfo 的 Translation 与表键一致",
              "%04x%04x" % (tr[0], tr[1]), table.name)

    print()
    print("=== 3. 读回版本号：阳性 / 阴性对照 ===")
    # 阳性对照：系统自带的 notepad.exe 一定有版本资源。
    # 这条不过 = 我的读取代码坏了，那下面第 4 节的「通过」就毫无意义。
    sys_ver = B._read_exe_version(SYSTEM_EXE)
    check("阳性对照：读得出 notepad.exe 的版本", len(sys_ver) > 0, True)
    if sys_ver:
        print("         （notepad.exe 报的是 %r —— 说明读的是真资源，不是常量）"
              % sys_ver)
    check("阴性对照：对非 PE 文件返回空串",
          B._read_exe_version(os.path.join(HERE, "build.py")), "")

    print()
    print("=== 4. 目标 exe 的版本资源 ===")
    if not exe:
        print("  [跳过] 没给 exe 路径。打包后这样跑：python version_test.py <distN/CampusLogin.exe>")
    elif not os.path.isfile(exe):
        check("exe 存在", exe, "存在的文件")
    else:
        got = B._read_exe_version(exe)
        print("         %s" % exe)
        print("         %d 字节，报的版本是 %r" % (os.path.getsize(exe), got))
        check("exe 的 FileVersion == campus_login.VERSION", got, C.VERSION)
        # 反向确认：读回来的值确实随 exe 变，不是一个写死的常量
        check("和系统 exe 的版本不同（证明读的是各自资源）",
              got != sys_ver or not sys_ver, True)

    print()
    print("=== 5. 文档里的版本号和 VERSION 一致 ===")
    # README 里必须写版本号（用户一眼要看到），但那就成了「第二处硬编码」——
    # 改 VERSION 时忘了改 README，界面显示 0.2.0、README 还写 0.1.0，没人会报错。
    # 所以这里把它**变成被检查的副本**：对不上就让测试红。
    # 正则要求前后都不是数字或点，免得把 IP 里的 "202.196.169" 当成版本号。
    import re
    pat = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])")
    # 检查器自己的对照：抓得到真版本号，又不能把 IP 当成版本号。
    # README 里确实写着门户 IP，没有阴性对照的话，正则写宽了会把它们算进「版本号」，
    # 于是测试永远红、或者更糟——被人当成「反正会红」而忽略。
    check("阳性：正则抓得到 9.9.9", pat.findall("版本 9.9.9"), ["9.9.9"])
    check("阴性：IP 不会被当成版本号",
          pat.findall("门户 202.196.169.166 / 内网 10.0.1.10"), [])
    for name in ("README.md",):
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            check("%s 存在" % name, path, "存在的文件")
            continue
        with open(path, "r", encoding="utf-8") as f:
            found = pat.findall(f.read())
        print("         %s 里出现的版本号: %r" % (name, found))
        check("%s 里的版本号都等于 VERSION" % name,
              sorted(set(found)), [C.VERSION])

    print()
    print("=== 6. 打包后 --version 真的弹出了窗口 ===")
    # 这是唯一一条「只能在打包产物上验」的路径：源码模式 print 就行，打包后没有
    # stdout，必须弹 messagebox。而弹窗是**模态**的，没人点它就一直停在那儿 ——
    # 所以「10 秒后还没退出」恰恰是**成功**的标志，不是超时失败。
    # 反过来，如果弹窗代码抛了异常，`log()` 会记下「--version 弹窗失败」然后
    # exit_now(0)，进程**立刻消失** —— 那时 timed_out 会是 False。
    # 两条断言合起来才能区分「在等用户点」和「已经挂了」。
    if not exe or not os.path.isfile(exe):
        print("  [跳过] 没给 exe 路径")
    else:
        import proc_tree
        log_file = C.LOG_FILE
        before = os.path.getsize(log_file) if os.path.isfile(log_file) else 0
        rc, timed_out = proc_tree.run_exe(exe, ["--version"], timeout=10)
        tail = ""
        if os.path.isfile(log_file):
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(before)
                tail = f.read()
        print("         退出码=%s 10s 内未退出=%s" % (rc, timed_out))
        check("弹窗路径没有抛异常（日志里没有「弹窗失败」）",
              "弹窗失败" in tail, False)
        check("进程停在模态弹窗上等用户点（10s 未退出）", timed_out, True)
        if not timed_out:
            print("         日志尾部: %s" % tail[-300:].replace("\n", " | "))
        # 阴性对照：--selftest 不建窗口、干完就退。它必须**不超时** ——
        # 否则「10s 未退出」可能只是 run_exe 的返回值永远为真，
        # 那上面那条断言就是废话（「检查通过」≠「检查在跑」，第七条第 3 点）。
        rc2, to2 = proc_tree.run_exe(exe, ["--selftest"], timeout=30)
        print("         对照 --selftest: 退出码=%s 超时=%s" % (rc2, to2))
        check("阴性对照：--selftest 自己会退出（不超时）", to2, False)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 —— 版本号从常量到 exe 属性页是一条链，没有第二处硬编码")
    return 0


if __name__ == "__main__":
    sys.exit(main())
