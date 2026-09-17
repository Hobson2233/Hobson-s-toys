# -*- coding: utf-8 -*-
"""起进程 / 杀进程树 / 看门狗 —— 三个容易踩同一个坑的地方，集中在这里。

## 坑 1：onefile 的 exe 是**两个**进程，而 Popen 只给你父进程

PyInstaller 的 onefile 打包结果运行时是两个进程：

    启动器父进程（解压到 _MEIxxxx，然后等子进程）
      └── 子进程（真正跑 Python 代码，**窗口属于它**）

`subprocess.Popen(...).pid`（以及 PowerShell 的 `Start-Process -PassThru`）拿到的是
**父进程**。所以 `proc.terminate()` / `proc.kill()` 只杀掉父进程，**子进程会带着窗口
活下来**，变成用户桌面上的孤儿窗口，还常被 Windows 标成「未响应」。

实测（2026-09-15）：父 31256 被 kill 之后，子 33868 仍然 `HasWindow=True`，
窗口标题照旧 —— 用户就是这么看到那个卡住的设置窗口的。

修法：把启动的进程放进一个 **Job Object** 并设 `KILL_ON_JOB_CLOSE`。子进程是父进程的
后代，会自动进同一个 job，所以关掉句柄时整棵树一起没。兜底再用 `taskkill /PID /T /F`。

**永远不要按进程名杀**（`taskkill /IM`）：用户可能自己开着同一个程序，会误伤；
而且非 ASCII 的进程名传给 taskkill 会被控制台代码页搞坏（实测中文名匹配不上，
静默失败）。

## 坑 2：`subprocess.run(timeout=N)` 超时也会留下孤儿

它超时后内部只 `kill()` 那个进程 —— 又是父进程。所以凡是「跑 exe 并等结果」的地方
（build.py / verify_exe.py / e2e_test.py / exit_stress.py）都该走本模块的 `run_exe()`。

## 坑 3：卡住的 GUI 脚本会把窗口留在桌面上

诊断脚本是排障工具，绝不该成为新的问题来源。Tk 卡住时解释器正常收尾根本不会发生，
所以 `atexit` / `try/finally` 都靠不住，只能靠 daemon 线程到点硬退。

**注意「硬退」不是 `os._exit()`**：它在 Windows 上仍走 `ExitProcess`，会执行
DLL 卸载回调，那一步本身就会慢甚至永久卡住。见下面 `hard_kill()`。

## 用法

    import proc_tree

    rc, timed_out = proc_tree.run_exe(EXE, ["--selftest"], timeout=45)

    # 或者手动控制：
    proc, job = proc_tree.start([EXE, "--auto"])
    ...
    proc_tree.shutdown(proc, job)

    disarm = proc_tree.arm_watchdog(90, "ui_shot.py")
    ...
    disarm()
    proc_tree.exit_hard(0)
"""
import ctypes
import os
import subprocess
import sys
import threading
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# HANDLE 是**指针宽度**：不声明 restype 时 ctypes 默认按 `c_int` 返回，
# 64 位下句柄值一旦超过 2^31 就会被截断成负数（`if not h` 也就失去意义）。
# 这里把返回 HANDLE 的几个函数声明清楚。
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateJobObjectW.restype = ctypes.c_void_p
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p]
kernel32.GetProcessTimes.restype = ctypes.c_int
kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
kernel32.TerminateProcess.restype = ctypes.c_int

CREATE_NO_WINDOW = 0x08000000
DEVNULL = subprocess.DEVNULL

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

# 看门狗触发时的退出码。故意和「断言失败(1)」分开，好判断到底是代码错还是卡住。
WATCHDOG_EXIT = 4


class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]



class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),      # ULONG_PTR，64 位下必须用 size_t
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _EXT_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


# ==================== 进程树 ====================

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260)]


