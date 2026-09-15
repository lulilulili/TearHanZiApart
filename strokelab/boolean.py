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
import os as _os

from functools import lru_cache

from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import unary_union

from .geometry import (parseContours, flattenSegs, shapeDescriptor,
                       signedArea, _medialAdjacency)

_FLAT = 3.0  # 布尔运算用的细分步长

# ---- 形态修复器·交界毛刺置换开关（2026-09-15）。默认关；病例集+健康
# 集标定与四门（关态 parity / 开态 bench / sample958 / cross）全绿后
# 转默认开。环境变量 STROKELAB_MORPH_REPAIR=1 开（verify 批跑 spawn
# worker 读不到主进程的模块属性赋值，环境变量随子进程继承——
# pipeline.CLIB_ENABLE 先例）。开关放 boolean 模块而非 pipeline 包
# __init__（本轮授权路径约束，arbitrate.LADDER_F 先例）；
# finalize.sealUnion 经 booleanClamp 属性运行期取值，标定脚本赋值即生效。
MORPH_REPAIR = _os.environ.get("STROKELAB_MORPH_REPAIR", "0") == "1"
MORPH_RETAIN_GATE = 0.95   # 检测门：保留率低于此（切割动过交界带）才审
MORPH_BRIDGE_GATE = 2      # 或 桥数≥此值（交界带靠桥缝合=毛刺高发）
MORPH_SPUR_FLOOR = 40.0    # 短叶枝阈下限（照 verify.MORPH_SPUR_MIN 口径）
MORPH_PATCH_MAX = 0.35     # 单补丁面积上限（占本笔比）。曾设 0.10——
                           # 入侵残片型病理（simkai 草 #5横被邻笔残片
                           # 压顶，残片≈38% 本笔面积）整类漏修；真防线
                           # 是 接触>颈宽+形态改善+单连通 三守卫，面积
                           # 上限只挡"整半边身子搬家"的失控场景
MORPH_SHARE_MAX = 0.45     # 单笔转让总面积上限——retainRatio 字段收口链
                           # 从不改写，"retain 不得下降"守卫按面积化口径
                           # 落地：置换后本笔至少留 55% 面积
MORPH_RECV_MAX = 0.6       # 补丁不得超过受让笔面积的 0.6 倍（挂在别人
                           # 身上的肉不会比别人的身子还大）
MORPH_RECV_SICK = 30       # 受让笔当前形态分（交叉+短叶枝）超过此值不
                           # 受让：重病笔的轮廓不可信，往它身上归肉是
                           # 垃圾上摞垃圾（simkai 草 #5横 分 88 实证，
                           # 收下残片后 verify 全字 junc 30→37 净恶化）。
                           # 阈值标定：宋体健康-中噪笔实测 1-17（強 的
                           # 受让候选 14-17 属衬线锯齿底噪，12 曾误拦），
                           # 重病实例 88——取 30 分界
MORPH_CONTACT_MIN = 4.0    # 邻笔接触长度下限（数值噪声级接触不构成转让证据）
MORPH_SEAM_TOL = 0.6       # 接触判定膨胀带（切割缝两侧 0.1 舍入+细分噪声）
MORPH_ARM_MAX = 250.0      # 伪臂候选链长上限：交界粘连尾巴（永 横折钩沿捺
                           # 左flank 的楔实测 187）在此之下；真笔臂（横杆/
                           # 竖身，geometry.medialJunctions 实测真 T 臂 354+）
                           # 在此之上——同一尺度先例（角噪二段剪除 <250）
MORPH_MIN_GAIN = 5         # 本笔手术收益（交叉+短叶枝净降）下限。标定：
                           # 赢家 永 5 / simhei草 15，噪声触发 我 1 / 婕 3
                           # ——收益≤重扫噪声量级(±2-3)的置换纯属搬椅子，
                           # 级联(种子→自洽二遍)方差还会外溢（健康集 筻
                           # 曾因小收益置换 retain -0.027）


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
        if region is None:
            region = pg
        else:
            try:
                region = region.symmetric_difference(pg)
            except Exception:
                region = region.buffer(0).symmetric_difference(pg.buffer(0))
    if region is None:
        return None
    if not region.is_valid:
        region = region.buffer(0)
    return region


