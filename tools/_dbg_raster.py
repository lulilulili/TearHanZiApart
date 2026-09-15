# -*- coding: utf-8 -*-
"""调试用极简光栅器：shapely 区域表 → PNG(纯标准库 zlib 编码)。
标定期自查用（目检毛边），不进生产路径。"""
import struct
import zlib

import numpy as np


def _pngChunk(tag, data):
    c = tag + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))


def writePng(path, rgb):
    """rgb: HxWx3 uint8 数组 → PNG 文件。"""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))
    out = (b"\x89PNG\r\n\x1a\n"
           + _pngChunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + _pngChunk(b"IDAT", zlib.compress(raw, 6))
           + _pngChunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(out)


PALETTE = [(230, 25, 75), (60, 180, 75), (255, 225, 25), (67, 99, 216),
           (245, 130, 48), (145, 30, 180), (70, 240, 240), (240, 50, 230),
           (188, 246, 12), (250, 190, 190), (0, 128, 128), (230, 190, 255),
           (154, 99, 36), (255, 250, 200), (128, 0, 0), (170, 255, 195)]


def rasterRegions(path, regions, marks=None, width=640, pad=60):
    """regions: [(shapely region|None)] 依序着色（后者覆盖前者、交叠区
    混色打折）；marks: [(x,y,rgb)] 打十字标记。坐标系 y 向上翻转。"""
    import shapely
    boxes = [r.bounds for r in regions if r is not None and not r.is_empty]
    if not boxes:
        return
    x0 = min(b[0] for b in boxes) - pad
    y0 = min(b[1] for b in boxes) - pad
    x1 = max(b[2] for b in boxes) + pad
    y1 = max(b[3] for b in boxes) + pad
    scale = width / (x1 - x0)
    h = int((y1 - y0) * scale) + 1
    xs = np.linspace(x0, x1, width)
    ys = np.linspace(y1, y0, h)   # 上行=大 y（翻转）
    gx, gy = np.meshgrid(xs, ys)
    img = np.full((h, width, 3), 255, np.uint8)
    covered = np.zeros((h, width), bool)
    for i, r in enumerate(regions):
        if r is None or r.is_empty:
            continue
        m = shapely.contains_xy(r, gx.ravel(), gy.ravel()).reshape(h, width)
        col = np.array(PALETTE[i % len(PALETTE)], np.uint8)
        both = m & covered
        img[m] = col
        img[both] = (col * 0.5).astype(np.uint8)  # 双重归属区调暗
        covered |= m
    for x, y, col in (marks or []):
        px = int((x - x0) * scale)
        py = int((y1 - y) * scale)
        for d in range(-4, 5):
            for ax, ay in ((px + d, py), (px, py + d)):
                if 0 <= ax < width and 0 <= ay < h:
                    img[ay, ax] = col
    writePng(path, img)
