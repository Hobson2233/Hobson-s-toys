# -*- coding: utf-8 -*-
"""统一测试入口 —— 一条命令把该跑的测试跑完。

为什么要它（2026-10-05 可维护性体检）：
    项目里有 50 多个测试 / 探针脚本，但**没有统一入口**，全靠人记「该跑哪几个」。
    而 `docs/DEVELOPMENT.md` 自己把其中三十多个标着「否（可以加进 build.py）」——
    意思是「本来就该跑，只是没人接」。这份清单就是把它接上。

三档（默认只跑 fast）：

| 档 | 是什么 | 能不能在无人值守会话里跑 |
|---|---|---|
| `fast` | 纯逻辑：不碰 Tk、不碰真桌面、不碰网络、不写真实数据 | 能，秒级 |
| `env`  | 要环境：Tk / 真桌面 / pylnk3 / 回环网络 | **不一定** —— 起不了窗口就会挂到超时 |
| `gate` | 构建门槛：需要一个**已构建的 exe** | 能（exe 由 build.py 产出） |

用法：
    python run_tests.py                  # = fast
    python run_tests.py fast|env|gate|all
    python run_tests.py gate <exe路径>    # 不传就用 paths.default_exe()
    python run_tests.py --list           # 列出全部（含不自动跑的 manual 档）
    python run_tests.py --audit          # 只做「有没有脚本漏接」的审计
    python run_tests.py -v               # 连通过的用例也把输出打出来

🔴 一律用**带 PyInstaller 的解释器**跑（项目里是 `envs/build`）：
   本脚本开头会自检，解释器不对就**明确报错**，不静默跳过 ——
   「检查通过」和「检查根本没跑」长得一样，是这类工具最容易骗人的地方。

⚠️ 输出用**临时文件**接，不用管道。理由见 `run_one()` 里的说明。
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# (脚本, 说明) —— 全部相对 repo 根目录
FAST = [
    ("ast_bugscan.py", "AST 静态扫描整个 repo（重复字典键 / 重复定义 / is 比字面量 / 空 except…）"),
    ("datasafe.py", "数据沙箱自检（重定向 + 拦写 + md5 核对）"),
    ("domain_drift_test.py", "站点域名一致性（7 个文件说的是同一个域名）"),
    ("form_drift_test.py", "门户表单逐键不漂移（登录 / 下线两张表）"),
    ("geometry_test.py", "9 种屏幕尺寸下窗口都不出屏"),
    ("logout_fail_probe.py", "两个窗口并发点「退出」→ 复现「退出失败」（不建 Tk，自带数据沙箱）"),
    ("network_state_test.py", "校园网判定四态 / MAC 校验 / 门户 MAC 替换"),
    ("poll_test.py", "界面轮询队列：出错记日志、不中断整批"),
    ("pwd_test.py", "密码框显示规则（纯函数）"),
    ("school_profile_test.py", "学校参数：换学校只改 config.json 真的生效（含阳性对照）"),
    ("secret_scan.py", "凭据 / 本机绝对路径扫描（提交前那道）"),
    ("ui_contrast_test.py", "按钮底色 ≠ 背景色（假控件树，不建 Tk）"),
    ("version_test.py", "版本号链路（纯函数部分；exe 那部分会自动跳过）"),
]

ENV = [
    ("autostart_test.py", "自启增删改查（startup_dir 劫持到临时目录，不碰真实启动项）", "pylnk3"),
    ("guard_test.py", "守护判断表 / 标记自愈 / do_auto 重试", "pylnk3"),
    ("proc_tree_test.py", "进程树与看门狗（含故意复现孤儿进程）", "起子进程"),
    ("net_check_test.py", "联网检测 test_internet 的三态语义", "网络"),
    ("wiring_gate_test.py", "验「接线自检」这道门槛的报警链路本身通不通", "真桌面"),
    ("ui_contrast_gate_test.py", "端到端验隐形按钮会被 --guitest 拦下", "真桌面"),
    ("pwd_gui_test.py", "密码框真实窗口绑定", "真桌面"),
    ("tcl_trim_test.py", "Tcl 收尾", "Tk"),
    ("content_h_probe.py", "量本机真实状态的内容高度（改界面竖向间距后要重跑）", "真桌面"),
    ("content_h_matrix.py", "量各运行状态下的内容高度矩阵", "真桌面"),
    ("layout_probe.py", "量布局：控件都在窗口内、不重叠", "真桌面"),
    ("ui_shot.py", "给真实窗口截图（5 张，本地看界面用）", "真桌面"),
]

GATE = [
    ("leak_check.py", "隐私：exe 里没夹带账号密码", "需要 exe"),
    ("portability_check.py", "可移植性：换台电脑能不能跑", "需要 exe"),
    ("help_check.py", "内置使用说明确实进了 exe", "需要 exe"),
    ("verify_exe.py", "只验不打包（冒烟 + 数据目录 + 更新落点）", "需要 exe"),
    ("analyze_archive.py", "归档构成分析", "需要 exe"),
]

# 🔴 列出来但**永不自动跑** —— 附上原因，免得下次有人「顺手加进去」。
#    判据（满足任一）：
#      · 会动真实账号 / 需要真实门户 / 需要现场参数
#      · 要反复起进程，或慢到不适合当日常入口
#      · **在这个受限会话里跑不出结论**：回环网络被丢弃、Tk 收尾会卡住、
#        断言依赖「ProgramData 可写」这类本机条件（2026-10-05 实测补的）
#      · 结论成立时**故意退非 0** 的诊断探针（退 1 = 「判断成立」，不是「失败」）
#    后两类是「换个环境就能跑」—— 哪天在正常桌面上，可以挪回 env 档试。
MANUAL = [
    ("switch_test.py", "需要真实校园网 + 真实账号（**会真的切账号**）"),
    ("e2e_test.py", "需要真实门户（先下线再自动登录）"),
    ("bench.py", "测启动耗时 / 内存峰值，要反复起 exe"),
    ("exit_stress.py", "反复起打包后的 exe，确认 headless 路径每次干净退出"),
    ("close_timing_test.py", "要在窗口**活着**时测「无响应」，必须真桌面"),
    ("exit_gui_test.py", "量点关闭后多久真的消失，必须真桌面"),
    ("exit_regression_test.py", "关窗后整棵进程树必须自己消失，必须真桌面"),
    ("exit_hang_probe.py", "探针：卡在退出上时用"),
    ("exit_plain_probe.py", "探针：退出路径对照"),
    ("exit_trace_probe.py", "探针：要 CAMPUS_EXIT_TRACE 环境变量"),
    ("auto_timing_probe.py", "拆 --auto 各阶段耗时，要现场日志"),
    ("logout_retry_probe.py", "探针：退出重试修复的阳性/阴性对照"),
    ("lnk_args_probe.py", "读真实 .lnk 的参数，要现场"),
    ("png_zoom.py", "工具：裁切放大 PNG，需要参数"),
    ("savepoint.py", "工具：本地存档，需要参数（save / list / restore / tidy）"),
    ("stack_probe.py", "工具：卡住时打线程栈，要现场"),
    ("shot.py", "库：截图原语（被 ui_shot.py 用）"),
    ("build.py", "构建入口，不是测试"),
    # ---- 2026-10-05 实测从 env 档挪下来的（当时 6 个「失败」里 5 个是分档错）----
    ("updater_test.py", "要**真回环网络**；受限会话把回环连接丢弃（脚本自己的 docstring 就写了），"
                        "实测必超时 240s。正常桌面上可以挪回 env"),
    ("watchdog_gil_test.py", "**概率性**复现 Tk 卡死；复现不到就报「什么都没验到」并退非 0"
                             "（脚本自己写了「常复现不到」）。要反复起 pythonw，不适合日常跑"),
    ("fresh_test.py", "要真桌面；且断言「数据目录落在 ProgramData 下」—— 受限会话里 "
                      "ProgramData 不可写，程序**正确地**退回 %APPDATA%，于是本条必然失败；"
                      "末尾 Tk 收尾还会卡住"),
    ("tk_probe.py", "诊断探针：打印 Tk 事件绑定表。打印完 root.destroy() 后收尾卡住，实测必超时"),
    ("tk_exit_race_test.py", "诊断探针：验证「os._exit 会卡、TerminateProcess 不会」这个**已知结论**，"
                             "结论成立时**故意退 1**（1 = 判断成立，不是失败）"),
]

TIERS = {"fast": FAST, "env": ENV, "gate": GATE, "manual": MANUAL}

# 不算测试的文件（库 / 数据 / 模板）。审计时会跳过它们，
# 但**只要出现一个新的 *_test.py / *_probe.py 没被登记，审计就报错**。
NOT_TESTS = {
    "campus_login.py", "paths.py", "palette.py", "updater.py", "proc_tree.py",
    "datasafe.py", "shot.py", "build.py", "savepoint.py", "png_zoom.py",
    "stack_probe.py", "local_secrets.example.py",
}


def _need_tk():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def check_interpreter(tier):
    """解释器自检。**不满足就返回错误说明**，别静默跑一半。"""
    problems = []
    if tier in ("env", "gate", "all") and not _need_tk():
        problems.append("当前解释器没有 tkinter —— env / gate 两档跑不了")
    if tier in ("env", "all"):
        try:
            import pylnk3  # noqa: F401
        except Exception:
            problems.append("当前解释器没有 pylnk3 —— autostart_test / guard_test 会自己报错退出(3)")
    return problems


def run_one(script, timeout, exe=None, extra_env=None):
    """跑一个脚本，返回 (状态, 耗时秒, 输出文本)。

    状态：pass / fail / timeout / missing

    🔴 **输出写临时文件，不走管道**。理由（`build.py` 里那条坑的同源问题）：
       管道的话，子进程再起的**孙进程**会继承写端；孙进程不退，写端就不关，
       `communicate()` 会**永远等不到 EOF** —— 设了 timeout 也照样挂死。
       写文件就没有「等 EOF」这回事，超时到了直接收进程树。
    """
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        return "missing", 0.0, "找不到 %s" % script

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if exe:
        # paths.py 认这个环境变量，门槛档的脚本会自己用它
        env["CAMPUS_EXE"] = exe
    if extra_env:
        env.update(extra_env)

    argv = [sys.executable, path]
    if script == "leak_check.py" and exe:
        argv.append(exe)          # leak_check 收一个位置参数

    fd, outpath = tempfile.mkstemp(prefix="runtests_", suffix=".log")
    os.close(fd)
    t0 = time.time()
    status = "pass"
    try:
        with open(outpath, "w", encoding="utf-8", errors="replace") as fh:
            p = subprocess.Popen(argv, cwd=HERE, stdout=fh,
                                 stderr=subprocess.STDOUT, env=env)
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                status = "timeout"
                rc = None
                # 连整棵树一起收：这些脚本会起 Tk / 子进程 / 甚至 exe
                try:
                    import proc_tree
                    proc_tree.kill_tree(p.pid)
                except Exception as e:
                    print("    (kill_tree 不可用，退回 p.kill(): %s)" % e)
                    p.kill()
                try:
                    p.wait(timeout=10)
                except Exception as e:
                    print("    (超时后进程仍未收干净，可能有残留: %s)" % e)
        if status != "timeout":
            status = "pass" if rc == 0 else "fail"
    finally:
        try:
            with open(outpath, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as e:
            text = "(读不到输出: %s)" % e
        try:
            os.remove(outpath)
        except OSError as e:
            # 删不掉无所谓（temp 目录系统会收），但不静默 —— 万一 temp 满了要知道
            print("    (临时输出文件删不掉: %s)" % e)
    return status, time.time() - t0, text


def tail(text, n=25):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def do_audit(verbose=True):
    """审计：有没有 `*_test.py` / `*_probe.py` 没被登记？

    这一条是防「新写的测试没人跑」—— 项目里 30 多个脚本「本来该跑、只是没人接」，
    就是这么攒出来的。新加测试脚本时忘了登记，这里会红。
    """
    registered = set()
    for rows in TIERS.values():
        for row in rows:
            registered.add(row[0])
    on_disk = sorted(f for f in os.listdir(HERE) if f.endswith(".py"))
    looks_like_test = [
        f for f in on_disk
        if (f.endswith("_test.py") or f.endswith("_probe.py")) and f not in NOT_TESTS
    ]
    missing = [f for f in looks_like_test if f not in registered]
    ghost = [f for f in registered if f not in on_disk]
    if verbose:
        print("审计：目录里像测试的有 %d 个，已登记 %d 个"
              % (len(looks_like_test), len(looks_like_test) - len(missing)))
        if missing:
            print("  🔴 这些测试脚本**没有被登记**，也就是说没人会跑到它们：")
            for f in missing:
                print("     %s" % f)
            print("     去 run_tests.py 里挑一档加进去（跑不跑得动都要登记，")
            print("     跑不动的放 MANUAL 并写清原因）。")
        if ghost:
            print("  🔴 登记了但文件不存在（改名/删了？）：%s" % "、".join(ghost))
        if not missing and not ghost:
            print("  ✅ 没有漏接")
    return 1 if (missing or ghost) else 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("tier", nargs="?", default="fast",
                    choices=["fast", "env", "gate", "all"])
    ap.add_argument("exe", nargs="?", default=None, help="门槛档用的 exe 路径")
    ap.add_argument("--list", action="store_true", help="列出全部用例后退出")
    ap.add_argument("--audit", action="store_true", help="只做「有没有漏接」的审计")
    ap.add_argument("-v", "--verbose", action="store_true", help="通过的用例也打输出")
    ap.add_argument("--timeout", type=int, default=None, help="每个用例的超时秒数")
    a = ap.parse_args()

    if a.audit:
        return do_audit()

    if a.list:
        for tier in ("fast", "env", "gate", "manual"):
            rows = TIERS[tier]
            auto = "**不会自动跑**" if tier == "manual" else "自动跑"
            print("\n=== %s（%d 个，%s）===" % (tier, len(rows), auto))
            for row in rows:
                script, why = row[0], row[1]
                need = row[2] if len(row) > 2 else ""
                print("  %-28s %s%s" % (script, why, ("  [要 %s]" % need) if need else ""))
        print()
        return do_audit()

    tiers = ["fast", "env", "gate"] if a.tier == "all" else [a.tier]

    problems = check_interpreter(a.tier)
    if problems:
        print("!! 解释器不满足要求：")
        for p in problems:
            print("   · %s" % p)
        print("   项目里齐的是 envs/build/Scripts/python.exe")
        return 2

    exe = None
    if "gate" in tiers:
        try:
            import paths
            exe = a.exe or paths.find_exe()
        except Exception:
            exe = a.exe
        if not exe:
            print("!! 门槛档需要一个已构建的 exe，但没找到。")
            print("   先跑 build.py，或者：python run_tests.py gate <exe路径>")
            return 2
        print("门槛档用的 exe: %s" % exe)

    timeouts = {"fast": 180, "env": 240, "gate": 300}
    results = []
    for tier in tiers:
        rows = TIERS[tier]
        print()
        print("=" * 72)
        print("=== %s 档（%d 个）===" % (tier, len(rows)))
        print("=" * 72)
        for row in rows:
            script, why = row[0], row[1]
            need = row[2] if len(row) > 2 else ""
            t = a.timeout or timeouts[tier]
            print("\n--- %s%s" % (script, ("  [要 %s]" % need) if need else ""))
            print("    %s" % why)
            status, elapsed, text = run_one(script, t, exe=exe)
            mark = {"pass": "OK  ", "fail": "失败", "timeout": "超时", "missing": "缺文件"}[status]
            print("    [%s] %.1fs" % (mark, elapsed))
            if status != "pass" or a.verbose:
                print("    " + "-" * 60)
                for ln in tail(text).splitlines():
                    print("    | %s" % ln)
                print("    " + "-" * 60)
            results.append((tier, script, status, elapsed))

    print()
    print("=" * 72)
    print("=== 汇总 ===")
    print("=" * 72)
    bad = [r for r in results if r[2] != "pass"]
    for tier, script, status, elapsed in results:
        if status != "pass":
            print("  [%s] %-28s %s" % (
                {"fail": "失败", "timeout": "超时", "missing": "缺文件"}[status],
                script, "%.1fs" % elapsed))
    n_pass = len(results) - len(bad)
    print("  %d 个通过，%d 个没通过（共 %d 个）" % (n_pass, len(bad), len(results)))
    if not bad:
        print("  ✅ 全绿")
    print()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
