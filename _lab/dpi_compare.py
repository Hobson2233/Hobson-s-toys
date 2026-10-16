# -*- coding: utf-8 -*-
"""一次性工具：拍「高 DPI 前 vs 后」的**同一段界面**，用来肉眼对比清晰度 + 字号。

为什么需要它：
    用户反馈「文字不太清晰 / 字太小」，改完之后**必须能证明真的变好了**。
    测试只能验逻辑（字号档位算得对不对、窗口尺寸对不对），验不了
    「看着锐不锐利」—— 那只能靠眼睛。所以同一段界面渲染两次：

      before = 进程**不开** DPI 感知（= 旧版本的行为）+ 旧字号 10
      after  = 当前版本（开 DPI 感知 + 按物理高度取字号，本机 14）

    ⚠️ 「不开 DPI 感知」是**进程级**的开关，一旦开了就撤不回来，
       所以只能起两个进程分别跑 —— 这就是本脚本用环境变量分档的原因。

不联网、不碰真实数据（`datasafe.sandbox()`）。
⚠️ 这是**对比实验的复现工具**，不是程序的一部分，别被 `campus_login` 调用。

用法：
    DPI_MODE=before  python _lab/dpi_compare.py    # 拍「改前」
    DPI_MODE=after   python _lab/dpi_compare.py    # 拍「改后」
    DPI_MODE=compose python _lab/dpi_compare.py    # 合成对比图
输出：
    repo/ui_shots/_dpi_before.png / _dpi_after.png / _dpi_对比.png

🔴 合成图为什么要「把 before 放大 1.5 倍」：
    改前的位图是**逻辑分辨率**（716px 宽），屏幕上看到的是 Windows 把它
    放大 1.5 倍之后的样子 —— 这就是「发虚」的来源。`PrintWindow` 抓到的是
    窗口**自己渲染的那张原始位图**（不虚），所以必须手动按同样的比例放大，
    才等价于用户眼睛看到的效果。用**双线性**插值模拟 Windows 的缩放器。
"""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

MODE = os.environ.get("DPI_MODE", "after").strip().lower()
if MODE not in ("before", "after", "compose"):
    raise SystemExit("DPI_MODE 只能是 before / after / compose，实际 %r" % MODE)

# ---- compose 档：只做图像处理，不建窗口、不 import campus_login ----
if MODE == "compose":
    import png_zoom as Z

    OUT = os.path.join(REPO, "ui_shots")
    # 裁切区域（在 after 图的坐标系里）—— 正好框住两行字
    CX, CY, CW, CH = 45, 95, 700, 115
    SCALE = 3


    def _up_bilinear(w, h, rgb, factor):
        """按 factor 放大（双线性）—— 模拟 Windows 对非 DPI 感知窗口的位图缩放。"""
        ow, oh = int(round(w * factor)), int(round(h * factor))
        out = bytearray(ow * oh * 3)
        for j in range(oh):
            sy = j / factor
            y0 = min(int(sy), h - 1)
            y1 = min(y0 + 1, h - 1)
            fy = sy - y0
            for i in range(ow):
                sx = i / factor
                x0 = min(int(sx), w - 1)
                x1 = min(x0 + 1, w - 1)
                fx = sx - x0
                for c in range(3):
                    p00 = rgb[(y0 * w + x0) * 3 + c]
                    p01 = rgb[(y0 * w + x1) * 3 + c]
                    p10 = rgb[(y1 * w + x0) * 3 + c]
                    p11 = rgb[(y1 * w + x1) * 3 + c]
                    top = p00 + (p01 - p00) * fx
                    bot = p10 + (p11 - p10) * fx
                    out[(j * ow + i) * 3 + c] = int(round(top + (bot - top) * fy))
        return ow, oh, out


    def _crop_zoom(w, h, rgb, x, y, cw, ch, scale):
        ow, oh, out = Z.crop_zoom(w, h, rgb, x, y, cw, ch, scale)
        return ow, oh, out


    def compose():
        w1, h1, n1, rgb1 = Z.read_png(os.path.join(OUT, "_dpi_before.png"))
        w2, h2, n2, rgb2 = Z.read_png(os.path.join(OUT, "_dpi_after.png"))
        rgb1 = Z.to_rgb(w1, h1, n1, rgb1)
        rgb2 = Z.to_rgb(w2, h2, n2, rgb2)
        # 改前：模拟 Windows 的 1.5 倍位图放大（本机缩放就是 150%）
        bw, bh, brgb = _up_bilinear(w1, h1, rgb1, 1.5)
        print("  改前原始 %dx%d → 放大 1.5x → %dx%d" % (w1, h1, bw, bh))
        print("  改后原始 %dx%d（本来就是物理像素）" % (w2, h2))

        cw, ch, top = _crop_zoom(bw, bh, brgb, CX, CY, CW, CH, SCALE)
        _cw2, _ch2, bot = _crop_zoom(w2, h2, rgb2, CX, CY, CW, CH, SCALE)

        gap = 6
        ow, oh = cw, ch * 2 + gap
        out = bytearray(ow * oh * 3)
        for j in range(ch):
            out[j * ow * 3:(j + 1) * ow * 3] = top[j * cw * 3:(j + 1) * cw * 3]
        sep0 = ch
        for j in range(sep0, sep0 + gap):
            for i in range(ow):
                out[(j * ow + i) * 3:(j * ow + i) * 3 + 3] = b"\x94\xa3\xb8"
        for j in range(ch):
            d = (sep0 + gap + j) * ow * 3
            out[d:d + cw * 3] = bot[j * cw * 3:(j + 1) * cw * 3]

        dst = os.path.join(OUT, "_dpi_对比.png")
        Z.write_png(dst, ow, oh, out)
        print("已写出 %s  (%dx%d)  —— 上=改前(放大1.5x模拟)，下=改后，都放大 %dx"
              % (dst, ow, oh, SCALE))

    compose()
    sys.exit(0)