def snapshot():
    """返回 {pid: ppid}。用 Toolhelp32 —— 不依赖 WMI/PowerShell，也不怕中文进程名。"""
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value
    h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h == INVALID:
        return {}
    out = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = e.th32ParentProcessID
            ok = kernel32.Process32NextW(h, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(h)
    return out


def children_of(pid):
    """pid 的直接子进程列表。"""
    return [p for p, pp in snapshot().items() if pp == pid]


def snapshot_names():
    """返回 {pid: 进程名}（含 .exe 后缀）。

    ⚠️ **不要拿 `snapshot()` 的返回值按名字过滤** —— 它给的是 `{pid: 父pid}`，
    拿父 pid 去比 `'python.exe'` 永远不相等，过滤器恒空，检查就变成**空转**：
    「没找到残留进程」既可能是真干净、也可能是过滤器坏了，两者输出一模一样。
    2026-09-17 踩过（看门狗残留检查空转了一轮）。要按名字找就用这个函数。
    """
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value
    h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h == INVALID:
        return {}
    out = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = e.szExeFile
            ok = kernel32.Process32NextW(h, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(h)
    return out


def find_by_name(name, exclude=()):
    """按进程名（不含 .exe 也行）找 pid 列表，方便做「有没有残留」的检查。

    ⚠️ **用 venv 里的解释器跑脚本时，一个「python.exe」其实是两个同名进程**：
    venv 的 `python.exe` / `pythonw.exe` 只是 redirector，它自己会再
    `CreateProcess` 出真解释器（实测：venv 起 1 个 → 新增 2 个同名进程；
    managed 解释器起 1 个 → 新增 1 个）。所以：

    - **按名字数个数会多算一倍**，`len()` 不能当「起了几个」用；
    - `Popen.pid` 拿到的是 **redirector**，不是跑代码的那个（真解释器是它的子进程）；
    - 好在 redirector 用 Job Object 绑着子进程，**kill 掉壳，真解释器跟着没**
      （实测 0.6s 内消失），所以「kill 完再查有没有残留」仍然成立。

    做「残留检查」时正确写法是**前后做差**（`after - before`），不是看绝对值。
    """
    want = name.lower()
    if not want.endswith(".exe"):
        want += ".exe"
    skip = set(exclude)
    return sorted(p for p, n in snapshot_names().items()
                  if str(n).lower() == want and p not in skip)


def wait_done(proc, timeout=60):
    """等「真正跑代码的那个进程」结束。返回 (是否按时结束, 退出码 或 None)。

    **不要等 `proc`（= Popen.pid）**：onefile 里那是**启动器父进程**。子进程退出后
    它还要递归删掉 `_MEIxxxx`，而 Windows 上杀软正在扫这些刚解压出来的文件、握着
    句柄时，删除会一直重试 —— 父进程能挂很久甚至根本不退（实测 `--guitest` 有时
    2 秒完成、有时 60 秒以上不返回、有时一直挂着）。
    **那时我们的程序其实早就干完并且退出了**（标记文件都写好了）。

    所以判据是「**子进程出现又消失**」。为此一发现子进程就先 OpenProcess 拿住句柄，
    这样它死后还能读到真正的退出码（不拿句柄的话 pid 可能已经被回收）。
    """
    t0 = time.time()
    child_h = None
    child_pid = 0

    # 1) 先等子进程出现（不是 onefile 的话，父进程自己就是干活的那个）
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return True, proc.returncode          # 单进程，父进程直接退了
        kids = children_of(proc.pid)
        if kids:
            child_pid = kids[0]
            child_h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                           False, child_pid)
            break
        time.sleep(0.05)

    if not child_pid:
        # 一直没等到子进程：退回等父进程
        try:
            proc.wait(timeout=max(0.1, timeout - (time.time() - t0)))
            return True, proc.returncode
        except subprocess.TimeoutExpired:
            return False, None

    # 2) 等子进程结束
    try:
        while time.time() - t0 < timeout:
            if not alive(child_pid):
                code = ctypes.c_ulong()
                if child_h and kernel32.GetExitCodeProcess(child_h, ctypes.byref(code)):
                    return True, code.value
                return True, None
            time.sleep(0.05)
        return False, None
    finally:
        if child_h:
            kernel32.CloseHandle(child_h)


def make_job():
    """建一个「关掉句柄就杀掉里面所有进程」的 Job Object。失败返回 None。"""
    try:
        job = kernel32.CreateJobObjectW(None, None)
    except Exception:
        return None
    if not job:
        return None
    info = _EXT_LIMIT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    return job


def join_job(job, pid):
    """把已经在跑的进程放进 job。系统拒绝（比如已有不允许嵌套的 job）就 False。"""
    if not job or pid <= 0:
        return False
    h = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, h))
    finally:
        kernel32.CloseHandle(h)


