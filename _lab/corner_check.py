# -*- coding: utf-8 -*-
"""验证「窗口外框圆角」到底有没有生效 —— 用**对照实验**，不靠肉眼猜。

为什么必须单独验证（2026-10-17）：
    `ui_shot.py` 的截图走 `PrintWindow`，抓的是**窗口自己渲染出来的内容**；
    而 DWM 的圆角是在**合成阶段**加的效果 —— PrintWindow 抓不到它，
    所以那种截图里四角永远是完整的矩形。
    ⇒ 「截图里看不出圆角」**既不能证明它没生效，也不能证明它生效了**。
       要验证只能**抓屏幕**（BitBlt 桌面 DC），因为那才是用户真正看到的东西。

对照实验的做法：
    同一个窗口，先把圆角偏好设成 `DONOTROUND(1)` 抓一张、再设成 `ROUND(2)` 抓一张。
      判据：**直角**时四角那几个像素属于窗口自身（和同行内侧的像素同色）；
            **圆角**时它们属于窗口外的桌面（和内侧像素明显不同色）。
    两张一对比，结论没法含糊 —— 比「我觉得好像圆了」可靠得多。

⚠️ 窗口必须在前台且没被别的窗口挡住：抓屏幕抓的是屏幕上**实际显示**的像素。
⚠️ 纯只读验证：数据目录先整个换到临时目录（和 ui_shot 同一套做法），
   跑完不会在真实数据上留下任何痕迹。

用法：
    python _lab/corner_check.py
"""
import ctypes
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import campus_login as C        # noqa: E402
import proc_tree                # noqa: E402
import shot                     # noqa: E402

SRCCOPY = 0x00CC0020
DWMWA_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

DEMO_DIR = os.path.join(tempfile.gettempdir(), "campus_corner_check")


def sandbox():
    """把数据目录换到临时目录（照抄 ui_shot 的做法，只写假数据）。

    ⚠️ `campus_login` 在**导入期**就把四个路径常量算好了，所以光改 `_DATA_DIR`
       不够 —— 那四个常量也得一起重指。
    """
    os.makedirs(DEMO_DIR, exist_ok=True)
    C._DATA_DIR[:] = [DEMO_DIR]
    C.CONFIG_FILE = os.path.join(DEMO_DIR, "config.json")
    C.ACCOUNTS_FILE = os.path.join(DEMO_DIR, "accounts.json")
    C.LOG_FILE = os.path.join(DEMO_DIR, "campus-login.log")
    C.RESULT_FILE = os.path.join(DEMO_DIR, "last-result.json")
    uid = "2026000001"
    C.save_config({"userId": uid, "passwd": "demo-1234"})
    C.save_accounts({"accounts": {uid: {"passwd": "demo-1234",
                                        "lastSuccess": "2026-01-01 09:00:00",
                                        "lastAttempt": "2026-01-01 09:00:00",
                                        "lastResult": "ok", "school": "zua"}},
                     "lastOnline": uid})
    print("沙箱数据目录: %s" % DEMO_DIR)


def set_corner(hwnd, pref):
    """设窗口圆角偏好，返回 HRESULT。"""
    v = ctypes.c_int(pref)
    return ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_CORNER_PREFERENCE, ctypes.byref(v), ctypes.sizeof(v))