@lru_cache(maxsize=256)
def _pathRegion(pathStr):
    """路径串 → 奇偶合成区域，模块级 LRU 只读共享。同一笔的区域在
    rescueStarved/clampStrokes/enforceConnectivity/reUnionCheck 之间
    曾重建 4-5 次。键=路径串，笔画路径被收口改写即换新串、自然失效。
    Shapely 2.x 几何不可变，所有调用点只做 intersection/difference/
    union（均产新对象）后重绑定，无就地改写（已逐点审计）。容量
    2048：单次收口触达路径至多数百，批量按 LRU 淘汰。"""
    return _evenOddRegion(_loopPolys(pathStr))


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


def _groupRegionOf(contours, g):
    """组 g 的 shapely 区域：外环并集 − 孔洞并集。rescueStarved 与
    clampStrokes 共用（曾各写一份等价逻辑）。"""
    outs = None
    hs = None
    for c in contours:
        if c.get("group") != g:
            continue
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) < 4:
            continue
        pg = Polygon(pts)
        if not pg.is_valid:
            pg = pg.buffer(0)
        if c.get("isHole"):
            hs = pg if hs is None else hs.union(pg)
        else:
            outs = pg if outs is None else outs.union(pg)
    if outs is not None and hs is not None:
        outs = outs.difference(hs)
        if not outs.is_valid:
            outs = outs.buffer(0)
    return outs


def rescueStarved(contours, strokes, kaiStrokePaths, kaiMedians=None, glyph=None):
    """饿死救济：重构后面积不足楷体占比预期 35% 的笔（含零宽退化环），
    用骨架走廊（中轴线按笔宽 buffer）∩ 本组轮廓区域作为救济区域——
    纯矢量、必在字形内。走廊中轴线用楷体中轴线经**组局部仿射**映射
    （楷体同组笔画包围盒→目标组轮廓包围盒）：全局仿射在部件比例
    差异大时会把楷体底横映到目标腔体中间（鸿蒙咋的口字旁全高瘦长、
    楷体口字旁在中上部）；精调中轴线被杂散样本带歪更不可用。
    与邻笔重叠=双重归属，允许，并从侵占邻笔区域减掉救济体恢复真
    划分。并集恒等仍由随后的 clampStrokes 保证。返回被救济笔序号。"""
    from shapely.geometry import LineString

    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return []
    kaiAreas = []
    for p in kaiStrokePaths:
        r = _pathRegion(p)
        kaiAreas.append(r.area if r is not None else 0.0)
    kaiTotal = sum(kaiAreas) or 1.0

    groupRegions = {}

    def regionOfGroup(g):
        if g not in groupRegions:
            groupRegions[g] = _groupRegionOf(contours, g)
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
        cur = None if s["failed"] else _pathRegion(s["path"])
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
            r2 = _pathRegion(s2["path"])
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