def close_job(job):
    """关句柄 —— KILL_ON_JOB_CLOSE 会顺带把整棵进程树杀掉。"""
    if job:
        try:
            kernel32.CloseHandle(job)
        except Exception:
            pass


def alive(pid):
    """这个 pid 还活着吗（只要 QUERY_LIMITED 权限，不需要 PROCESS_ALL）。"""
    if pid <= 0:
        return False
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(h)


def kill_tree(pid, wait=8.0):
    """兜底：taskkill /T 杀整棵树。返回是否确认清干净了。"""
    if pid <= 0:
        return True
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=DEVNULL, stderr=DEVNULL,
                       timeout=15, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < wait:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return not alive(pid)


def start(cmd, **kw):
    """Popen + 把新进程放进 kill-on-close 的 job。返回 (proc, job)。

    `job` 为 None 表示系统没让我们接管 —— 那种情况收尾要退回 `kill_tree(proc.pid)`。
    """
    kw.setdefault("stdout", DEVNULL)
    kw.setdefault("stderr", DEVNULL)
    job = make_job()
    proc = subprocess.Popen(cmd, **kw)
    if not join_job(job, proc.pid):
        job = None
    return proc, job


def shutdown(proc, job, verbose=False):
    """杀掉 proc 以及它生出来的所有进程。返回是否确认清干净了。"""
    if proc is None:
        return True
    close_job(job)
    if job is None:
        kill_tree(proc.pid)
    try:
        proc.wait(timeout=10)
    except Exception:
        kill_tree(proc.pid)
    left = alive(proc.pid)
    if verbose:
        print("已关闭程序" if not left
              else "**警告：进程 %s 还在，可能有孤儿窗口**" % proc.pid)
    return not left


def run_exe(exe, args=(), timeout=60, cwd=None):
    """跑 exe 等它结束。返回 (退出码 或 None, 是否超时)。

    **别用 subprocess.run(timeout=...)** —— 两个原因：
      1. 它超时后内部只 kill 启动器父进程，子进程会变成孤儿；
      2. 它等的是启动器父进程，而父进程做完活之后可能挂在删 _MEI 上（见 wait_done），
         于是明明已经跑完的调用被误判成「超时」。

    这里等的是**真正干活的那个进程**，超时再把整棵树收掉。
    """
    proc, job = start([exe] + list(args), cwd=cwd)
    try:
        done, code = wait_done(proc, timeout)
        return code, (not done)
    finally:
        # 收尾顺手把还挂着的启动器父进程一起收掉，不留残余进程
        shutdown(proc, job)


