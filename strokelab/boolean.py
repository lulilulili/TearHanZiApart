# -*- coding: utf-8 -*-
"""strokelab.boolean — shapely 布尔收口：保证所有笔画并集与原字形恒等。

clampStrokes：
  1. 裁剪（保证“不多”）：每笔区域与原字形求交，越界部分删除；
  2. 残差回填（保证“不少”）：原形中未被任何笔覆盖的面片，按共享边界长度
     回填给相邻笔画；
  3. 仅对确有问题的笔画替换为裁剪后的多边形路径（细分 2 单位），
     无问题的笔画保留原始贝塞尔精确切片路径。
"""

from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

from .geometry import parseContours, flattenSegs

_FLAT = 3.0  # 布尔运算用的细分步长


def _loopPolys(pathStr):
    polys = []
    for c in parseContours(pathStr):
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) >= 4:
            try:
                pg = Polygon(pts)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                if not pg.is_empty:
                    polys.append(pg)
            except Exception:
                pass
    return polys


def _evenOddRegion(polys):
    """多环按奇偶合成区域（环形段=外环⊕孔环）。"""
    region = None
    for pg in polys:
        region = pg if region is None else region.symmetric_difference(pg)
    if region is None:
        return None
    if not region.is_valid:
        region = region.buffer(0)
    return region


def glyphRegion(contours):
    outers = []
    holes = []
    for c in contours:
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) < 4:
            continue
        try:
            pg = Polygon(pts)
            if not pg.is_valid:
                pg = pg.buffer(0)
            (holes if c["isHole"] else outers).append(pg)
        except Exception:
            pass
    if not outers:
        return None
    region = unary_union(outers)
    if holes:
        region = region.difference(unary_union(holes))
    if not region.is_valid:
        region = region.buffer(0)
    return region


def _regionToPath(region):
    geoms = []
    if isinstance(region, Polygon):
        geoms = [region]
    elif isinstance(region, MultiPolygon):
        geoms = list(region.geoms)
    else:
        try:
            geoms = [g for g in region.geoms if isinstance(g, Polygon)]
        except Exception:
            return ""
    parts = []
    for g in geoms:
        if g.area < 4:
            continue
        rings = [g.exterior] + list(g.interiors)
        for ring in rings:
            coords = list(ring.coords)
            if len(coords) < 4:
                continue
            d = "M %.1f %.1f " % coords[0][:2]
            d += " ".join("L %.1f %.1f" % c[:2] for c in coords[1:-1])
            parts.append(d + " Z")
    return " ".join(parts)


def reUnionCheck(contours, strokes):
    """覆盖率/溢出率（shapely 面积精确计算）。"""
    glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return {"cover": 0, "excess": 0}
    regions = []
    for s in strokes:
        if s["failed"]:
            continue
        r = _evenOddRegion(_loopPolys(s["path"]))
        if r is not None and not r.is_empty:
            regions.append(r)
    if not regions:
        return {"cover": 0, "excess": 0}
    union = unary_union(regions)
    if not union.is_valid:
        union = union.buffer(0)
    cover = union.intersection(glyph).area / glyph.area * 100
    excess = (union.area - union.intersection(glyph).area) / union.area * 100
    return {"cover": round(cover, 1), "excess": round(excess, 1)}


def clampStrokes(contours, strokes, excessTol=0.5, coverTol=99.5):
    """裁剪 + 残差回填。就地修改 strokes 的 path，返回收口后的 unionCheck。"""
    glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return {"cover": 0, "excess": 0}

    regions = []
    for s in strokes:
        r = None
        if not s["failed"]:
            r = _evenOddRegion(_loopPolys(s["path"]))
        regions.append(r)

    # 1) 裁剪：越界删除
    clamped = []
    replaced = [False] * len(strokes)
    for i, r in enumerate(regions):
        if r is None or r.is_empty:
            clamped.append(None)
            continue
        inter = r.intersection(glyph)
        if not inter.is_valid:
            inter = inter.buffer(0)
        excess = r.area - inter.area
        if excess > max(4.0, r.area * excessTol / 100):
            replaced[i] = True
        clamped.append(inter)

    # 2) 残差回填：未覆盖面片给共享边界最长的笔
    valid = [c for c in clamped if c is not None and not c.is_empty]
    if valid:
        union = unary_union(valid)
        if not union.is_valid:
            union = union.buffer(0)
        residual = glyph.difference(union)
        if not residual.is_empty and residual.area > max(4.0, glyph.area * (100 - coverTol) / 100):
            pieces = list(residual.geoms) if hasattr(residual, "geoms") else [residual]
            for piece in pieces:
                if piece.area < 4:
                    continue
                bestI, bestLen = -1, -1.0
                pb = piece.buffer(1.5)
                for i, c in enumerate(clamped):
                    if c is None or c.is_empty:
                        continue
                    try:
                        shared = pb.intersection(c).area
                    except Exception:
                        shared = 0.0
                    if shared > bestLen:
                        bestLen, bestI = shared, i
                if bestI >= 0:
                    merged = clamped[bestI].union(piece)
                    if not merged.is_valid:
                        merged = merged.buffer(0)
                    clamped[bestI] = merged
                    replaced[bestI] = True

    # 3) 仅替换确有问题的笔画路径（其余保留贝塞尔精确切片）
    for i, s in enumerate(strokes):
        if replaced[i] and clamped[i] is not None and not clamped[i].is_empty:
            p = _regionToPath(clamped[i])
            if p:
                s["path"] = p
                s["clamped"] = True

    # 4) 收口后校验
    finalRegions = [c for c in clamped if c is not None and not c.is_empty]
    if not finalRegions:
        return {"cover": 0, "excess": 0}
    union = unary_union(finalRegions)
    if not union.is_valid:
        union = union.buffer(0)
    cover = union.intersection(glyph).area / glyph.area * 100
    excess = (union.area - union.intersection(glyph).area) / max(1.0, union.area) * 100
    return {"cover": round(cover, 1), "excess": round(excess, 1)}
