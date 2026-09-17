# -*- coding: utf-8 -*-
"""给程序界面截图（不依赖 PIL —— PNG 用标准库 zlib 自己编码）。

为什么需要它：
    「界面好不好看」这一层测试是测不出来的 —— 逻辑测全绿，界面也可能排得乱七八糟
    （按钮压住输入框、文字被裁掉）。所以留一个能真的看一眼的工具。

做法：
    1. 起一个进程（不传 pid 就用 --exe 指定的程序）；
    2. 轮询等它出现可见的顶层窗口（按 PID 找，比按标题找稳）；
    3. 用 PrintWindow 让**窗口自己渲染**到内存位图 —— 只抓这个窗口，
       **不抓整个桌面**（全屏截图会把用户别的东西也拍进去）；
    4. GetDIBits 取像素 → 手写 PNG。

用法：
    python shot.py --out shot.png
    python shot.py --exe "C:\\path\\app.exe" --args "" --out shot.png
    python shot.py --pid 1234 --out shot.png        # 抓已经在跑的窗口
"""
import argparse
import ctypes
import os
import struct
import subprocess
import sys
import time
import zlib

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

CREATE_NO_WINDOW = 0x08000000


# ==================== 进程树清理 ====================
# 实现在 proc_tree.py 里 —— build.py / verify_exe.py / e2e_test.py /
# exit_stress.py 也用它，一份实现避免各处跑偏。核心事实：
# **onefile 的 exe 是两个进程** —— 启动器父进程 + 真正持有窗口的子进程，
# 而 Popen.pid 给的是父进程。只 terminate 父进程 = 留下一个孤儿窗口。
import proc_tree
import paths


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def be_dpi_aware():
    """不声明 DPI 感知的话，GetWindowRect 给的是缩放后的坐标，抓出来会错位。"""
    for fn in ("SetProcessDpiAwarenessContext", "SetProcessDPIAware"):
        try:
            f = getattr(user32, fn)
        except AttributeError:
            continue
        try:
            if fn == "SetProcessDpiAwarenessContext":
                f(ctypes.c_void_p(-4))       # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            else:
                f()
            return fn
        except Exception:
            pass
    return None


def find_window(pid, timeout=15.0):
    """等 PID 名下的第一个可见顶层窗口，返回 hwnd（超时返回 None）。"""
    found = []

    def cb(hwnd, _):
        wpid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    t0 = time.time()
    while time.time() - t0 < timeout:
        found[:] = []
        user32.EnumWindows(EnumWindowsProc(cb), None)
        if found:
            return found[0]
        time.sleep(0.3)
    return None


def png_bytes(rgb_rows, w, h):
    """把 RGB 行编码成 PNG（标准库就够，不用 PIL）。"""
    raw = b"".join(b"\x00" + row for row in rgb_rows)   # 每行前置 filter=0

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8bit truecolor
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def capture(hwnd, foreground=True, settle=0.7):
    """用 PrintWindow 抓这个窗口，返回 (png_bytes, w, h)。

    foreground=False 用于「窗口就在本进程里、而且确定已经画好了」的场景 ——
    那种情况下去抢前台焦点只会打扰用户，没必要。
    """
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise RuntimeError("GetWindowRect 失败")
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸异常: %dx%d" % (w, h))

    if foreground:
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    if settle:
        time.sleep(settle)

    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        # flags=2 (PW_RENDERFULLCONTENT)：让窗口自己渲染，被别的窗口挡住也没关系
        ok = user32.PrintWindow(hwnd, hdc_mem, 2)

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h              # 负数 = 自上而下，省得再翻行
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0          # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)
        if got == 0:
            raise RuntimeError("GetDIBits 失败（PrintWindow 返回 %s）" % ok)

        px = buf.raw
        rows = []
        for y in range(h):
            base = y * w * 4
            # DIB 是 BGRA，PNG 要 RGB
            rows.append(bytes(b for i in range(base, base + w * 4, 4)
                              for b in (px[i + 2], px[i + 1], px[i])))
        return png_bytes(rows, w, h), w, h
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def hwnd_of(tk_widget):
    """拿 Tk 顶层窗口的 HWND。

    优先用 wm_frame()：那是带标题栏的外框，截出来才是用户看到的样子。
    它返回的是十六进制字符串，解析失败就退回 winfo_id()（内容区）。
    """
    try:
        f = tk_widget.wm_frame()
        if f:
            v = int(f, 16)
            if v:
                return v
    except Exception:
        pass
    return tk_widget.winfo_id()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=paths.default_exe())
    ap.add_argument("--args", default="", help="传给程序的参数（默认空 = GUI 模式）")
    ap.add_argument("--pid", type=int, default=0, help="抓已经在跑的进程，不再新起")
    ap.add_argument("--out", default="shot.png")
    ap.add_argument("--wait", type=float, default=15.0)
    ap.add_argument("--dpi-aware", action="store_true",
                    help="只在**被截的进程自己声明了 DPI 感知**时才加。"
                         "默认不声明 —— 被测程序不感知时我们也不该感知，"
                         "否则 Tk 看到的是物理像素，窗口量出来偏大"
                         "（实测 820x848 变成 1148x1056，截出来不是用户看到的样子）")
    a = ap.parse_args()

    if a.dpi_aware:
        print("DPI 感知:", be_dpi_aware() or "（设置失败）")
    else:
        print("DPI 感知: 不声明（与被测程序保持一致）")

    proc = None
    job = None
    target_pid = a.pid
    if not a.pid:
        cmd = [a.exe] + ([a.args] if a.args else [])
        # proc_tree.start 会先建 job 再启动：启动器父进程一进去，它后来 spawn 的
        # 子进程（真正拥有窗口那个）会自动进同一个 job，收尾时整棵树一起没。
        proc, job = proc_tree.start(cmd, creationflags=0x00000008 | 0x00000200)
        target_pid = proc.pid
        print("已启动 PID = %s（启动器父进程）；job = %s"
              % (target_pid, "已接管" if job else "**没接管上，将退回 taskkill**"))
    else:
        # 抓别人已经开着的进程：只截图，**绝不能杀** —— 那不是我们起的
        print("抓已有 PID =", target_pid)

    try:
        hwnd = find_window(target_pid, a.wait)
        if not hwnd:
            print("结果: 没找到可见窗口（超时 %.0fs）" % a.wait)
            return 1
        print("hwnd =", hwnd)

        data, w, h = capture(hwnd)
        out = os.path.abspath(a.out)
        with open(out, "wb") as f:
            f.write(data)
        print("已保存: %s  %dx%d  %d 字节" % (out, w, h, len(data)))
        return 0
    finally:
        # **别只 proc.terminate()** —— 那只会杀掉启动器父进程，子进程会带着窗口
        # 变成孤儿留在用户桌面上。shutdown 关 job 句柄，整棵树一起没，
        # 并复查确认真的清干净了。
        if proc is not None:
            proc_tree.shutdown(proc, job, verbose=True)


if __name__ == "__main__":
    sys.exit(main())