def sweep_mei_temp(min_age=600, verbose=True):
    """清掉 PyInstaller 留下的 `_MEIxxxx` 解压目录，返回删除个数。

    **为什么会有残留**：onefile 的启动器父进程负责在退出时删掉 `_MEIxxxx`。
    强杀进程树时（`shutdown` 就是强杀）父进程一起没了，于是解压目录留在 TEMP 里。
    正常退出的进程不会泄漏。实测一次密集排障下来能攒到 179 MB。

    **判断「还能不能用」靠先改名，不是靠直接删。** 直接 `rmtree` 很危险：
    Windows 只锁**已打开的文件句柄**，正在运行的程序里那些还没读到的文件
    （比如 Tk 之后才会加载的 tcl 脚本）是可以删掉的 —— 于是一边删一边把
    运行中的程序搞崩。而**目录里有任何文件被打开着，改名就会失败**，
    所以「改名成功 = 这个目录确实没人用」才是可靠判据。改完名再删。
    另外还加一个时间门槛，避免碰到刚起来的进程。

    **跳过分两种，报告里分开写**（`太新 N / 还在用 N`）—— 处置完全相反：
    「太新」等一会儿或把 `min_age` 降下来就能删；「还在用」说明真有进程握着
    句柄，别硬删。合并成一句「还在用/太新」会让人不知道该等还是该收手。

    **这笔开销有多大**（09-17 实测）：每个 `_MEIxxxx` 解压出来 **17.3 MB**，
    一轮回归（`exit_stress` / `exit_regression_test` / `proc_tree_test` 都会强杀进程树）
    能攒 **17~27 个，约 300~470 MB**。`proc_tree_test.py` 结尾会自己调一次
    `sweep_mei_temp(min_age=600)`，所以那批「已经放了 10 分钟以上」的会被它清掉。
    """
    import shutil
    import tempfile
    root = tempfile.gettempdir()
    now = time.time()
    removed = 0
    skipped_new = 0        # 太新（没过 min_age 门槛）
    skipped_busy = 0       # 改名失败 = 目录里还有文件被打开着
    try:
        names = [n for n in os.listdir(root) if n.startswith("_MEI")]
    except Exception:
        return 0
    for n in names:
        p = os.path.join(root, n)
        if not os.path.isdir(p):
            continue
        try:
            if now - os.path.getmtime(p) < min_age:
                skipped_new += 1
                continue
        except Exception:
            continue
        # 先改名探活：改得动说明目录里没有任何文件被占用，才敢删
        dead = p + ".del"
        try:
            os.rename(p, dead)
        except OSError:
            skipped_busy += 1     # 还在用（或有权限问题）—— 跳过就是，别报错
            continue
        try:
            shutil.rmtree(dead)
        except Exception:
            pass                  # 改名已经成功了，删不干净也不算「还在用」
        removed += 1
    if verbose and (removed or skipped_new or skipped_busy):
        # 两种跳过要**分开报**：它们的意思和下一步完全不同，合并成一句
        # 「还在用/太新」会让人不知道该等一等还是该收手（09-17 为此多查了两步）。
        print("清理 _MEI 残留: 删掉 %d 个，跳过 %d 个（太新 %d / 还在用 %d）"
              % (removed, skipped_new + skipped_busy, skipped_new, skipped_busy))
    return removed


def mei_usage():
    """返回 (目录个数, 占用字节数)。排查时先看这个数，涨了说明有强杀留下的。"""
    import tempfile
    root = tempfile.gettempdir()
    count = 0
    total = 0
    try:
        names = [n for n in os.listdir(root) if n.startswith("_MEI")]
    except Exception:
        return 0, 0
    for n in names:
        p = os.path.join(root, n)
        if not os.path.isdir(p):
            continue
        count += 1
        for dirpath, _dirnames, filenames in os.walk(p):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except Exception:
                    pass
    return count, total


# ==================== 看门狗 ====================

def hard_kill(code):
    """不执行 DLL 卸载回调地把本进程干掉。

    **`os._exit()` 不是「硬退」**：它在 Windows 上走 CRT `_exit` → `ExitProcess`，
    而 ExitProcess 会依次调用所有已加载 DLL 的 `DLL_PROCESS_DETACH` ——
    Tcl/Tk 的清理既慢（实测 11~12 秒）又可能**永远卡住**。

    2026-09-17 用插桩（`exit_trace_probe.py`，程序内按阶段打点）实测：
    日志停在「即将硬退」之后再无输出，faulthandler 的 20 秒定时器线程也被
    ExitProcess 提前终止（dump 文件 0 字节）→ 卡点确定在 ExitProcess 的
    C 层，而不是 Python 层。所以下面这段旧注释里「谁也救不了」的说法是**错的**：

        （旧注释）「它救命的手段同样是 os._exit()；如果连 os._exit() 都卡在
         DLL 清理里，那就没有别的办法了。」

    `TerminateProcess` 不走 DLL 卸载，因此没有这个死面。这就是那条出路。

    注意：campus_login.py 里有一份等价实现（`hard_kill`），**故意重复**——
    应用本体不能依赖本模块（本模块是排障工具，不随 exe 打包）。
    """
    try:
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code & 0xFFFFFFFF)
    except Exception:
        pass
    # 成功的话不会走到这里。兜底：退回 os._exit（在非 Windows 上也是这条路）。
    os._exit(code)


