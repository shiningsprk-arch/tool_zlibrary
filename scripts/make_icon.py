# -*- coding: utf-8 -*-
"""生成包根 icon.png（256×256，纯标准库，无 Pillow 依赖）。

图案：深蓝圆角底板 + 白色大写 Z + 浅蓝书页 + 橙色书签——Z-Library 风格的原创
图标（不直接使用官方 logo 资产，避免公开仓的商标/版权风险）。

渲染用 3× 超采样抗锯齿；PNG 编码手写 IHDR/IDAT/IEND 三段。

用法：python scripts/make_icon.py
"""
import math
import os
import struct
import zlib

SIZE = 256
SS = 3  # 超采样倍率

BG = (12, 24, 42, 255)        # 深海军蓝
FG = (246, 248, 252, 255)     # 近白
PAGE = (122, 180, 230, 255)   # 浅蓝书页
ACCENT = (255, 154, 64, 255)  # 橙色书签
TRANSPARENT = (0, 0, 0, 0)

# 白色 Z：上下横杠 + 中间斜杠，多边形一次围出
Z_POLY = [
    (70, 64), (186, 64), (186, 96), (120, 164), (186, 164),
    (186, 196), (70, 196), (70, 164), (136, 96), (70, 96),
]
BOOK_LEFT = [(64, 198), (124, 212), (124, 232), (64, 218)]
BOOK_RIGHT = [(192, 198), (132, 212), (132, 232), (192, 218)]
BOOKMARK = [(192, 40), (214, 40), (214, 82), (203, 70), (192, 82)]


def in_rrect(x, y, x0, y0, x1, y1, r):
    qx = max(abs(x - (x0 + x1) / 2) - ((x1 - x0) / 2 - r), 0)
    qy = max(abs(y - (y0 + y1) / 2) - ((y1 - y0) / 2 - r), 0)
    return math.hypot(qx, qy) <= r


def in_polygon(x, y, points):
    inside = False
    count = len(points)
    j = count - 1
    for i in range(count):
        xi, yi = points[i]
        xj, yj = points[j]
        if ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def sample(x, y):
    """返回 (x, y) 处的 RGBA。"""
    if not in_rrect(x, y, 8, 8, 248, 248, 56):
        return TRANSPARENT
    if in_polygon(x, y, Z_POLY):
        return FG
    if in_polygon(x, y, BOOKMARK):
        return ACCENT
    if in_polygon(x, y, BOOK_LEFT) or in_polygon(x, y, BOOK_RIGHT):
        return PAGE
    return BG


def render():
    step = 1.0 / SS
    rows = []
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = px + (sx + 0.5) * step
                    y = py + (sy + 0.5) * step
                    pr, pg, pb, pa = sample(x, y)
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a == 0:
                row += b"\x00\x00\x00\x00"
            else:
                row += bytes((round(r / a), round(g / a), round(b / a),
                              round(a / (SS * SS))))
        rows.append(bytes(row))
    return rows


def write_png(path, rows):
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "icon.png")
    write_png(out, render())
    print("icon.png 已生成：%s（%d 字节）" % (out, os.path.getsize(out)))


if __name__ == "__main__":
    main()
