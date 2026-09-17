"""放大/裁切 PNG 的局部，用来「看清」截图里到底画了什么。

为什么需要它：**截图里的文字是光栅化像素，不是字符串。** 想确认一张图里有没有
不该出现的账号、密码，任何字节搜索都是无效的（实测连窗口标题都搜不到）——
唯一可靠的办法是**用眼睛看**。而整张窗口图缩到 800px 宽时，密码框里的星号
到底几个根本数不准，所以需要裁切 + 放大。

只依赖标准库（环境里没有 PIL）：zlib 解 PNG、手写 PNG 编码。

用法：
    python png_zoom.py 图.png --info
    python png_zoom.py 图.png --crop 45,485,300,50 --scale 5 --out 放大.png
    python png_zoom.py 图.png --cols 485,530,45,400     # 数一行里有多少个字形
"""

import argparse
import os
import struct
import sys
import zlib


def read_png(path):
    """解出 (宽, 高, 通道数, 像素字节)。支持 8bit 灰度/RGB/RGBA，非隔行。"""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG: %s" % path)
    pos, idat, ihdr = 8, bytearray(), None
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
    if ihdr is None:
        raise ValueError("没有 IHDR")
    w, h, depth, ct, comp, filt, inter = ihdr
    if depth != 8:
        raise ValueError("只支持 8bit，实际 %d" % depth)
    if inter:
        raise ValueError("不支持隔行 PNG")
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ct]

    raw = zlib.decompress(bytes(idat))
    stride = w * nch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, nch, out


def to_rgb(w, h, nch, px):
    """统一成 RGB。灰度/灰+alpha 按灰度铺，带 alpha 的合成到白底。"""
    if nch == 3:
        return bytearray(px)
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        if nch == 1:
            v = px[i]
            rgb[i * 3:i * 3 + 3] = bytes((v, v, v))
        elif nch == 2:
            v = px[i * 2]
            rgb[i * 3:i * 3 + 3] = bytes((v, v, v))
        else:
            r, g, b, a = px[i * 4:i * 4 + 4]
            rgb[i * 3] = (r * a + 255 * (255 - a)) // 255
            rgb[i * 3 + 1] = (g * a + 255 * (255 - a)) // 255
            rgb[i * 3 + 2] = (b * a + 255 * (255 - a)) // 255
    return rgb


def write_png(path, w, h, rgb):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    out = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(out)


def crop_zoom(w, h, rgb, x, y, cw, ch, scale):
    cw = min(cw, w - x)
    ch = min(ch, h - y)
    out = bytearray(cw * scale * ch * scale * 3)
    ow = cw * scale
    for j in range(ch * scale):
        sy = y + j // scale
        row = rgb[(sy * w + x) * 3:(sy * w + x + cw) * 3]
        dst = bytearray()
        for i in range(cw):
            dst += row[i * 3:i * 3 + 3] * scale
        out[j * ow * 3:(j + 1) * ow * 3] = dst
    return ow, ch * scale, out


def column_clusters(w, h, rgb, y0, y1, x0, x1, dark=160, min_gap=2):
    """把一行文字按「空白列」切成若干簇，用来数字形个数。

    判据是「这一列在该行带内有没有足够暗的像素」——对星号这种小字形，
    单看一行可能穿不过去，所以按**列在带内的最暗值**判。
    """
    cols = []
    for x in range(x0, min(x1, w)):
        mn = 255
        for y in range(y0, min(y1, h)):
            i = (y * w + x) * 3
            v = (rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000
            if v < mn:
                mn = v
        cols.append(mn < dark)

    clusters, start, gap = [], None, 0
    for idx, ink in enumerate(cols):
        if ink:
            if start is None:
                start = idx
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                clusters.append((x0 + start, x0 + idx - gap))
                start = None
    if start is not None:
        clusters.append((x0 + start, x0 + len(cols) - 1))
    return clusters


def main():
    ap = argparse.ArgumentParser(description="放大 PNG 局部，看清截图里的字")
    ap.add_argument("png")
    ap.add_argument("--info", action="store_true", help="只打印尺寸")
    ap.add_argument("--crop", help="x,y,w,h")
    ap.add_argument("--scale", type=int, default=5)
    ap.add_argument("--out", help="输出路径，默认 <原名>_zoom.png")
    ap.add_argument("--cols", help="y0,y1,x0,x1 —— 数这个区域里有多少个字形簇")
    ap.add_argument("--dark", type=int, default=160, help="判定为墨的灰度阈值")
    a = ap.parse_args()

    w, h, nch, px = read_png(a.png)
    print("尺寸: %dx%d, %d 通道" % (w, h, nch))
    rgb = to_rgb(w, h, nch, px)

    if a.cols:
        y0, y1, x0, x1 = (int(v) for v in a.cols.split(","))
        cl = column_clusters(w, h, rgb, y0, y1, x0, x1, dark=a.dark)
        print("区域 y=%d..%d x=%d..%d 内共 %d 个字形簇:" % (y0, y1, x0, x1, len(cl)))
        for i, (s, e) in enumerate(cl, 1):
            print("  %2d: x=%d..%d  宽 %d" % (i, s, e, e - s + 1))
        return 0

    if a.crop:
        x, y, cw, ch = (int(v) for v in a.crop.split(","))
        ow, oh, out = crop_zoom(w, h, rgb, x, y, cw, ch, a.scale)
        dst = a.out or os.path.splitext(a.png)[0] + "_zoom.png"
        write_png(dst, ow, oh, out)
        print("已写出 %s  (%dx%d, 放大 %dx)" % (dst, ow, oh, a.scale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