def resolveKaiDisjointOverlaps(strokes, kaiMedians, ratio=0.6, kaiDist=40.0):
    """楷体不相交笔对的重叠减除：同组两笔在楷体中轴线互不接近（最近
    距离>kaiDist）却切出大面积重叠（>较小者60%）——融合日/目/口的
    封底横被外框整体包含（亨/亭/勤 100%）。把重叠带从**较大**笔减除
    （内横保带、外框让位）；减除后较大笔碎裂则放弃（宁留双重归属）。
    返回修正的笔序号列表。"""
    rs = []
    for m in kaiMedians:
        step = max(1, len(m) // 12)
        rs.append([tuple(p) for p in m[::step]] + [tuple(m[-1])])

    def kaiMin(i, j):
        return min(math.hypot(p[0] - q[0], p[1] - q[1])
                   for p in rs[i] for q in rs[j])

    regions = [None if s["failed"] else _pathRegion(s["path"])
               for s in strokes]
    fixed = []
    for i in range(len(strokes)):
        for j in range(i + 1, len(strokes)):
            if strokes[i]["failed"] or strokes[j]["failed"]:
                continue
            if strokes[i].get("group") != strokes[j].get("group"):
                continue
            ri, rj = regions[i], regions[j]
            if ri is None or rj is None or ri.is_empty or rj.is_empty:
                continue
            if i < len(rs) and j < len(rs) and kaiMin(i, j) <= kaiDist:
                continue
            try:
                inter = ri.intersection(rj)
            except Exception:
                continue
            small = min(ri.area, rj.area)
            if small < 25 or inter.area < ratio * small:
                continue
            big, sml = (i, j) if ri.area >= rj.area else (j, i)
            try:
                cand = regions[big].difference(regions[sml])
                if not cand.is_valid:
                    cand = cand.buffer(0)
            except Exception:
                continue
            pieces = [g for g in _piecesOf(cand)
                      if g.area >= max(25.0, cand.area * 0.02)]
            if len(pieces) > 2 or cand.area < 25:
                continue
            p = _regionToPath(cand)
            if not p:
                continue
            strokes[big]["path"] = p
            strokes[big]["clamped"] = True
            regions[big] = cand
            fixed.append(big)
    return fixed


def fillResidualGaps(contours, strokes, glyph=None, minPiece=25.0):
    """终态补缝：未覆盖残片（≥minPiece）无条件回填给同组共享边界最长
    的笔。与 clampStrokes 的回填同逻辑，但没有 0.5% 总量闸——那是
    校验容差不是跳过闸，0.4% 的缺口曾在爱的冖区留白缝。只该在救济/
    减除/连通性全部尘埃落定后的终态调用（中间轮次调用会把救济前的
    大残差乱粘给邻笔——質12横曾被粘26639）。返回是否有改动。"""
    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return False
    regions = []
    for s in strokes:
        r = None if s["failed"] else _pathRegion(s["path"])
        regions.append(r)
    valid = [r for r in regions if r is not None and not r.is_empty]
    if not valid:
        return False
    union = unary_union(valid)
    if not union.is_valid:
        union = union.buffer(0)
    residual = glyph.difference(union)
    if residual.is_empty or residual.area <= 4.0:
        return False
    changed = False
    pieces = list(residual.geoms) if hasattr(residual, "geoms") else [residual]
    for piece in pieces:
        if piece.area < minPiece:
            continue
        pb = piece.buffer(1.5)
        # 归属组=与残片交叠最大的轮廓组
        pieceG, pgArea = None, 0.0
        for g in sorted({c.get("group", 0) for c in contours}):
            reg = _groupRegionOf(contours, g)
            if reg is None:
                continue
            try:
                a = reg.intersection(piece).area
            except Exception:
                a = 0.0
            if a > pgArea:
                pgArea, pieceG = a, g
        bestI, bestL = -1, 0.0
        for i, r in enumerate(regions):
            if r is None or r.is_empty:
                continue
            if pieceG is not None and strokes[i].get("group") != pieceG:
                continue
            try:
                shared = pb.intersection(r).area
            except Exception:
                shared = 0.0
            if shared > bestL:
                bestL, bestI = shared, i
        if bestI < 0:
            for i, r in enumerate(regions):
                if r is None or r.is_empty:
                    continue
                try:
                    shared = pb.intersection(r).area
                except Exception:
                    shared = 0.0
                if shared > bestL:
                    bestL, bestI = shared, i
        if bestI < 0:
            continue
        merged = regions[bestI].union(piece)
        if not merged.is_valid:
            merged = merged.buffer(0)
        p2 = _regionToPath(merged)
        if p2:
            strokes[bestI]["path"] = p2
            strokes[bestI]["clamped"] = True
            regions[bestI] = merged
            changed = True
    return changed


def enforceConnectivity(contours, strokes, maxRounds=3, glyph=None):
    """单笔单连通终态收口（公理：同一笔画不会断成两个孤立连通组）。
    多片笔画只留最大片，其余显著片按共享边界最长原则划给相邻笔；
    无人接壤的片留回原主（宁可 SPLIT 不丢墨——并集恒等优先）。
    残差回填/邻笔减除等上游环节偶发的断笔在此统一修复。
    返回是否有改动。"""
    regions = [None if s["failed"] else _pathRegion(s["path"])
               for s in strokes]
    dirty = set()
    for _ in range(maxRounds):
        changed = False
        for i in range(len(strokes)):
            r = regions[i]
            if r is None or r.is_empty:
                continue
            big = [g for g in _piecesOf(r) if g.area >= 25.0]
            if len(big) <= 1:
                continue
            big.sort(key=lambda g: -g.area)
            keep = r
            for piece in big[1:]:
                # 独立字体轮廓可以互相叠印：碎片若已由其它笔完整覆盖，
                # 删除重复归属即可，不必跨组搬运。否则門内独立横上的
                # 碎片会被组隔离挡住，在框的两笔之间来回转移。
                covered = [r2 for j, r2 in enumerate(regions)
                           if j != i and r2 is not None and not r2.is_empty
                           and r2.intersects(piece)]
                if covered and piece.difference(unary_union(covered)).area < 1e-6:
                    keep = keep.difference(piece)
                    dirty.add(i)
                    changed = True
                    continue
                # 探测半径放宽到 4：裁剪的数值缝隙曾让残片"无人接壤"而
                # 滞留原主（TC威的竖捺）；仍找不到接壤者则给距离最近的笔
                pb = piece.buffer(4.0)
                bestJ, bestShare = -1, 1.0
                gi = strokes[i].get("group")
                for j, r2 in enumerate(regions):
                    # 图层隔离：面片只捐给同组的笔——跨组捐赠会让直出
                    # 组被外组碎片污染（悬浮部件与外组孔洞共享边界）
                    if j == i or r2 is None or r2.is_empty or                             strokes[j].get("group") != gi:
                        continue
                    try:
                        share = pb.intersection(r2).area
                    except Exception:
                        share = 0.0
                    if share > bestShare:
                        bestShare, bestJ = share, j
                if bestJ < 0:
                    bestD = 1e18
                    for j, r2 in enumerate(regions):
                        if j == i or r2 is None or r2.is_empty or                                 strokes[j].get("group") != gi:
                            continue
                        try:
                            d = piece.distance(r2)
                        except Exception:
                            continue
                        if d < bestD:
                            bestD, bestJ = d, j
                    if bestJ >= 0 and bestD > 15.0:
                        bestJ = -1
                    # 同组全空/全远时的接壤跨组捐赠（勤：竖2残留1936孤儿
                    # 片距同组邻笔178，而他组笔与其 buffer(4) 交286/311——
                    # 融合条带的墨本就属于邻部件）。门控：仅当同组无人
                    # 接壤且他组确有实接壤者；防跨组污染的原禁令针对的
                    # "悬浮部件与外组孔洞共享边界"场景同组也接壤，不入此支
                    if bestJ < 0:
                        bestX, bestXs = -1, 25.0
                        for j, r2 in enumerate(regions):
                            if j == i or r2 is None or r2.is_empty or \
                                    strokes[j].get("group") == gi:
                                continue
                            try:
                                share = pb.intersection(r2).area
                            except Exception:
                                continue
                            if share > bestXs:
                                bestXs, bestX = share, j
                        bestJ = bestX
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


def reUnionCheck(contours, strokes, glyph=None):
    """覆盖率/溢出率（shapely 面积精确计算）。"""
    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return {"cover": 0, "excess": 0}
    regions = []
    for s in strokes:
        if s["failed"]:
            continue
        r = _pathRegion(s["path"])
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


def clampStrokes(contours, strokes, excessTol=0.5, coverTol=99.5, glyph=None):
    """裁剪 + 残差回填。就地修改 strokes 的 path，返回收口后的 unionCheck。"""
    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return {"cover": 0, "excess": 0}

    regions = []
    for s in strokes:
        r = None
        if not s["failed"]:
            r = _pathRegion(s["path"])
        regions.append(r)

    # 1) 裁剪：越界删除；与字形交集为空（整笔落在墨外——退化重构环的
    #    奇偶区域可能整体翻到界外）时置 failed，交由饿死救济重建
    clamped = []
    replaced = [False] * len(strokes)
    for i, r in enumerate(regions):
        if r is None or r.is_empty:
            strokes[i]["path"] = ""
            strokes[i]["failed"] = True
            clamped.append(None)
            continue
        inter = r.intersection(glyph)
        if not inter.is_valid:
            inter = inter.buffer(0)
        if inter.is_empty or inter.area < 4.0:
            strokes[i]["path"] = ""
            strokes[i]["failed"] = True
            clamped.append(None)
            continue
        excess = r.area - inter.area
        if excess > max(4.0, r.area * excessTol / 100):
            replaced[i] = True
        clamped.append(inter)

    # 2) 残差回填：未覆盖面片给共享边界最长的笔。**图层隔离**：残差
    #    面片先按最大交叠定所属连通组，只允许该组的笔认领（悬浮部件
    #    与外组孔洞共享边界，跨组回填会把面片粘给别组的笔——直出组
    #    因此被外组污染）；本组无人接壤才放开全局。
    valid = [c for c in clamped if c is not None and not c.is_empty]
    if valid:
        union = unary_union(valid)
        if not union.is_valid:
            union = union.buffer(0)
        residual = glyph.difference(union)
        if not residual.is_empty and residual.area > max(4.0, glyph.area * (100 - coverTol) / 100):
            groupRegionCache = {}

            def _regionOfGroup(g):
                if g not in groupRegionCache:
                    groupRegionCache[g] = _groupRegionOf(contours, g)
                return groupRegionCache[g]

            allGroups = sorted({c.get("group", 0) for c in contours})
            pieces = list(residual.geoms) if hasattr(residual, "geoms") else [residual]
            for piece in pieces:
                if piece.area < 4:
                    continue
                pieceG, pgArea = None, 0.0
                for g in allGroups:
                    reg = _regionOfGroup(g)
                    if reg is None:
                        continue
                    try:
                        a = reg.intersection(piece).area
                    except Exception:
                        a = 0.0
                    if a > pgArea:
                        pgArea, pieceG = a, g
                pb = piece.buffer(1.5)

                def bestOwner(candidates):
                    bi, bl = -1, 0.0
                    for i in candidates:
                        c = clamped[i]
                        if c is None or c.is_empty:
                            continue
                        try:
                            shared = pb.intersection(c).area
                        except Exception:
                            shared = 0.0
                        if shared > bl:
                            bl, bi = shared, i
                    return bi

                sameG = [i for i in range(len(strokes))
                         if strokes[i].get("group") == pieceG]
                bestI = bestOwner(sameG) if pieceG is not None else -1
                if bestI < 0:
                    bestI = bestOwner(range(len(strokes)))
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
                # 小数舍入可能把刚好贴着边界的薄片压成往返线。判定
                # 必须针对写出的路径，否则 failed=False 会阻止后续
                # 饿死救济，造成整字并集正常但某一笔为空。
                written = _pathRegion(p)
                if written is None or written.is_empty or written.area < 4.0:
                    s["path"] = ""
                    s["failed"] = True
                    clamped[i] = None
                else:
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


# ---------------------------------------------------------------- 形态修复器：交界毛刺置换

def _nonzeroLargest(pathStr):
    """切割路径 → nonzero 语义单笔区域最大片（中轴图审计域）。切割路径
    继承字体原始绕向，奇偶合成会在保留片搭接处误挖假孔，Voronoi 中轴
    会被假孔搅出伪分叉——审计域用 nonzero（外环并集减反绕环并集）；
    手术域仍用 _pathRegion（奇偶=reUnionCheck/verify UNION 的消费语义，
    写出路径的区域按此恒等）。语义同 verify.strokeMorphRegion，独立
    实现（verify 与生产代码路径强制隔离，不能互相 import）。"""
    pairs = []
    for c in parseContours(pathStr):
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) < 4:
            continue
        try:
            pg = Polygon(pts)
            if not pg.is_valid:
                pg = pg.buffer(0)
            if not pg.is_empty:
                pairs.append((pg, signedArea(pts)))
        except Exception:
            pass
    if not pairs:
        return None
    outerSign = 1.0 if max(pairs, key=lambda t: abs(t[1]))[1] >= 0 else -1.0
    outers = [pg for pg, a in pairs if a * outerSign >= 0]
    holes = [pg for pg, a in pairs if a * outerSign < 0]
    try:
        region = unary_union(outers)
        if holes:
            region = region.difference(unary_union(holes))
    except Exception:
        return None
    if not region.is_valid:
        region = region.buffer(0)
    pieces = _piecesOf(region)
    if not pieces:
        return None
    region = max(pieces, key=lambda g: g.area)
    return region if region.area >= 25.0 else None