def _creation_time(pid):
    """进程创建时间（FILETIME 的 100ns 计数）。取不到返回 None。

    用途：看门狗要确认「这个 PID 还是当初那个进程」。**PID 会被系统回收**，
    如果看门狗只是 sleep 到点就按 PID 杀，遇到目标早已退出、PID 被别人复用
    的情况就会**误杀无关进程** —— 那比不杀严重得多。
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, kt, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        ok = kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                      ctypes.byref(kt), ctypes.byref(u))
        return ((c.hi << 32) | c.lo) if ok else None
    finally:
        kernel32.CloseHandle(h)


def watchdog_child_main():
    """看门狗**进程**的主体：睡够时间后按 PID 硬杀目标。由 `arm_watchdog()` 拉起。

    为什么必须是独立进程、不能用线程：
        主线程若卡在**持 GIL 的 C 调用**里（Tk 卡住正是这种），看门狗线程
        `time.sleep` 醒来后永远抢不到 GIL，**到点也不会触发**。而它存在的意义
        恰恰是这种卡法。2026-09-17 实测（`watchdog_gil_test.py --synthetic`，
        用 `setswitchinterval(1000)` + 忙循环确定性占住 GIL）：线程版 15 秒看门狗
        **一次都没触发**，子进程活过 25 秒只能外部杀。独立进程没有 GIL 依赖。

    参数（`sys.argv`，因为是用 `python -c` 起的）：
        argv[1] 目标 pid / argv[2] 秒数 / argv[3] 退出码 /
        argv[4] 目标创建时间 / argv[5] 备注
    """
    pid = int(sys.argv[1])
    secs = float(sys.argv[2])
    code = int(sys.argv[3])
    created = int(sys.argv[4])
    note = sys.argv[5]
    time.sleep(secs)
    if _creation_time(pid) != created:
        sys.stderr.write(
            "\n[看门狗] 目标进程 %s 已经不在了（或 PID 已被复用），不动手。\n" % pid)
        return 0
    try:
        sys.stderr.write(
            "\n[看门狗] %s 秒内没结束，强制退出（%s）\n"
            "         —— 窗口不会留在桌面上。断言多半已经跑完了，\n"
            "            看上面的输出判断结果。\n"
            % (secs, note or "多半是 Tk 卡住"))
        sys.stderr.flush()
    except Exception:
        pass
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if h:
        try:
            kernel32.TerminateProcess(h, code & 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(h)
    return 0


def _arm_thread_watchdog(seconds, note):
    """退化路径：起不了独立进程时（例如被冻结打包）用线程版。

    ⚠️ 线程版在「主线程占着 GIL」时**救不了**（见 `watchdog_child_main` 的说明），
    所以它只是「聊胜于无」，不是等价替代。
    """
    state = {"done": False}

    def worker():
        time.sleep(seconds)
        if state["done"]:
            return
        try:
            sys.stderr.write(
                "\n[看门狗-线程版] %s 秒内没结束，强制退出（%s）\n" % (seconds, note))
            sys.stderr.flush()
        except Exception:
            pass
        hard_kill(WATCHDOG_EXIT)

    threading.Thread(target=worker, name="watchdog", daemon=True).start()

    def disarm():
        state["done"] = True

    return disarm


def arm_watchdog(seconds, note=""):
    """到点还没解除就强制退出。返回一个「解除」函数（可重复调用）。

    Tk 卡住时解释器正常收尾根本不会发生，所以 atexit / try/finally 都靠不住 ——
    只能另起一个**独立进程**到点硬退，才拦得住「窗口留在用户桌面上」。
    （为什么不线程：见 `watchdog_child_main` 的说明 —— 线程版抢不到 GIL。）
    """
    src = ("import sys\n"
           "sys.path.insert(0, %r)\n"
           "import proc_tree\n"
           "sys.exit(proc_tree.watchdog_child_main())\n"
           % os.path.dirname(os.path.abspath(__file__)))
    proc = None
    if not getattr(sys, "frozen", False):
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", src,
                 str(os.getpid()), str(seconds), str(WATCHDOG_EXIT),
                 str(_creation_time(os.getpid())), note],
                stdout=DEVNULL, creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            # ⚠️ 这里**不能一声不吭**。`proc is None` 有两种来源，处置完全不同：
            #   · 打包后（frozen）—— 预期内，没别的解释器可用，退化是设计好的；
            #   · Popen 真的失败（杀软拦截、解释器被挪走……）—— **意外**，
            #     而退化后的线程版在「主线程占着 GIL」时不会触发，那恰恰是
            #     看门狗唯一存在的理由。静默退化 = 安全网悄悄消失。
            # 测试脚本的 stderr 是继承来的真句柄（pythonw.exe 下也一样，实测），
            # 所以这行留声是真的有人看得见。
            proc = None
            try:
                sys.stderr.write(
                    "\n[看门狗] ⚠️ 独立进程起不来（%s: %s），退化成线程版 ——\n"
                    "         线程版在主线程占着 GIL 时**不会触发**，别把它当安全网。\n"
                    % (type(exc).__name__, exc))
                sys.stderr.flush()
            except Exception:
                pass
    if proc is None:
        return _arm_thread_watchdog(seconds, note)

    # 把看门狗放进一个 KILL_ON_JOB_CLOSE 的 job，**并且故意不关句柄**：
    # 父进程一死（正常退、硬退、被看门狗杀掉都算），它持有的 job 句柄被系统关掉
    # → job 关闭 → 看门狗跟着没。否则脚本用 exit_hard() 结束时不会调 disarm()，
    # 看门狗会一直挂到超时才自己退出，白留一个进程。
    # 系统不让嵌套 job 时 join_job 返回 False —— 那种情况下看门狗仍会超时自退。
    job = make_job()
    if job:
        join_job(job, proc.pid)

    def disarm():
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    return disarm


def exit_hard(code):
    """不走解释器正常收尾地退出，绕开 Tk 收尾阶段的死锁。

    **但它不是瞬间的，也不是万无一失的。** 2026-09-16 实测（`_t_destroy.py` 三组对照）：

        A 建 Tk 窗口 -> os._exit()            退出，但耗时 12 秒
        B 建 Tk 窗口 -> destroy() -> os._exit()  退出，耗时 11 秒
        C 建 Tk 窗口 -> destroy() -> sys.exit()  **挂死**

    为什么 A/B 也要 11~12 秒：`os._exit()` 在 Windows 上走 `ExitProcess`，
    它会等所有已加载 DLL 的 `DLL_PROCESS_DETACH` 跑完 —— Tcl/Tk 的清理
    就是慢（而且不可靠）。正常收尾（C）会走更多 Tk 关闭逻辑，直接卡死。

    **2026-09-17 修正：上面那段的成因是对的，但推论错了。**
    当时的推论是「看门狗不是绝对保证，卡在 DLL 清理里谁也救不了」，
    于是继续用 `os._exit` —— 而「慢」和「卡住」本来就是同一件事（DLL 卸载），
    两者都没有出路。真正的出路是**根本不执行 DLL 卸载**：`TerminateProcess`
    不做这一步，所以既快（毫秒级）又不会卡。现在本函数走 `hard_kill()`。

    原判断被推翻的经过（含插桩证据）见 `.workbuddy-ai/memory/2026-09-17.md`。

    安全前提：所有文件写入都用 `with open(...)` 即时落盘，没有待刷的缓冲。
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    hard_kill(code)
