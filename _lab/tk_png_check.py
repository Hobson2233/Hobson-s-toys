# -*- coding: utf-8 -*-
"""验证：tkinter 能不能直接把接口返回的 base64 PNG 显示出来（不用 PIL）。

为什么必须先验：如果 tkinter 显示不了 PNG，整个「弹窗让用户看验证码」的设计就要改
（要么转格式、要么引入依赖）。这是个**一旦做完功能才发现就白干**的前置风险。

同时验证「放大显示」—— 130x40 太小了，得放大 3~4 倍才看得清。
tkinter 的 PhotoImage 有 zoom()，但那要整数倍；这里试 subsample/zoom。
"""
import base64
import json
import os
import sys
import tempfile

PNG = os.path.join(tempfile.gettempdir(), "zjzu_self_probe", "captcha.png")
print("验证码文件：%s  存在=%s" % (PNG, os.path.isfile(PNG)))
raw = open(PNG, "rb").read()
print("PNG 字节数：%d  头：%r" % (len(raw), raw[:8]))

b64 = base64.b64encode(raw).decode("ascii")

try:
    import tkinter as tk
except Exception as e:
    print("❌ 没有 tkinter：%r" % (e,))
    sys.exit(1)

print("tkinter 版本：Tk %s" % tk.TkVersion)

root = tk.Tk()
root.withdraw()

# ① 直接吃 base64 字符串
try:
    img = tk.PhotoImage(data=b64)
    print("✅ ① PhotoImage(data=base64) 成功  尺寸 %dx%d" % (img.width(), img.height()))
except Exception as e:
    print("❌ ① PhotoImage(data=base64) 失败：%r" % (e,))
    img = None

# ② 直接吃原始 bytes（某些版本支持）
try:
    img2 = tk.PhotoImage(data=raw)
    print("✅ ② PhotoImage(data=raw bytes) 成功  尺寸 %dx%d"
          % (img2.width(), img2.height()))
except Exception as e:
    print("❌ ② PhotoImage(data=raw bytes) 失败：%r" % (e,))

# ③ 放大 4 倍
if img is not None:
    try:
        big = img.zoom(4)
        print("✅ ③ zoom(4) 成功  %dx%d" % (big.width(), big.height()))
    except Exception as e:
        print("❌ ③ zoom(4) 失败：%r" % (e,))

# ④ 贴到 Label 上、真的渲染一帧（只 measure 不 mainloop）
if img is not None:
    try:
        lbl = tk.Label(root, image=img)
        lbl.pack()
        root.update_idletasks()
        root.update()
        print("✅ ④ Label 渲染成功（窗口已绘制一帧）")
    except Exception as e:
        print("❌ ④ Label 渲染失败：%r" % (e,))

root.destroy()
print("完成")