def screen_grab(hwnd):
    """用**屏幕** DC 抓窗口所在的矩形。返回 (取像素函数, 宽, 高)。

    和 `shot.capture` 唯一的差别就是这一处：位图来源是屏幕 DC（BitBlt），
    而不是 `PrintWindow`。所以能拿到 DWM 合成之后的真实画面。
    """
    r = shot.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise RuntimeError("GetWindowRect 失败")
    w, h = r.right - r.left, r.bottom - r.top
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, r.left, r.top, SRCCOPY):
            raise RuntimeError("BitBlt 失败")
        bi = shot.BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(shot.BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h                # 负数 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0) == 0:
            raise RuntimeError("GetDIBits 失败")
        raw = buf.raw

        def at(x, y):
            i = (y * w + x) * 4
            return (raw[i + 2], raw[i + 1], raw[i])      # BGRA -> RGB

        rows = []
        for y in range(h):
            base = y * w * 4
            rows.append(bytes(b for i in range(base, base + w * 4, 4)
                              for b in (raw[i + 2], raw[i + 1], raw[i])))
        return at, w, h, shot.png_bytes(rows, w, h)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def diff(c1, c2):
    return max(abs(c1[i] - c2[i]) for i in range(3))


def probe(at, w, h):
    """看四角：角落像素 vs 同行内侧像素。返回「看起来是圆角」的角数。"""
    n_round = 0
    inset = 46                                  # 内侧参照点离边缘多远（躲开圆角半径）
    for name, (cx, cy), (nx, ny) in (
            ("左上", (1, 1), (inset, 1)),
            ("右上", (w - 2, 1), (w - 1 - inset, 1)),
            ("左下", (1, h - 2), (inset, h - 2)),
            ("右下", (w - 2, h - 2), (w - 1 - inset, h - 2))):
        c, n = at(cx, cy), at(nx, ny)
        d = diff(c, n)
        # 阈值 20：直角时两者同色（差 0~2，抗锯齿/渐变最多几个点）；
        # 圆角时角落是桌面壁纸，和标题栏/内容区几乎不可能这么接近。
        rounded = d > 20
        n_round += 1 if rounded else 0
        print("    %s角  角落 #%02x%02x%02x   内侧 #%02x%02x%02x   色差 %-3d  %s"
              % (name, c[0], c[1], c[2], n[0], n[1], n[2], d,
                 "角落是桌面 → 圆角" if rounded else "角落是窗口自己 → 直角"))
    return n_round


# 界面里出现得最多、最能代表「这是我们自己画的窗口」的几个底色。
# ⚠️ 从 palette 取，别抄字面量 —— 换配色时抄的值会静默失效（红线 58）。
def _own_colors():
    from palette import BG, CARD, NAV_BG, LINE, FIELD
    return (BG, CARD, NAV_BG, LINE, FIELD)


def _hex2rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def is_our_window(at, w, h):
    """抽一批窗口内的点，看有多少比例落在我们自己的底色上。

    🔴 为什么必须有这道自检（2026-10-17 实测踩到）：
        第一次跑的时候 `SetForegroundWindow` 没抢到前台，BitBlt 抓到的是
        **桌面上的别的东西**（角落是 `#2977d7` 这种压根不属于本界面的颜色）。
        而脚本照样把四角算了一遍、照样打印出「圆角/直角」——
        **把错误说成了事实**，这正是本项目最忌讳的失败方式。
        所以先确认「抓到的确实是我们这个窗口」，再谈圆角。
    """
    want = [_hex2rgb(c) for c in _own_colors()]
    hit = 0
    total = 0
    # 避开标题栏（顶部约 40 物理像素）和边缘，只在客户区里抽样
    for fy in (0.25, 0.4, 0.55, 0.7, 0.85):
        for fx in (0.2, 0.35, 0.5, 0.65, 0.8):
            c = at(int(w * fx), int(h * fy))
            total += 1
            if min(diff(c, wc) for wc in want) <= 12:
                hit += 1
    return hit / float(total), hit, total


def main():
    shot.be_dpi_aware()
    sandbox()

    apps = []
    _orig_init = C.LoginApp.__init__

    def _init(self, *a, **k):
        _orig_init(self, *a, **k)
        apps.append(self)
        self.root.after(3000, work)

    def work():
        try:
            root = apps[0].root
            # 🔴 抓屏幕要的是「屏幕上真实显示的样子」，所以窗口必须**真的在最上面**。
            #    光调 SetForegroundWindow 是不够的：从后台进程调用它常常被系统
            #    拒绝（前台窗口锁定），第一次跑就是这么抓到别人家的像素的。
            #    置顶（-topmost）绕开这个限制。
            root.attributes("-topmost", True)
            root.lift()
            root.focus_force()
            root.update_idletasks()
            time.sleep(1.5)

            hwnd = shot.hwnd_of(root)
            outdir = os.path.join(HERE, "out")
            os.makedirs(outdir, exist_ok=True)
            results = {}
            for label, pref, tag in (
                    ("DONOTROUND(1) 强制直角", DWMWCP_DONOTROUND, "donotround"),
                    ("ROUND(2)     圆角", DWMWCP_ROUND, "round")):
                hr = set_corner(hwnd, pref)
                root.update_idletasks()
                time.sleep(1.2)                 # 等 DWM 重画
                at, w, h, png = screen_grab(hwnd)
                p = os.path.join(outdir, "corner_%s.png" % tag)
                with open(p, "wb") as f:
                    f.write(png)
                print("\n[%s]  DwmSetWindowAttribute hr=%s   抓屏 %dx%d  已存 %s"
                      % (label, hr, w, h, os.path.relpath(p, REPO)))
                ratio, hit, total = is_our_window(at, w, h)
                print("    自检：窗口内抽样 %d 点，%d 点落在本界面底色上（%.0f%%）"
                      % (total, hit, ratio * 100))
                if ratio < 0.6:
                    print("    ❌ 抓到的**不是我们的窗口**（大概被别的窗口挡住了）。")
                    print("       这份数据不能用来判断圆角 —— 直接作废，不猜。")
                    results[pref] = None
                    continue
                results[pref] = probe(at, w, h)
            print("\n=== 结论 ===")
            if None in results.values():
                print("  ⚠️ 有一次没抓到自己的窗口，本次**不作结论**（不把错误说成事实）。")
            else:
                print("  强制直角：%d/4 个角看着是圆角（期望 0）"
                      % results[DWMWCP_DONOTROUND])
                print("  圆角偏好：%d/4 个角看着是圆角（期望 4）" % results[DWMWCP_ROUND])
                if results[DWMWCP_DONOTROUND] == 0 and results[DWMWCP_ROUND] == 4:
                    print("  ✅ 圆角确实生效，而且能靠偏好开关控制")
                elif results[DWMWCP_ROUND] == 4:
                    print("  ℹ️ 圆角一直是圆的：DONOTROUND 也是圆的 ⇒ 这个系统上"
                          "窗口**本来就带圆角**（Win11 的默认行为），我们的设置只是显式化。")
                else:
                    print("  ⚠️ 圆角**没生效** —— 抓到的窗口是直角，得查 API 那条链。")
        except Exception:
            import traceback
            print("验证失败:\n%s" % traceback.format_exc())
        finally:
            try:
                apps[0].root.quit()
            except Exception:
                pass

    C.LoginApp.__init__ = _init
    try:
        C.run_gui()
    finally:
        C.LoginApp.__init__ = _orig_init
    proc_tree.exit_hard(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
