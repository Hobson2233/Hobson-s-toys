# -*- coding: utf-8 -*-
"""给「会建真窗口 / 会跑 run_gui 的脚本」用的数据沙箱。

🔴 为什么必须有（2026-10-04，**真出过事**）：
    `run_gui()` 启动时会走 `load_all() → mutate_accounts() → save_accounts()`，
    而 `mutate_accounts()` 内部调的是**模块全局的** `load_accounts()`。
    于是一个「只想量界面高度」的探针，只要打桩了 `load_accounts`（换成合成账号），
    就会把**打桩用的假账号**原样写进真实的 `accounts.json`。
    实测后果：真实账号 `2507210230` 的 `lastSuccess`/`lastAttempt`/`lastResult`
    被清空，还凭空多出 6 个假账号。

    这类脚本最危险的地方是**意图（只读）和行为（写盘）之间没有任何提示**：
    跑完打印一堆高度数字，看起来完全正常，磁盘上的用户数据已经被改了。

三道闸（缺一不可，各拦各的）：
    1) **重定向**：把 `_DATA_DIR` 和 5 个路径常量一起指到临时目录。
       ⚠️ 光改 `_DATA_DIR` 不够 —— `CONFIG_FILE` 等在**导入期**就已经算好了。
    2) **写拦截**：把 `write_json` 包一层，目标不在沙箱里就**当场抛异常**。
       前一道只是「让它写到别处」，这一道才是「让它根本写不出去」。
    3) **事后核对**：记住真实数据文件的 md5，收尾时比对。
       前两道万一都失效，至少能**知道**出事了 —— 而不是静默通过。

用法：
    import datasafe
    datasafe.sandbox(copy_real=True)      # 必须在 run_gui() 之前调用
    ...
    datasafe.assert_untouched()           # 收尾时核对真实数据没被动过

    python datasafe.py                    # 跑自检（含负向对照）
"""
import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import campus_login as C      # noqa: E402

# 会被重定向的路径常量。和 campus_login 里的 5 个一一对应。
_PATH_ATTRS = ("CONFIG_FILE", "ACCOUNTS_FILE", "LOG_FILE", "RESULT_FILE")

_state = {"dir": None, "real": None, "snap": None}
_orig_write_json = C.write_json


