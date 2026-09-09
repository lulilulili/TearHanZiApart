# -*- coding: utf-8 -*-
"""strokelab.boolean — shapely 布尔收口：保证所有笔画并集与原字形恒等。

clampStrokes：
  1. 裁剪（保证“不多”）：每笔区域与原字形求交，越界部分删除；
  2. 残差回填（保证“不少”）：原形中未被任何笔覆盖的面片，按共享边界长度
     回填给相邻笔画；
  3. 仅对确有问题的笔画替换为裁剪后的多边形路径（细分 2 单位），
     无问题的笔画保留原始贝塞尔精确切片路径。
"""

import math

from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

from .geometry import parseContours, flattenSegs, shapeDescriptor

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
    """孔洞按所属组减除后再组间取并（≈nonzero 填充）：孔只挖自己外环的
    区域，穿过该孔的其它实体轮廓经并集仍保持实心——全局减孔曾把"中"的
    中竖在口字内腔处挖断。"""
    byGroup = {}
    for c in contours:
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) < 4:
            continue
        try:
            pg = Polygon(pts)
            if not pg.is_valid:
                pg = pg.buffer(0)
            g = byGroup.setdefault(c.get("group", 0), {"outers": [], "holes": []})
            (g["holes"] if c.get("isHole") else g["outers"]).append(pg)
        except Exception:
            pass
    regions = []
    for g in byGroup.values():
        if not g["outers"]:
            continue
        r = unary_union(g["outers"])
        if g["holes"]:
            r = r.difference(unary_union(g["holes"]))
        if not r.is_valid:
            r = r.buffer(0)
        if not r.is_empty:
            regions.append(r)
    if not regions:
        return None
    region = unary_union(regions)
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


def _piecesOf(region):
    if region is None or region.is_empty:
        return []
    if isinstance(region, Polygon):
        return [region]
    if isinstance(region, MultiPolygon):
        return list(region.geoms)
    return [g for g in getattr(region, "geoms", []) if isinstance(g, Polygon)]


