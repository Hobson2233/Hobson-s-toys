# -*- coding: utf-8 -*-
"""定制版 Tcl/Tk 数据收集钩子 —— 覆盖 PyInstaller 自带的 hook-_tkinter.py。

自带钩子会把整个 Tcl/Tk 库目录（约 4.1MB）原样打进去，其中一大半这个程序
根本用不到。这里按路径过滤掉几类：

    _tcl_data/tzdata   1.18MB   时区数据。界面里从不显示时间或时区。
    _tcl_data/msgs      116KB   Tcl 本地化消息。缺了会退回英文，而界面文案
                                全是程序自己写的中文，跟它没关系。
    _tk_data/msgs        81KB   同上。
    tcl8/8.5, tcl8/8.4  140KB   Tcl 8.5/8.4 兼容目录，我们只跑 8.6。

**故意保留 `_tcl_data/encoding`（1.54MB）**：中文文本要经 Tcl 做编解码，
删掉它的风险（界面中文变乱码）远大于省下的体积，不值得赌。

用法：构建时用 --additional-hooks-dir 指向本文件所在目录。
"""
from PyInstaller.utils.hooks.tcl_tk import tcltk_info

# 命中任一项就丢弃（比较的是归一化后的 "/a/b/c/" 形式）
SKIP_PARTS = (
    "/tzdata/",
    "/tcl8/8.5/",
    "/tcl8/8.4/",
    "/msgs/",
)


def hook(hook_api):
    # 保留自带钩子的这两道检查：数据目录收集不到就应该让构建失败，
    # 而不是出一个运行期才报错的包。
    if tcltk_info.tcl_data_missing:
        raise SystemExit(
            "ERROR: Tcl 数据目录收集失败 (%r)" % (tcltk_info.tcl_data_dir,))
    if tcltk_info.tk_data_missing:
        raise SystemExit(
            "ERROR: Tk 数据目录收集失败 (%r)" % (tcltk_info.tk_data_dir,))

    kept, dropped, dropped_bytes = [], 0, 0
    import os
    for item in tcltk_info.data_files:
        dest = "/" + item[0].replace("\\", "/").strip("/") + "/"
        low = dest.lower()
        if any(p in low for p in SKIP_PARTS):
            dropped += 1
            try:
                dropped_bytes += os.path.getsize(item[1])
            except Exception:
                pass
            continue
        kept.append(item)

    print("[hook-_tkinter] Tcl/Tk 数据: 保留 %d 项，剔除 %d 项（约 %.2f MB）"
          % (len(kept), dropped, dropped_bytes / 1024.0 / 1024.0))
    hook_api.add_datas(kept)