def md5(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


def candidate_dirs():
    """所有可能存放用户数据的目录，按 `data_dir()` 的优先级排。

    ⚠️ 为什么是**两个**（2026-10-04 实测踩到）：
        `data_dir()` 带一个「写得进 **且 删得掉**」的写测试（`_writable`），
        而沙箱会把删除重定向到回收站、回收站操作又被中止 → `os.remove` 抛
        OSError → `_writable()` 判 False → `data_dir()` **正确地**退回
        `%APPDATA%\\CampusLogin`。
        （这个现象和「它是环境限制不是产物缺陷」的结论，见
          portability_check.check_data_dir_fallback 里那段记录。）

        后果是：**同一台机器上两个目录都可能有真实数据** ——
        桌面版 exe（不在沙箱里跑，删得掉）用的是 ProgramData，
        而我们这些探针/测试脚本（在沙箱里跑）用的是 %APPDATA%。
        只盯其中一个，就会漏掉另一个里躺着的用户数据。
    """
    out = []
    try:
        common = (C._shell_folder(C.CSIDL_COMMON_APPDATA)
                  or os.environ.get("ProgramData") or "")
    except Exception:
        common = os.environ.get("ProgramData") or ""
    if common:
        out.append(os.path.join(common, C.APP_NAME))
    try:
        out.append(os.path.join(C.roaming_dir(), C.APP_NAME))
    except Exception:
        pass
    return out


def real_dir():
    """**用户真实数据所在的**目录（第一次调用时定下来并缓存）。

    🔴 为什么不直接用 `C.data_dir()`（2026-10-04 踩到，偏差是静默的）：
        本环境里 `data_dir()` 会退回 `%APPDATA%\\CampusLogin`，而那里
        **没有 accounts.json**。于是探针量到的是「空账号列表」的界面：
        `reqh = 814`，账号卡 111px。
        可用户的数据在 `C:\\ProgramData\\CampusLogin`，真实状态是
        `reqh = 830`、账号卡 127px —— **整整差 16px**。
        数字照常打出来、看着一切正常，只是不对应真实状态。

    判定规则：**哪个候选目录里真的有 `accounts.json`，就用哪个**；
    都没有（全新机器）就按 app 的优先级取第一个。

    ⚠️ 必须在 sandbox() 之前调用一次，否则 sandbox() 改完 `_DATA_DIR`
       之后就再也问不到真身是谁了（2026-10-04 自检第一次跑就撞在这个 None 上）。
    """
    if _state["real"] is None:
        chosen = None
        for d in candidate_dirs():
            if os.path.isfile(os.path.join(d, "accounts.json")):
                chosen = d
                break
        if chosen is None:
            dirs = candidate_dirs()
            chosen = dirs[0] if dirs else C.data_dir()
        _state["real"] = chosen
    return _state["real"]


def all_data_files():
    """**两个候选目录里**可能被写的文件（用 campus_login 自己的清单，别另抄一份）。"""
    out = []
    for d in candidate_dirs():
        for n in C.PRIVATE_FILES:
            out.append(os.path.join(d, n))
    return out


def real_data_files():
    """真实数据目录里的文件。保留旧名字 —— 探针和文档都在用它。"""
    return [os.path.join(real_dir(), n) for n in C.PRIVATE_FILES]


def snapshot():
    """记下**所有候选目录**里真实数据文件的 md5。

    ⚠️ 扫全部候选目录而不是只扫 `real_dir()`：`data_dir()` 会因环境摇摆，
       只盯一个就等于给另一个留了后门 —— 而那个「另一个」很可能才是
       用户桌面版 exe 真正在用的。护栏宁可多盯一个空目录，不能少盯一个真目录。
    """
    return {p: md5(p) for p in all_data_files()}


def _inside(path, root):
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def sandbox(copy_real=False, tag="probe"):
    """把数据目录换到临时目录。**必须在 run_gui() 之前调用。**

    copy_real=True 时，先把真实数据**复制**一份进去 ——
    这样测出来的仍然是「这台机器真实状态」的界面，但一个字节都不会写回真身。
    """
    real = real_dir()
    d = os.path.join(tempfile.gettempdir(), "campus_datasafe_" + tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)

    if copy_real:
        for name in C.PRIVATE_FILES:
            src = os.path.join(real, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(d, name))

    C._DATA_DIR[:] = [d]
    for attr in _PATH_ATTRS:
        setattr(C, attr, os.path.join(d, getattr(C, attr).rsplit(os.sep, 1)[-1]))
    _state["dir"] = d

    # 闸 2：写出沙箱就抛异常。**不吞、不警告，直接炸** ——
    # 一个「本来只想读」的脚本试图写真实数据，必须让写的那一行响起来。
    def guarded_write_json(path, obj):
        if not _inside(path, d):
            raise RuntimeError(
                "datasafe: 拒绝写入沙箱之外的文件 %s（沙箱是 %s）—— "
                "这个脚本本来应该只读真实数据，写到这里说明沙箱没盖住这条路径。"
                % (path, d))
        return _orig_write_json(path, obj)

    C.write_json = guarded_write_json

    # 自检：确认真的换了地方。漏改一个常量就会「沙箱看着生效、其实漏了一条路」。
    outside = [a for a in _PATH_ATTRS if not _inside(getattr(C, a), d)]
    if outside:
        raise RuntimeError("datasafe: 这些常量没被重定向: %s" % ", ".join(outside))
    if not _inside(C.data_dir(), d):
        raise RuntimeError("datasafe: data_dir() 仍指向 %s" % C.data_dir())
    _state["snap"] = snapshot()
    return d


def assert_untouched():
    """核对真实数据文件一个字节都没变。变了就抛异常并指出是哪个。"""
    if _state["snap"] is None:
        return "datasafe: 没调用过 sandbox()，无从核对"
    now = snapshot()
    changed = [p for p in now if now[p] != _state["snap"].get(p)]
    if changed:
        raise RuntimeError(
            "datasafe: 🔴 真实数据被改动了！\n  " + "\n  ".join(
                "%s\n     之前 %s\n     现在 %s"
                % (p, _state["snap"].get(p), now[p]) for p in changed))
    watched = len(set(candidate_dirs()))
    return ("datasafe: 真实数据全部未改动（盯了 %d 个候选目录里的 %d 个文件）"
            % (watched, len(now)))


def _self_check():
    """自检：正向（沙箱内能写）+ 负向（沙箱外必须被拦）。只测正向 = 什么都没测。"""
    fails = []

    def check(name, ok, extra=""):
        print("  [%s] %s%s" % ("OK" if ok else "失败", name, extra))
        if not ok:
            fails.append(name)

    real = real_dir()
    before = snapshot()
    print("候选数据目录:")
    for d0 in candidate_dirs():
        has = os.path.isfile(os.path.join(d0, "accounts.json"))
        print("    %s %s   %s" % ("●" if d0 == real else " ", d0,
                                 "有 accounts.json" if has else "（没有 accounts.json）"))
    print("真实数据目录: %s" % real)

    d = sandbox(copy_real=False, tag="selfcheck")
    print("沙箱目录:     %s" % d)

    # 0) real_dir() 必须落在候选目录里，而且**优先选真的有数据的那个**。
    #    这条是 2026-10-04 补的：原来 real_dir() 直接用 C.data_dir()，
    #    而 data_dir() 在本机会退回 %APPDATA%（那里没有数据），
    #    于是探针量的是空账号列表的界面 —— 数字照打，只是不对。
    check("real_dir() 落在候选目录里", real in candidate_dirs())
    with_data = [x for x in candidate_dirs()
                 if os.path.isfile(os.path.join(x, "accounts.json"))]
    check("real_dir() 优先选真有数据的目录",
          (not with_data) or real == with_data[0],
          "  有数据的: %s，选中: %s" % (with_data, real) if with_data else "  （本机无数据，跳过）")

    # 1) 四个常量都指到沙箱里了
    bad = [a for a in _PATH_ATTRS if not _inside(getattr(C, a), d)]
    check("4 个路径常量都重定向到沙箱", bad == [], "  漏的: %s" % bad if bad else "")

    # 2) 正向：走程序自己的 save_accounts 写，必须落在沙箱里
    C.save_accounts({"accounts": {"x": {"passwd": "p", "lastSuccess": "",
                                        "lastAttempt": "", "lastResult": ""}},
                     "lastOnline": ""})
    check("正向：save_accounts 写在沙箱里",
          os.path.isfile(os.path.join(d, "accounts.json")))

    # 3) 负向：直接往真实路径写，必须**抛异常**（这道闸不响就等于没有）
    real_acct = os.path.join(real, "accounts.json")
    raised = None
    try:
        C.write_json(real_acct, {"accounts": {}, "lastOnline": ""})
    except RuntimeError as e:
        raised = e
    check("负向：往真实路径写被当场拦住", raised is not None,
          "" if raised else "  ⚠️ 没拦住！")
    if raised:
        print("        拦下的异常：%s" % str(raised)[:90])

    # 4) 负向的负向：确认拦截真的没写进去（而不是「抛了异常但文件已经坏了」）
    check("负向：真实 accounts.json 未被改动", md5(real_acct) == before.get(real_acct))

    # 5) 收尾核对
    check("真实数据整体未被改动", snapshot() == before)

    # 6) 假沙箱路径的边界：同前缀但不是子目录，不该被放行
    check("边界：同前缀目录不算沙箱内", not _inside(d + "_x", d))

    if fails:
        print("失败 %d 项: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("全部通过 —— 沙箱能挡住写真实数据，且正向路径仍然可用")
    return 0


if __name__ == "__main__":
    sys.exit(_self_check())