def _walkLeafChain(adj, leaf):
    """度1叶沿度≤2链走到头 → (链节点表, 链长, 挂点交叉节点|None)。
    与 geometry.medialJunctions / verify._morphWalkLeaf 同法——三处
    阈值语义不同（全字级120 / verify 单笔审计 / 本处修复域），且
    verify 与生产隔离不能互相 import，各自独立持有。"""
    chain = [leaf]
    chainLen = 0.0
    cur, prev = leaf, None
    while True:
        nbrs = [(v, w) for v, w in adj[cur].items() if v != prev]
        if not nbrs:
            return chain, chainLen, None
        v, w = nbrs[0]
        chainLen += w
        prev, cur = cur, v
        if len(adj[cur]) != 2:
            break
        chain.append(cur)
    return chain, chainLen, (cur if len(adj[cur]) >= 3 else None)


def _dropLeafChain(adj, chain):
    """从无向图 adj 中整链摘除（含挂边）。"""
    for node in chain:
        for v in list(adj.get(node, {})):
            adj[v].pop(node, None)
        adj.pop(node, None)


def _morphScan(region):
    """单笔中轴图形态勘察 → (交叉节点数, 短叶枝记录表, 伪臂候选表)
    | None。剪枝口径照 verify.morphAuditStroke（链长 < max(2×挂点余隙,
    40)）；剪稳后剩余图里 链长<MORPH_ARM_MAX 的叶链是伪臂候选（交界
    粘连尾巴长于笔宽、逃过 spur 阈，正是 verify 注释里"保留计入交叉
    节点"的形态病本体）。交叉节点数/短叶枝数与 verify 审计同口径，
    伪臂只作修复候选不进指标。"""
    adj = _medialAdjacency(region)
    if not adj:
        return None
    adj = {u: dict(vs) for u, vs in adj.items()}
    rings = [region.exterior] + list(region.interiors)

    def clearance(p):
        pt = Point(p)
        return min(r.distance(pt) for r in rings)

    spurs = []
    changed = True
    while changed:
        changed = False
        for leaf in [u for u, vs in adj.items() if len(vs) == 1]:
            if leaf not in adj or len(adj[leaf]) != 1:
                continue
            chain, chainLen, junc = _walkLeafChain(adj, leaf)
            if junc is None:
                continue
            if chainLen < max(2.0 * clearance(junc), MORPH_SPUR_FLOOR):
                spurs.append({"tip": leaf, "chain": chain,
                              "clrs": [clearance(p) for p in chain],
                              "len": chainLen})
                _dropLeafChain(adj, chain)
                changed = True
    arms = []
    for leaf in [u for u, vs in adj.items() if len(vs) == 1]:
        chain, chainLen, junc = _walkLeafChain(adj, leaf)
        if junc is None or chainLen >= MORPH_ARM_MAX:
            continue
        arms.append({"tip": leaf, "chain": chain,
                     "clrs": [clearance(p) for p in chain], "len": chainLen})
    juncN = sum(1 for vs in adj.values() if len(vs) >= 3)
    return juncN, spurs, arms