# ---- before 档：把 `ctypes.windll` 换成「一碰就炸」的替身 ----
# `_init_dpi()` 里 `SetProcessDpiAwareness` / `SetProcessDPIAware` 都抛异常
# ⇒ 它的外层 `except` 生效 ⇒ 返回 1.0 ⇒ 等于「没开 DPI 感知」。
# ⚠️ 只在 `import campus_login` 期间替换，之后立刻还原 ——
#    `shot` / `datasafe` 后面还要用真的 `windll`。
_real_windll = ctypes.windll
if MODE == "before":
    class _Boom(object):
        def __getattr__(self, name):
            raise OSError("DPI 感知被这个对比工具故意挡住（before 档）")

    class _FakeWindll(object):
        def __getattr__(self, name):
            return _Boom()

    ctypes.windll = _FakeWindll()

import campus_login as C          # noqa: E402
ctypes.windll = _real_windll      # noqa: E402

import tkinter as tk              # noqa: E402
import tkinter.font as tkfont     # noqa: E402

import datasafe                   # noqa: E402
import proc_tree                  # noqa: E402
import shot                       # noqa: E402

OUT = os.path.join(REPO, "ui_shots")

# before 档还要把字号按回旧值：旧 `_ui_font()` 拿到的是**逻辑**高度 1067，
# 走「< 1400 ⇒ 10」那一档。这里直接替换掉，等价于旧行为。
if MODE == "before":
    C._ui_font = lambda root, tkfont_: ("Microsoft YaHei UI", 10)


def build(root, fam, base):
    """搭一段「像主界面」的内容：同一套配色 + 同一个圆角卡片 + 真实字号。"""
    page = tk.Frame(root, bg=C.BG)
    page.pack(fill="both", expand=True)

    cv, body = C.round_panel(page, tk, C.OUTER_PAD, C.INNER_PAD, C.dp(14),
                             stretch=True)
    cv.pack(fill="x", padx=C.OUTER_PAD, pady=C.OUTER_PAD)

    tk.Label(body, text="已连接校园网", font=(fam, base), bg=C.CARD, fg=C.FG,
             anchor="w").pack(fill="x", padx=C.INNER_PAD,
                              pady=(C.INNER_PAD, 0))
    tk.Label(body, text="账号 2507****30    上行 1.24 GB    下行 8.61 GB",
             font=(fam, base), bg=C.CARD, fg=C.SUB,
             anchor="w").pack(fill="x", padx=C.INNER_PAD,
                              pady=(C.dp(6), C.INNER_PAD))
    return body


def main():
    datasafe.sandbox(copy_real=False, tag="dpi_compare")

    root = tk.Tk()
    root.title("（DPI 对比实验用，不是真界面）")
    root.geometry("%dx%d+40+40" % (C.dp(700), C.dp(150)))
    root.update()

    fam, base = C._ui_font(root, tkfont)
    build(root, fam, base)
    root.update()

    name = "_dpi_%s.png" % MODE
    hwnd = shot.hwnd_of(root)
    png, w, h = shot.capture(hwnd, foreground=True, settle=0.5)
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(png)

    print("MODE=%s  UI_SCALE=%.3f  字号=%d  %s  %dx%d  %d 字节"
          % (MODE, C.UI_SCALE, base, name, w, h, len(png)))
    print("  屏幕读数 winfo_screenwidth=%d  height=%d  tk_scaling=%.3f"
          % (root.winfo_screenwidth(), root.winfo_screenheight(),
             root.tk.call("tk", "scaling")))
    root.destroy()
    proc_tree.exit_hard(0)


if __name__ == "__main__":
    main()