def rescueStarved(contours, strokes, kaiStrokePaths, kaiMedians=None):
    """饿死救济：重构后面积不足楷体占比预期 35% 的笔（含零宽退化环），
    用骨架走廊（中轴线按笔宽 buffer）∩ 本组轮廓区域作为救济区域——
    纯矢量、必在字形内。走廊中轴线用楷体中轴线经**组局部仿射**映射
    （楷体同组笔画包围盒→目标组轮廓包围盒）：全局仿射在部件比例
    差异大时会把楷体底横映到目标腔体中间（鸿蒙咋的口字旁全高瘦长、
    楷体口字旁在中上部）；精调中轴线被杂散样本带歪更不可用。
    与邻笔重叠=双重归属，允许，并从侵占邻笔区域减掉救济体恢复真
    划分。并集恒等仍由随后的 clampStrokes 保证。返回被救济笔序号。"""
    from shapely.geometry import LineString

    glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return []
    kaiAreas = []
    for p in kaiStrokePaths:
        r = _evenOddRegion(_loopPolys(p))
        kaiAreas.append(r.area if r is not None else 0.0)
    kaiTotal = sum(kaiAreas) or 1.0

    groupRegions = {}

    def regionOfGroup(g):
        if g not in groupRegions:
            outers = [c for c in contours if c.get("group") == g and not c.get("isHole")]
            holes = [c for c in contours if c.get("group") == g and c.get("isHole")]
            region = None
            for c in outers:
                pts = flattenSegs(c["segs"], _FLAT)
                if len(pts) < 4:
                    continue
                pg = Polygon(pts)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                region = pg if region is None else region.union(pg)
            if region is not None:
                for c in holes:
                    pts = flattenSegs(c["segs"], _FLAT)
                    if len(pts) < 4:
                        continue
                    pg = Polygon(pts)
                    if not pg.is_valid:
                        pg = pg.buffer(0)
                    region = region.difference(pg)
                if not region.is_valid:
                    region = region.buffer(0)
            groupRegions[g] = region
        return groupRegions[g]

    rescued = []
    widthsAll = sorted(float(s.get("width") or 0.0) for s in strokes
                       if s.get("width"))
    medWidth = widthsAll[len(widthsAll) // 2] if widthsAll else 60.0

    groupStrokeIdx = {}
    for i, s in enumerate(strokes):
        groupStrokeIdx.setdefault(s.get("group"), []).append(i)

    def structMedian(i, g):
        """楷体中轴线经组局部仿射（楷体同组笔画包围盒→目标组区域包围盒）。"""
        if kaiMedians is None or i >= len(kaiMedians) or not kaiMedians[i]:
            return None
        region = regionOfGroup(g)
        if region is None or region.is_empty:
            return None
        tb = region.bounds
        ks = groupStrokeIdx.get(g, [i])
        xs = [p[0] for k in ks if k < len(kaiMedians) for p in kaiMedians[k]]
        ys = [p[1] for k in ks if k < len(kaiMedians) for p in kaiMedians[k]]
        if not xs:
            return None
        kx0, kx1 = min(xs), max(xs)
        ky0, ky1 = min(ys), max(ys)
        sx = (tb[2] - tb[0]) / max(1.0, kx1 - kx0)
        sy = (tb[3] - tb[1]) / max(1.0, ky1 - ky0)
        return [(tb[0] + (p[0] - kx0) * sx, tb[1] + (p[1] - ky0) * sy)
                for p in kaiMedians[i]]

    for i, s in enumerate(strokes):
        cur = None if s["failed"] else _evenOddRegion(_loopPolys(s["path"]))
        curArea = cur.area if cur is not None else 0.0
        expect = glyph.area * (kaiAreas[i] / kaiTotal if i < len(kaiAreas) else 0.0)
        starved = curArea < max(100.0, expect * 0.35)
        # 轴向病判：横/竖笔的轮廓 PCA 主轴严重背离（>45°）= 切错拿了
        # 邻笔的竖片/横片（鸿蒙咋的口底横曾拿到壁上竖条），同样重建
        misAxis = False
        if not starved and cur is not None and s.get("type") in ("横", "竖"):
            d = shapeDescriptor([s["path"]])
            if d and d["elong"] >= 1.8:
                ang = abs(math.degrees(d["mainAngle"])) % 180.0
                dev = min(ang, 180.0 - ang) if s["type"] == "横" \
                    else abs(ang - 90.0)
                misAxis = dev > 45.0
        if not starved and not misAxis:
            continue
        median = structMedian(i, s.get("group"))
        if median is None or len(median) < 2:
            median = s.get("median")
        if not median or len(median) < 2:
            continue
        region = regionOfGroup(s.get("group"))
        if region is None or region.is_empty:
            region = glyph
        # 饿死笔自身的宽度估计同样是饿的（样本少）——用全字笔宽中位数兜底
        w = max(float(s.get("width") or 0.0), medWidth, 30.0)
        try:
            corridor = LineString([tuple(p) for p in median]).buffer(w * 0.6)
            # 位置吸附：bbox 线性映射在部件比例非线性差异时会把走廊放
            # 进腔体（楷体口矮胖底横占30%高、鸿蒙口瘦高占12%，映射落在
            # 孔洞中，与两壁的交仍不小但全是碎竖片）。沿法向滑动搜索，
            # 评分用"最大单片面积"（墨带整片必胜壁上碎片），取"达最优
            # 80%中位移最小"的位置。
            from shapely.affinity import translate

            def biggestPiece(geom):
                ps = _piecesOf(geom)
                return max((g.area for g in ps), default=0.0)

            ddx = median[-1][0] - median[0][0]
            ddy = median[-1][1] - median[0][1]
            LL = math.hypot(ddx, ddy) or 1.0
            nx, ny = -ddy / LL, ddx / LL
            rb = region.bounds
            span = max(rb[2] - rb[0], rb[3] - rb[1])
            cands = []
            for t in range(-10, 11):
                off = span * 0.5 * t / 10.0
                c2 = corridor if t == 0 else \
                    translate(corridor, xoff=nx * off, yoff=ny * off)
                try:
                    a = biggestPiece(c2.intersection(region))
                except Exception:
                    a = 0.0
                cands.append((a, abs(off), off))
            bestA = max(a for a, _, _ in cands)
            if bestA <= 0:
                continue
            good = min((ab, off) for a, ab, off in cands if a >= bestA * 0.8)
            if abs(good[1]) > 1e-9:
                corridor = translate(corridor, xoff=nx * good[1],
                                     yoff=ny * good[1])
            body = corridor.intersection(region)
            if not body.is_valid:
                body = body.buffer(0)
        except Exception:
            continue
        pieces = _piecesOf(body)
        if pieces:
            # 走廊穿过孔洞/间隙会碎成多片，只留最大片（真身）防 SPLIT
            body = max(pieces, key=lambda g: g.area)
        if body.is_empty or body.area < 25:
            continue
        # 饿死救济要求救济体明显大于现状；轴向病重建则是形状置换，
        # 只要求救济体像样（≥楷体占比预期的1/4）
        if starved and body.area < curArea * 1.5:
            continue
        if misAxis and body.area < expect * 0.25:
            continue
        p = _regionToPath(body)
        if not p:
            continue
        s["path"] = p
        s["failed"] = False
        s["clamped"] = True
        s["rescued"] = True
        rescued.append(i)
        # 救济体所在的墨原先整块判给了邻笔（这正是饿死的原因）——从
        # 侵占邻笔的区域里减掉救济体，把双重归属改回真正的划分。减完
        # 会碎成多片（走廊横穿邻笔）则放弃减除，保留双重归属。
        for j, s2 in enumerate(strokes):
            if j == i or s2["failed"] or s2.get("group") != s.get("group"):
                continue
            r2 = _evenOddRegion(_loopPolys(s2["path"]))
            if r2 is None or r2.is_empty:
                continue
            inter = r2.intersection(body)
            if inter.area < body.area * 0.25:
                continue
            cand = r2.difference(body)
            if not cand.is_valid:
                cand = cand.buffer(0)
            big = [g for g in _piecesOf(cand)
                   if g.area >= max(25.0, cand.area * 0.02)]
            if len(big) != 1:
                continue
            p2 = _regionToPath(cand)
            if p2:
                s2["path"] = p2
                s2["clamped"] = True
    return rescued



def enforceConnectivity(contours, strokes, maxRounds=3):
    """单笔单连通终态收口（公理：同一笔画不会断成两个孤立连通组）。
    多片笔画只留最大片，其余显著片按共享边界最长原则划给相邻笔；
    无人接壤的片留回原主（宁可 SPLIT 不丢墨——并集恒等优先）。
    残差回填/邻笔减除等上游环节偶发的断笔在此统一修复。
    返回是否有改动。"""
    regions = [None if s["failed"] else _evenOddRegion(_loopPolys(s["path"]))
               for s in strokes]
    dirty = set()
    for _ in range(maxRounds):
        changed = False
        for i in range(len(strokes)):
            r = regions[i]
            if r is None or r.is_empty:
                continue
            big = [g for g in _piecesOf(r)
                   if g.area >= max(25.0, r.area * 0.02)]
            if len(big) <= 1:
                continue
            big.sort(key=lambda g: -g.area)
            keep = r
            for piece in big[1:]:
                pb = piece.buffer(1.5)
                bestJ, bestShare = -1, 1.0
                for j, r2 in enumerate(regions):
                    if j == i or r2 is None or r2.is_empty:
                        continue
                    try:
                        share = pb.intersection(r2).area
                    except Exception:
                        share = 0.0
                    if share > bestShare:
                        bestShare, bestJ = share, j
                if bestJ < 0:
                    continue
                keep = keep.difference(piece)
                if not keep.is_valid:
                    keep = keep.buffer(0)
                merged = regions[bestJ].union(piece)
                if not merged.is_valid:
                    merged = merged.buffer(0)
                regions[bestJ] = merged
                dirty.add(bestJ)
                changed = True
            if changed:
                regions[i] = keep
                dirty.add(i)
        if not changed:
            break
    for k in dirty:
        if regions[k] is None or regions[k].is_empty:
            continue
        p = _regionToPath(regions[k])
        if p:
            strokes[k]["path"] = p
            strokes[k]["clamped"] = True
    return bool(dirty)


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