def _burrPatch(cur, spur):
    """毛刺补丁提取：叶枝沿链逐点圆盘（半径=2×该点余隙）并集 ∩ 本笔
    当前区域，取含链末端的那片。末端单圆盘不够——交界毛刺常是沿邻笔
    缝隙下垂的长条楔（永 横折钩沿捺左flank 187 长、楔尖余隙仅 0.4-3），
    补丁须罩住整条走廊且半径须随局部余隙走。两处几何卫生（永 标定
    实测）：①从末端起取**连续前缀**，余隙陡升处截断（>max(2×链余隙
    中位, 6)=已进交界肥厚区，散点保留会留缺口）；②圆盘并集先做闭运算
    （+r/−r 缓冲）再求交——不同半径圆盘拼接的扇贝形切口会让重扫
    Voronoi 喷出几十条假短叶枝，改善守卫恒否决。"""
    chain, clrs = spur["chain"], spur["clrs"]
    med = sorted(clrs)[len(clrs) // 2]
    cap = max(2.0 * med, 6.0)
    pref = []
    for p, c in zip(chain, clrs):
        if c > cap and len(pref) >= 2:
            break
        pref.append((p, c))
    disks = [Point(p).buffer(2.0 * max(c, 1.5), 8) for p, c in pref[::2]]
    disks.append(Point(pref[-1][0]).buffer(2.0 * max(pref[-1][1], 1.5), 8))
    try:
        zone = unary_union(disks)
        s = 2.0 * cap
        zone = zone.buffer(s, 8).buffer(-s, 8)
        patch = zone.intersection(cur)
    except Exception:
        return None
    pieces = _piecesOf(patch)
    if not pieces:
        return None
    pt = Point(spur["tip"])
    for pc in pieces:
        if pc.contains(pt) or pc.distance(pt) < 0.2:
            return pc
    return max(pieces, key=lambda g: g.area)


def _seamContact(patch, other):
    """补丁边界落在邻笔 tol 邻域内的长度。切割缝两侧路径各自细分并
    0.1 舍入，精确 boundary∩boundary 会漏，按膨胀带口径量。"""
    if other is None or other.is_empty:
        return 0.0
    try:
        if patch.distance(other) > MORPH_SEAM_TOL:
            return 0.0
        return patch.boundary.intersection(other.buffer(MORPH_SEAM_TOL)).length
    except Exception:
        return 0.0


def _bigPieces(region, refArea):
    return [g for g in _piecesOf(region)
            if g.area >= max(25.0, refArea * 0.02)]


def _scanScore(region):
    """区域最大片的 (交叉+短叶枝) 合计分 | None。受让方传染守卫用。"""
    main = max(_piecesOf(region), key=lambda g: g.area, default=None)
    if main is None:
        return None
    scan = _morphScan(main)
    if not scan:
        return None
    return scan[0] + len(scan[1])


def _repairOneStroke(strokes, regions, k, mates):
    """单笔毛刺勘察+试置换（不落盘）→ (新本笔区域, {受让笔:[补丁并,
    数,并后区域]}) | None。置换判据：补丁与邻笔的边界接触长度 > 补丁
    挂回本笔主体的颈宽（圆弧割线长，偏保守）——"挂在别人身上的肉"
    才转让。守卫：①补丁单块/合计面积上限（retain 面积化口径）；②逐
    补丁不破单连通；③置换后本笔中轴图指标净改善且交叉不升；④受让方
    传染守卫——重病笔（形态分>MORPH_RECV_SICK）不受让 + 受让侧恶化
    合计≤2×本笔改善量（simkai 草曾出现 横折 把残片塞给已病的 #5横，
    本笔小赚邻笔大亏，verify 侧 junc 30→37 净恶化）。"""
    own = regions[k]
    big0 = _bigPieces(own, own.area)
    if not big0:
        return None
    main0 = max(big0, key=lambda g: g.area)
    audit = _nonzeroLargest(strokes[k]["path"])
    if audit is None:
        return None
    # 域一致性门：审计域（nonzero）与手术域（奇偶）不一致的笔跳过——
    # 保留片搭接被奇偶误挖出假孔时两域中轴拓扑不可比，改善守卫失真；
    # 手术若照奇偶写出会把假孔实体化，照 nonzero 写出会改并集语义。
    try:
        if audit.symmetric_difference(main0).area > max(1.0, own.area * 0.002):
            return None
    except Exception:
        return None
    scan0 = _morphScan(main0)
    if not scan0:
        return None
    junc0, spurs0, arms0 = scan0
    if not spurs0 and not arms0:
        return None
    # 执行域门：只修交叉节点型病笔（junc0≥1）。spur-only 笔（衬线锯齿
    # 底噪）的批量搬运在标定中净害——漱 spur 267→297、攮 morphBad
    # 2→4(码型迁移)、健康集 筻 经"置换→种子→自洽二遍"级联 retain
    # -0.027；而毛刺/枝桠必挂交叉点（伪臂按定义挂在交叉点上，junc0=0
    # 时 arms 恒空，标定 13 个 donor 行 11/11 实测），锯齿型不在射程。
    if junc0 < 1:
        return None
    cur = own
    gains = {}
    recvScore = {}      # 受让候选形态分惰性缓存（每邻笔最多一次 Voronoi）
    for sp in spurs0 + arms0:
        patch = _burrPatch(cur, sp)
        if patch is None or patch.area < 9.0 or \
                patch.area > own.area * MORPH_PATCH_MAX:
            continue
        try:
            rest = cur.difference(patch)
            if not rest.is_valid:
                rest = rest.buffer(0)
            if len(_bigPieces(rest, own.area)) != len(big0):
                continue
            neck = patch.boundary.intersection(rest).length
        except Exception:
            continue
        bestJ, bestC = -1, max(neck, MORPH_CONTACT_MIN)
        for j in mates:
            if regions[j] is None or \
                    patch.area > regions[j].area * MORPH_RECV_MAX:
                continue
            if j not in recvScore:
                recvScore[j] = _scanScore(regions[j])
            if recvScore[j] is None or recvScore[j] > MORPH_RECV_SICK:
                continue
            c = _seamContact(patch, regions[j])
            if c > bestC:
                bestC, bestJ = c, j
        if bestJ < 0:
            continue
        cur = rest
        if bestJ in gains:
            gains[bestJ][0] = gains[bestJ][0].union(patch)
            gains[bestJ][1] += 1
        else:
            gains[bestJ] = [patch, 1]
    if not gains or own.area - cur.area > own.area * MORPH_SHARE_MAX:
        return None
    mainNew = max(_piecesOf(cur), key=lambda g: g.area, default=None)
    if mainNew is None:
        return None
    scan2 = _morphScan(mainNew)
    if not scan2:
        return None
    junc2, spurs2, _arms2 = scan2
    # 改善守卫：交叉节点不得上升，交叉+短叶枝合计必须严格下降（合计
    # 门允许切口处冒出个别新短叶枝，但只在交叉下降更多时放行——永
    # 横折钩实测 junc 7→0、spur 15→17，合计 22→17）
    if junc2 > junc0 or junc2 + len(spurs2) >= junc0 + len(spurs0):
        return None
    # 受让方传染守卫：受让侧恶化合计 ≤ 2×本笔改善量。静态合计对
    # "毛边换主"近守恒（锯齿边归谁谁挨罚），真收益在下游——一遍置换
    # →种子更干净→自洽二遍重切整体更优（永 静态 NET+4 但 verify 终态
    # junc 7→2 spur 19→11）；绝对失控（受让侧恶化远超本笔改善）仍拦
    donorGain = (junc0 + len(spurs0)) - (junc2 + len(spurs2))
    if donorGain < MORPH_MIN_GAIN:
        return None
    recvDelta = 0
    for j, entry in gains.items():
        m2 = regions[j].union(entry[0])
        if not m2.is_valid:
            m2 = m2.buffer(0)
        after = _scanScore(m2)
        if after is None:
            return None
        recvDelta += after - (recvScore.get(j) or 0)
        entry.append(m2)     # 并后受让区域随包带出，commit 免重算
    if recvDelta > 2 * donorGain:
        return None
    return cur, gains


def _commitRepair(strokes, regions, k, trial):
    """置换落盘（整包 all-or-nothing）：受让侧试并+双侧路径重建全部
    通过才写；受让笔不得新增显著碎片（补丁与邻笔只是 0.6 带内接触、
    实际隔缝时并集会成飞地——那是 SPLIT，整笔放弃）。返回
    [[笔, 补丁数, 受让笔], ...]（失败返回 []，一切未动）。"""
    cur, gains = trial
    merged, newPaths = {}, {}
    for j, (patch, _n, m2) in gains.items():
        p2 = _regionToPath(m2)
        if not p2 or len(_bigPieces(m2, m2.area)) > \
                len(_bigPieces(regions[j], regions[j].area)):
            return []
        merged[j], newPaths[j] = m2, p2
    pK = _regionToPath(cur)
    if not pK:
        return []
    written = _pathRegion(pK)
    if written is None or written.is_empty or written.area < 4.0:
        return []
    strokes[k]["path"] = pK
    strokes[k]["clamped"] = True
    regions[k] = cur
    out = []
    for j, (patch, n, _m2) in gains.items():
        strokes[j]["path"] = newPaths[j]
        strokes[j]["clamped"] = True
        regions[j] = merged[j]
        out.append([k, n, j])
    return out


def morphRepairStrokes(strokes):
    """交界毛刺置换（形态修复器，MORPH_REPAIR 开关）：衬线体交叉组
    切割后交界带留下的毛边/枝桠（simsun 永 横折钩 retain0.82+2桥）是
    "挂在别人身上的肉"——短叶枝末端的轮廓局部凸起若与某同组邻笔的
    边界接触长度大于其挂回本笔主体的颈宽，则整块转让给该邻笔。转让
    是 difference/union 对偶（补丁只在两笔间换主），并集恒等天然保持。
    挂点=收口链末端（finalize.sealUnion，归属/切割/救济/补缝已尘埃
    落定，只做交界再划界）。返回 [[笔, 补丁数, 受让笔], ...]。"""
    repairs = []
    regions = [None if s["failed"] else _pathRegion(s["path"])
               for s in strokes]
    groupIdx = {}
    for i, s in enumerate(strokes):
        if not s["failed"] and regions[i] is not None \
                and not regions[i].is_empty:
            groupIdx.setdefault(s.get("group"), []).append(i)
    for k, s in enumerate(strokes):
        if s["failed"] or regions[k] is None or regions[k].is_empty \
                or regions[k].area < 100.0:
            continue
        if s.get("retainRatio", 1.0) >= MORPH_RETAIN_GATE and \
                (s.get("bridges") or 0) < MORPH_BRIDGE_GATE:
            continue
        mates = [j for j in groupIdx.get(s.get("group"), []) if j != k]
        if not mates:
            continue    # 无同组邻笔=无受让方，免付 Voronoi
        trial = _repairOneStroke(strokes, regions, k, mates)
        if trial is not None:
            repairs.extend(_commitRepair(strokes, regions, k, trial))
    return repairs
