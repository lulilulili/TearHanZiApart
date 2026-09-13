# -*- coding: utf-8 -*-
"""strokelab.pipeline — 拆解管线：D 构建 → 归属 → 精调 → 矢量切割 → 划分式重构。

流程（v5 架构）：
  D = B库同类型笔画骨架（A↔B 映射产物，目标字体自己的形态）按楷体结构 C 定位；
  D 与目标字形实际路径匹配 → 归属/迭代精调 → 标签跳变处 De Casteljau 精确切割 →
  环路追踪 + 直割线弦 + 跨轮廓并环重构 → 布尔收口（boolean.clampStrokes）保证
  所有笔画并集与原字形恒等（不多不少，交叠区双重归属）。
"""

import math
import time

from ..geometry import (dist, lineSeg, cubicSeg, parseContours, contourToPath,
                       nearestBatch,
                       flattenSegs,
                       bboxOfPoints, pointInPolygon, nearestOnPolyline,
                       polylineLength, resamplePolyline, analyzeContours,
                       bezPoint, bezTangent, bezSlice, segLength,
                       shapeDescriptor, shapeSimilarity, refineMedianFit,
                       recenterMedian, corridorPoint,
                       outlineCenterline, midpointRectify,
                       straightenSections, signedArea)
from ..classify import (findLibEntry, similarTypes, PROBE_TABLE,
                       semanticSegments, matchTier)
from .. import boolean as booleanClamp

ITERS = 5

# G8.5 梯队探针开关：开启时 result 附带 ladderProbe 信号（标定用），
# 探测本身无副作用。执行器待探测精度在 25 字族+健康集上标定后接入。
LADDER_PROBE = False
# G8.5 梯队执行器开关：探测标定达标（家族14/25、健康0/23、抽样0/200）
# 后接入。按 fired 部件的 plan 施行组重排+中轴带重置。
LADDER_ACT = True

from .helpers import (_medianDeviation, _hungarian, _switchbackCount,
                      _axisFails, _reMedianFromStroke, _selfSeeds,
                      _meanOf, _strokeCenter, _secondPassBetter)
from .state import PipelineCtx
from . import grouping
from . import dbuild
from . import assign
from . import arbitrate


def runPipeline(dataHub, fontEntry, ch, applyBooleanClamp=True,
                seedMedians=None, selfConsistent=True):
    kai = dataHub.kai(ch)
    if not kai:
        return {"error": "MakeMeAHanzi 中没有「%s」的笔画数据" % ch}
    raw = fontEntry.glyphContours(ch)
    if not raw:
        return {"error": "字体 %s 中没有「%s」字形" % (fontEntry.key, ch)}

    ctx = PipelineCtx(dataHub=dataHub, fontEntry=fontEntry, ch=ch,
                      applyBooleanClamp=applyBooleanClamp,
                      seedMedians=seedMedians,
                      selfConsistent=selfConsistent, kai=kai, raw=raw)

    grouping.parseAndMerge(ctx)
    contours = ctx.contours

    # 计时器入 ctx（PipelineCtx.tick 与原 _tick 闭包逐字节同款：毫秒取整）
    ctx.startTimer()
    _timings = ctx.timings
    _tick = ctx.tick

    dbuild.run(ctx)
    kaiStrokeBBoxes = ctx.kaiStrokeBBoxes
    tb = ctx.tb
    affine = ctx.affine
    medians = ctx.medians
    initMedians = ctx.initMedians
    templateSources = ctx.templateSources
    templateEnts = ctx.templateEnts
    nStrokes = ctx.nStrokes


    grouping.buildTables(ctx)
    nGroups = ctx.nGroups
    groupOuters = ctx.groupOuters
    groupHoles = ctx.groupHoles
    groupCentroids = ctx.groupCentroids
    groupBBoxes = ctx.groupBBoxes
    groupCoarse = ctx.groupCoarse


    assign.run(ctx)
    strokeGroupCost = ctx.strokeGroupCost
    costRows = ctx.costRows
    penMatrix = ctx.penMatrix
    strokeGroup = ctx.strokeGroup
    groupStrokes = ctx.groupStrokes
    semanticClaims = ctx.semanticClaims

    arbitrate.run(ctx)
    slotSwaps = ctx.slotSwaps
    kaiMatches0 = ctx.kaiMatches0
    ladderProbe = ctx.ladderProbe
    ladderRealign = ctx.ladderRealign
    ladderTouched = ctx.ladderTouched

    # 组局部重锚定（失配门控，用户设想：相对位置代替绝对位置）：全局
    # 仿射是绝对定位，部件比例悬殊时组内名义布局整体错位——磷·石口
    # 高瘦，楷体口的三笔名义全挤在组上半段，封底横悬在腔体中间，组下
    # 三分之一无人认领。组内≥2笔、成员名义联合框对组墨框轴向覆盖
    # <0.7 时，改用楷体**相对布局**：联合名义框→组墨框整体仿射重映射
    # （轴对齐缩放，横竖臂保持横竖）。v10 无门控全量复位曾净负收益
    # ——放对的也被搬乱；门控确保只救真错位，正常字零扰动。
    groupRemapInfo = []
    for g in range(nGroups):
        if g in ladderTouched:
            # 梯队执行器刚按条带 y 锚定过的组：G9 的整组仿射复位会把
            # 带位搬走（自己造成的联合框覆盖缺口自己豁免）
            continue
        ss = groupStrokes.get(g, [])
        if len(ss) < 2:
            continue
        bb = groupBBoxes.get(g)
        if bb is None or bb.w < 8 or bb.h < 8:
            continue
        # 守卫：无孔单杆组内仍有≥2条与杆平行的笔=未解决的超载状态，
        # 重锚定把双横一起压进单横带只会钉死错误（甲曾 cov[1.0,0.5]
        # 触发把笔2压成高2的切片），交给 G4 超载重指派处理
        outersG = [c for c in contours if c["group"] == g and not c["isHole"]]
        if len(outersG) == 1 and not any(
                c["group"] == g and c["isHole"] for c in contours):
            aspectG = max(bb.w, bb.h) / max(1.0, min(bb.w, bb.h))
            if aspectG >= 3.0:
                barAngG = 0.0 if bb.w >= bb.h else 90.0
                nPar = 0
                for k in ss:
                    mk = initMedians[k]
                    aK = math.degrees(math.atan2(
                        mk[-1][1] - mk[0][1], mk[-1][0] - mk[0][0])) % 180.0
                    d0 = abs(aK - barAngG) % 180.0
                    if min(d0, 180.0 - d0) <= 30.0:
                        nPar += 1
                if nPar >= 2:
                    continue
        pts = [p for k in ss for p in initMedians[k]]
        nb = bboxOfPoints(pts)
        if nb.w < 4 or nb.h < 4:
            continue
        covX = max(0.0, min(nb.x1, bb.x1) - max(nb.x0, bb.x0)) / max(1.0, bb.w)
        covY = max(0.0, min(nb.y1, bb.y1) - max(nb.y0, bb.y0)) / max(1.0, bb.h)
        import os as _os
        if _os.environ.get("SL_DEBUG_REMAP"):
            print("DEBUG组%d 笔%s nb(%d,%d..%d,%d) bb(%d,%d..%d,%d) cov %.2f/%.2f" % (
                g, [k+1 for k in ss], nb.x0, nb.y0, nb.x1, nb.y1,
                bb.x0, bb.y0, bb.x1, bb.y1, covX, covY))
        if min(covX, covY) >= 0.8:
            continue  # 0.7→0.8：吃的口横折竖臂把联合框拉高、覆盖0.70压线
                      # 漏网，底横仍悬空250单位颗粒无收（口底横族234字）；
                      # 覆盖0.7-0.8区间的重锚定只是≤1.4×温和拉伸，失真小
        sx2 = bb.w / nb.w
        sy2 = bb.h / nb.h
        if not (0.25 <= sx2 <= 4.0 and 0.25 <= sy2 <= 4.0):
            continue
        for k in ss:
            medians[k] = [(bb.x0 + (p[0] - nb.x0) * sx2,
                           bb.y0 + (p[1] - nb.y0) * sy2)
                          for p in medians[k]]
            initMedians[k] = [tuple(p) for p in medians[k]]
        groupRemapInfo.append({
            "group": g, "strokes": list(ss),
            "from": [round(nb.x0), round(nb.y0), round(nb.x1), round(nb.y1)],
            "to": [round(bb.x0), round(bb.y0), round(bb.x1), round(bb.y1)],
            "cov": [round(covX, 2), round(covY, 2)]})

    # D 断面吸附（组内法向滑动预对位）：bbox 重锚定是线性映射，部件
    # 内部的非线性比例差仍会把封底横放进腔体——楷体口的底横在竖臂
    # 高度的 25-36% 处、鸿蒙高瘦口是 0-11% 的贴底带，重锚定后仍悬空
    # 100+ 单位、迭代中颗粒无收（吃/口底横族 234+ 字 TYPE 失败）。
    # 对走廊支撑贫瘠的横/竖笔，沿法向细扫组内最大单片支撑位吸附；
    # 同向笔已占位（法向距<40）不吸附，吸附后同向笔楷体次序必须保持。
    try:
        from shapely.geometry import (Polygon as _Pg2, LineString as _Ls2,
                                      Point as _PtSnap)
        from shapely.affinity import translate as _Tr2
        _regCache2 = {}

        def _regOf2(g):
            if g not in _regCache2:
                reg = None
                for poly in groupOuters[g]:
                    pg = _Pg2(poly)
                    if not pg.is_valid:
                        pg = pg.buffer(0)
                    reg = pg if reg is None else reg.union(pg)
                if reg is not None:
                    for poly in groupHoles[g]:
                        pg = _Pg2(poly)
                        if not pg.is_valid:
                            pg = pg.buffer(0)
                        reg = reg.difference(pg)
                    if not reg.is_valid:
                        reg = reg.buffer(0)
                _regCache2[g] = reg
            return _regCache2[g]

        def _bigPiece2(geom):
            best = 0.0
            for gm in getattr(geom, "geoms", [geom]):
                a = getattr(gm, "area", 0.0)
                if a > best:
                    best = a
            return best

        def _lineSupport2(line, region):
            """中轴在线条墨带内的最长连续比例；横穿两侧竖壁不算横带。"""
            inter = line.intersection(region)
            return max((getattr(gm, "length", 0.0)
                        for gm in getattr(inter, "geoms", [inter])),
                       default=0.0) / max(1.0, line.length)

        kaiCent = [(sum(p[0] for p in m2) / len(m2),
                    sum(p[1] for p in m2) / len(m2))
                   for m2 in kai["medians"]]
        for g in range(nGroups):
            ss = groupStrokes.get(g, [])
            if len(ss) < 2:
                continue
            axStrokes = [k for k in ss
                         if kai["strokeTypes"][k] in ("横", "竖")]
            if not axStrokes:
                continue
            reg = _regOf2(g)
            if reg is None or reg.is_empty:
                continue
            bb = groupBBoxes.get(g)
            if bb is None:
                continue
            span = max(bb.w, bb.h)
            placedPos = {}
            for k in ss:
                m2 = medians[k]
                placedPos[k] = (sum(p[0] for p in m2) / len(m2),
                                sum(p[1] for p in m2) / len(m2))
            for k in axStrokes:
                t = kai["strokeTypes"][k]
                m2 = medians[k]
                if len(m2) < 2:
                    continue
                ddx = m2[-1][0] - m2[0][0]
                ddy = m2[-1][1] - m2[0][1]
                L = math.hypot(ddx, ddy)
                if L < 8:
                    continue
                nx1, ny1 = -ddy / L, ddx / L
                try:
                    axisLine = _Ls2([tuple(p) for p in m2])
                    cor = axisLine.buffer(24.0)
                    sup0 = _bigPiece2(cor.intersection(reg))
                    axisSup0 = _lineSupport2(axisLine, reg)
                except Exception:
                    continue
                # 贫瘠判定按走廊标称面积的占比：横走廊横穿竖壁也能蹭到
                # ~2000 支撑（两片壁肉），绝对阈值会漏掉真悬空的封底横
                corArea = L * 48.0
                if sup0 >= corArea * 0.35 and axisSup0 >= 0.65:
                    continue
                cands2 = []
                for i2 in range(-12, 13):
                    off = span * 0.5 * i2 / 12.0
                    if abs(off) < 1:
                        continue
                    try:
                        movedLine = _Tr2(axisLine, xoff=nx1 * off,
                                        yoff=ny1 * off)
                        if _lineSupport2(movedLine, reg) < 0.8:
                            continue
                        sv = _bigPiece2(_Tr2(cor, xoff=nx1 * off,
                                             yoff=ny1 * off).intersection(reg))
                    except Exception:
                        continue
                    cands2.append((sv, abs(off), off))
                if not cands2:
                    continue
                thr2 = max(corArea * 0.65, 1.25 * max(sup0, 1.0))
                good2 = [c for c in cands2 if c[0] >= thr2]
                if not good2:
                    continue
                # 达标位置取位移最小（同救济的吸附准则）：全局最大会
                # 跨越腔体跳到远端他笔的杆上（肝的左竖曾+334跳上右壁）
                good2.sort(key=lambda c: c[1])
                bestS, _, bestT = good2[0]
                # 占位冲突（任意类型）：目标走廊被他笔中轴线实质占据则
                # 放弃（横折钩的竖臂曾因异型不查而被竖鸠占）
                tgtCor = _Tr2(cor, xoff=nx1 * bestT, yoff=ny1 * bestT)
                occupied = False
                for k2 in ss:
                    if k2 == k:
                        continue
                    m3 = medians[k2]
                    step3 = max(1, len(m3) // 10)
                    pts3 = m3[::step3]
                    try:
                        inC = sum(1 for p in pts3
                                  if tgtCor.contains(_PtSnap(p[0], p[1])))
                    except Exception:
                        continue
                    if inC >= 0.5 * len(pts3):
                        occupied = True
                        break
                if occupied:
                    continue
                import os as _os2
                if _os2.environ.get("SL_DEBUG_SNAP"):
                    print("SNAP 笔%d %s 组%d off=%.0f sup %.0f->%.0f" % (
                        k + 1, t, g, bestT, sup0, bestS))
                newC = (placedPos[k][0] + nx1 * bestT,
                        placedPos[k][1] + ny1 * bestT)
                conflict = False
                for k2 in ss:
                    if k2 == k or kai["strokeTypes"][k2] != t:
                        continue
                    dPerp = abs((placedPos[k2][0] - newC[0]) * nx1 +
                                (placedPos[k2][1] - newC[1]) * ny1)
                    if dPerp < 40.0:
                        conflict = True
                        break
                    sK = (kaiCent[k][0] - kaiCent[k2][0]) * nx1 + \
                         (kaiCent[k][1] - kaiCent[k2][1]) * ny1
                    sT = (newC[0] - placedPos[k2][0]) * nx1 + \
                         (newC[1] - placedPos[k2][1]) * ny1
                    if sK * sT < 0:
                        conflict = True
                        break
                if conflict:
                    continue
                medians[k] = [(p[0] + nx1 * bestT, p[1] + ny1 * bestT)
                              for p in medians[k]]
                initMedians[k] = [tuple(p) for p in medians[k]]
                placedPos[k] = newC
    except Exception:
        pass

    contourAllowed = [groupStrokes.get(c["group"], list(range(nStrokes)))
                      for c in contours]

    _tick("连通组分治")

    # ------------------------------------------------------------ 迭代归属+精调
    glyphArea = sum((-abs(c["area"]) if c["isHole"] else abs(c["area"]))
                    for c in contours)
    totalMedianLen = sum(polylineLength(m) for m in medians) or 1.0
    w0 = max(14.0, min(180.0, abs(glyphArea) / totalMedianLen))
    widths = [w0] * nStrokes
    scoreWidths = list(widths)

    def extendMedian(m, ext):
        if len(m) < 2 or ext <= 0:
            return m
        t0x, t0y = m[0][0] - m[1][0], m[0][1] - m[1][1]
        L0 = math.hypot(t0x, t0y) or 1.0
        tnx, tny = m[-1][0] - m[-2][0], m[-1][1] - m[-2][1]
        Ln = math.hypot(tnx, tny) or 1.0
        return [(m[0][0] + t0x / L0 * ext, m[0][1] + t0y / L0 * ext)] + list(m) + \
               [(m[-1][0] + tnx / Ln * ext, m[-1][1] + tny / Ln * ext)]

    scoreMedians = [extendMedian(m, min(70.0, widths[k] * 1.1))
                    for k, m in enumerate(medians)]

    sampleSets = []
    for c in contours:
        arr = []
        for si, seg in enumerate(c["segs"]):
            n = max(3, min(26, int(math.ceil(segLength(seg) / 9))))
            for kk in range(n):
                t = kk / n
                arr.append({"s": si + t, "pt": bezPoint(seg, t),
                            "tan": bezTangent(seg, t), "label": 0})
        sampleSets.append(arr)

    def scoreOf(pt, tan, k):
        near = nearestOnPolyline(pt, scoreMedians[k])
        halfW = scoreWidths[k] * 0.5 + 6
        dirPen = 1 - abs(tan[0] * near["tan"][0] + tan[1] * near["tan"][1])
        return near["d"] / halfW + 0.5 * dirPen

    def labelOf(pt, tan, allowed=None):
        cand = allowed if allowed is not None else range(nStrokes)
        best, bestScore = next(iter(cand)), 1e18
        for k in cand:
            sc = scoreOf(pt, tan, k)
            if sc < bestScore:
                bestScore, best = sc, k
        return best

    usedKaiFallback = [False] * nStrokes
    # 批量评分预备：样本点/切向在迭代间不变，一次性排成数组
    import numpy as _np
    _flatSamples = [sm for arr in sampleSets for sm in arr]
    _flatCi = [ci for ci, arr in enumerate(sampleSets) for _ in arr]
    _ptsArr = _np.array([sm["pt"] for sm in _flatSamples], dtype=_np.float64)         if _flatSamples else _np.zeros((0, 2))
    _tanArr = _np.array([sm["tan"] for sm in _flatSamples], dtype=_np.float64)         if _flatSamples else _np.zeros((0, 2))
    _ciArr = _np.array(_flatCi, dtype=_np.int64)
    for it in range(ITERS):
        assigned = [[] for _ in range(nStrokes)]
        # 逐样本评分批量化（语义与 labelOf 等价：分数矩阵按 allowed 顺序
        # argmin，并列取先者；nearestBatch 与标量版逐位一致）
        _scores = _np.empty((nStrokes, len(_flatSamples)), dtype=_np.float64)
        for k in range(nStrokes):
            d, _, _, tx, ty, _ = nearestBatch(_ptsArr, scoreMedians[k])
            halfW = scoreWidths[k] * 0.5 + 6
            dirPen = 1.0 - _np.abs(_tanArr[:, 0] * tx + _tanArr[:, 1] * ty)
            _scores[k] = d / halfW + 0.5 * dirPen
        _labels = _np.empty(len(_flatSamples), dtype=_np.int64)
        for ci in range(len(sampleSets)):
            mask = _ciArr == ci
            if not mask.any():
                continue
            alw = _np.array(contourAllowed[ci], dtype=_np.int64)
            sub = _scores[alw][:, mask]
            _labels[mask] = alw[_np.argmin(sub, axis=0)]
        for i, sm in enumerate(_flatSamples):
            sm["label"] = int(_labels[i])
            assigned[sm["label"]].append(sm["pt"])
        # 饿死自救：某笔颗粒无收 ⇒ 其 B 模板骨架劣质/错位（如 TC 竖变体），
        # D 退回楷体中轴线重新参赛——楷体位置由结构 C 保证，只输形态不输位置
        for k in range(nStrokes):
            if len(assigned[k]) < 6 and not usedKaiFallback[k]:
                usedKaiFallback[k] = True
                medians[k] = [affine(p) for p in kai["medians"][k]]
                initMedians[k] = [tuple(p) for p in medians[k]]
                scoreMedians[k] = extendMedian(
                    medians[k], min(70.0, widths[k] * 1.1))
        for k in range(nStrokes):
            pts = assigned[k]
            if not pts:
                continue
            d, _, _, _, _, _ = nearestBatch(pts, medians[k])
            ds = _np.sort(d)
            widths[k] = max(10.0, min(220.0, 2 * float(ds[len(ds) // 2])))
        wMed = sorted(widths)[len(widths) // 2] or w0
        scoreWidths = [max(0.55 * wMed, min(1.6 * wMed, w)) for w in widths]
        if it >= ITERS - 1:
            break
        for k in range(nStrokes):
            if len(assigned[k]) >= 6:
                medians[k] = refineMedianFit(medians[k], initMedians[k],
                                             assigned[k], widths[k])
                # 总长度硬约束：防止小笔画（点）被逐轮伸缩复利拉爆——
                # 点的 D 一旦变成长对角线会侵占邻笔区域、令主人判定失火
                newLen = polylineLength(medians[k])
                initLen = polylineLength(initMedians[k]) or 1.0
                ratio = newLen / initLen
                if ratio > 1.4 or ratio < 0.6:
                    f = (1.4 if ratio > 1.4 else 0.6) / ratio
                    m = medians[k]
                    mcx = sum(p[0] for p in m) / len(m)
                    mcy = sum(p[1] for p in m) / len(m)
                    medians[k] = [(mcx + (p[0] - mcx) * f,
                                   mcy + (p[1] - mcy) * f) for p in m]
                # 垂直断面居中：矫正只按己方样本拟合造成的贴边。
                # maxOff 硬上限：对整字轮廓居中时，交叉区断面中点在两笔
                # 之间跳（汉·横撇曾蛇形折返），超半笔宽的"修正"一律拒绝
                medians[k] = recenterMedian(
                    medians[k], contours,
                    max(1.6 * widths[k], 1.2 * w0, 40.0),
                    maxOff=0.6 * widths[k])
        scoreMedians = [extendMedian(m, min(70.0, widths[k] * 1.1))
                        for k, m in enumerate(medians)]

    # 终态收口：精调/断面居中在融合区残留的之字抖动统一吸直（与 B 骨架
    # 同款后处理：直段吸直+拐角坍缩、真曲段保持）。D′ 中轴线是笔画中线
    # 的最终陈述，拓扑必须与骨架一致——横钩不该折返几十次。
    for k in range(nStrokes):
        cleaned = straightenSections(medians[k])
        if len(cleaned) >= 2:
            medians[k] = cleaned

    _tick("归属迭代精调")

    # S2 模板可视化：把 B 模板画在精调后的 D 位置（精调后中轴线包围盒 + 半笔宽），
    # 与匹配实际使用的几何一致，避免初始放置的视觉重叠误导
    templatePaths = []
    for k in range(nStrokes):
        ent = templateEnts[k]
        if not ent:
            templatePaths.append("")
            continue
        mb = bboxOfPoints(medians[k])
        half = widths[k] * 0.55
        fx0, fy0 = mb.x0 - half, mb.y0 - half
        fw = mb.w + 2 * half
        fh = mb.h + 2 * half
        eb = ent["outlineBBox"]
        ew = max(1.0, eb[2] - eb[0])
        ehh = max(1.0, eb[3] - eb[1])

        def mv(p, fx0=fx0, fy0=fy0, fw=fw, fh=fh, eb=eb, ew=ew, ehh=ehh):
            return (fx0 + (p[0] - eb[0]) * fw / ew,
                    fy0 + (p[1] - eb[1]) * fh / ehh)

        parts = []
        for d in ent["contours"]:
            for c in parseContours(d):
                moved = [(sg[0], mv(sg[1]), mv(sg[2]), mv(sg[3]), mv(sg[4]))
                         for sg in c["segs"]]
                parts.append(contourToPath(moved))
        templatePaths.append(" ".join(parts))

    # ------------------------------------------------------------ 主人判定整体归属
    resampledMedians = [resamplePolyline([tuple(p) for p in m], 15) for m in medians]
    resampledInit = [resamplePolyline([tuple(p) for p in m], 15) for m in initMedians]
    strokeSampleTotals = [0] * nStrokes
    for arr in sampleSets:
        for sm in arr:
            strokeSampleTotals[sm["label"]] += 1

    def fracInside(rm, poly):
        cnt = sum(1 for p in rm if pointInPolygon(p, poly))
        return cnt / (len(rm) or 1)

    for ci, c in enumerate(contours):
        if c["isHole"]:
            continue
        votes = {}
        for sm in sampleSets[ci]:
            votes[sm["label"]] = votes.get(sm["label"], 0) + 1
        nSamp = len(sampleSets[ci]) or 1
        fracs = []
        for k in contourAllowed[ci]:
            fr = fracInside(resampledMedians[k], c["poly"])
            fi = fracInside(resampledInit[k], c["poly"])
            fracs.append((max(fr, fi), min(fr, fi), fi, fr, k))
        fracs.sort(key=lambda x: -x[0])
        fMax1, fMin1, _fi1, fr1, winner = fracs[0]
        # 竞争者：设计位置（初始或精调后）确实占住轮廓，且实际拥有本轮廓
        # 可观样本份额（≥20%）才算——中轴线只是路过/起点搭在别人身上而
        # 抢不到样本的（天的捺起点在撇杆内）不算竞争，不该挡住正主整体归属
        fMax2 = 0.0
        for f in fracs[1:]:
            if votes.get(f[4], 0) >= 0.2 * nSamp:
                fMax2 = max(fMax2, f[0])
        if fMax2 >= 0.45:
            continue
        # 主人资格：初始+精调都占住（常规）；或初始落位失败但精调深度收敛
        # 且已实际拥有多数样本（天的撇 fi=0 fr=0.88、握有 69% 样本）
        if not ((fMax1 >= 0.55 and fMin1 >= 0.35) or
                (fr1 >= 0.8 and votes.get(winner, 0) >= 0.6 * nSamp)):
            continue
        # 饿死保护：接管会令某笔别处仅剩极少样本（绝对），或一次夺走该笔
        # 过半样本（相对——被夺一半以上说明它在此轮廓有实质领地）都跳过
        starve = any(k != winner and (strokeSampleTotals[k] - v < 6
                     or v >= 0.5 * strokeSampleTotals[k])
                     for k, v in votes.items())
        if starve:
            continue
        for sm in sampleSets[ci]:
            if sm["label"] != winner:
                strokeSampleTotals[sm["label"]] -= 1
                strokeSampleTotals[winner] += 1
                sm["label"] = winner

    # ------------------------------------------------------------ 边弧整体归属
    # 印刷字形的笔画边界天然落在轮廓角点：按角点把轮廓切成边弧，整条边弧
    # 按平均得分整体归属——直边中途不再出现碎片切换（口的左竖外缘曾被
    # 横的端部评分蚕食出多段）。无角点的平滑字体、以及确有成块分歧的长弧
    # 保留逐样本标签。顺序：先外轮廓收敛 → 孔洞径向对应 → 孔洞按多数票
    # （径向对应必须继承整弧修正后的外缘标签，反过来会带病投票）
    cornerSets = []
    for c in contours:
        segs = c["segs"]
        nSeg = len(segs)
        corners = set()
        for j in range(nSeg):
            tA = bezTangent(segs[j - 1], 1.0)
            tB = bezTangent(segs[j], 0.0)
            if tA[0] * tB[0] + tA[1] * tB[1] < math.cos(math.radians(40)):
                corners.add(j % nSeg)
        cornerSets.append(corners)
    wMedFinal = sorted(widths)[len(widths) // 2] or w0
    # 饿死保护基数只数非孔洞样本：孔洞标签稍后镜像外缘，外缘被整弧改判的
    # 损失会在孔洞上翻倍，用全量基数会让保护被绕过（TC"中"左竖曾因此饿死）
    arcTotals = [0] * nStrokes
    for cj, arr in enumerate(sampleSets):
        if contours[cj]["isHole"]:
            continue
        for sm in arr:
            arcTotals[sm["label"]] += 1

    def consolidateContour(ci):
        c = contours[ci]
        arr = sampleSets[ci]
        cs = sorted(cornerSets[ci])
        if len(cs) < 2 or not arr:
            return
        arcs = {}
        for i, sm in enumerate(arr):
            j = 0
            for jj, cv in enumerate(cs):
                if cv <= sm["s"]:
                    j = jj
                elif cv > sm["s"]:
                    break
            if sm["s"] < cs[0]:
                j = len(cs) - 1
            arcs.setdefault(j, []).append(i)
        for idxs in arcs.values():
            labels = [arr[i]["label"] for i in idxs]
            if len(set(labels)) <= 1:
                continue
            arcLen = sum(dist(arr[idxs[t]]["pt"], arr[idxs[t + 1]]["pt"])
                         for t in range(len(idxs) - 1))
            # 直边（弧内切向累计转角小）必然单主，整弧归属；弯弧（平滑字体
            # 的连笔过渡可能真跨笔画）长且分歧成块时保留逐样本标签
            turn = 0.0
            for t in range(len(idxs) - 1):
                tA = arr[idxs[t]]["tan"]
                tB = arr[idxs[t + 1]]["tan"]
                turn += math.degrees(math.acos(max(-1.0, min(1.0,
                    tA[0] * tB[0] + tA[1] * tB[1]))))
            if turn > 40.0:
                domShare = max(labels.count(l) for l in set(labels)) / len(labels)
                if arcLen > 3.0 * wMedFinal and domShare < 0.75:
                    continue
            bestK, bestSc = labels[0], 1e18
            if c["isHole"]:
                # 孔洞边弧看"断面中心"归属：从弧上取点向墨侧探到对面边界，
                # 用通道中点对各笔打分——口的内缘断面中心是竖条中心（径向
                # 对应的正确场景），日的孔顶边断面中心正落在中横中轴线上
                # （径向对应会把它分给外框近邻、中横颗粒无收的场景）。
                # 探不到通道（宽交叠区）退回径向对应标签的多数票
                capH = max(2.0 * wMedFinal, 1.2 * w0, 60.0)
                probes = []
                for q in (len(idxs) // 4, len(idxs) // 2, 3 * len(idxs) // 4):
                    sm = arr[idxs[q]]
                    cp = corridorPoint(sm["pt"], sm["tan"], contours, capH,
                                       max(8.0, capH * 0.15))
                    if cp is not None:
                        probes.append((cp, sm["tan"]))
                if probes:
                    for k in contourAllowed[ci]:
                        sc = sum(scoreOf(cp, tn, k) for cp, tn in probes) \
                            / len(probes)
                        if sc < bestSc:
                            bestSc, bestK = sc, k
                else:
                    cnt = {}
                    for l in labels:
                        cnt[l] = cnt.get(l, 0) + 1
                    bestK = max(cnt, key=cnt.get)
            else:
                for k in contourAllowed[ci]:
                    sc = sum(scoreOf(arr[i]["pt"], arr[i]["tan"], k)
                             for i in idxs) / len(idxs)
                    if sc < bestSc:
                        bestSc, bestK = sc, k
            # 饿死保护：整弧改判会把某笔总样本压到极少时跳过（小点的孤立
            # 轮廓曾被整弧划给邻笔，正主颗粒无收）
            lost = {}
            for l in labels:
                if l != bestK:
                    lost[l] = lost.get(l, 0) + 1
            if any(arcTotals[l] - n_ < 6 for l, n_ in lost.items()):
                continue
            for i in idxs:
                if arr[i]["label"] != bestK:
                    arcTotals[arr[i]["label"]] -= 1
                    arcTotals[bestK] += 1
                    arr[i]["label"] = bestK

    for ci, c in enumerate(contours):
        if not c["isHole"]:
            consolidateContour(ci)

    # 孔洞边界标签径向对应（环形结构内外一致，继承已收敛的外缘标签）
    for ci, c in enumerate(contours):
        if not c["isHole"]:
            continue
        for sm in sampleSets[ci]:
            best, bestD = -1, 1e18
            for cj, c2 in enumerate(contours):
                if c2["isHole"] or c2["group"] != c["group"]:
                    continue
                for sm2 in sampleSets[cj]:
                    d = dist(sm["pt"], sm2["pt"])
                    if d < bestD:
                        bestD, best = d, sm2["label"]
            if best >= 0:
                sm["label"] = best
    arcTotals = [0] * nStrokes
    for arr in sampleSets:
        for sm in arr:
            arcTotals[sm["label"]] += 1
    for ci, c in enumerate(contours):
        if c["isHole"]:
            consolidateContour(ci)

    # ------------------------------------------------------------ 标签平滑
    for arr in sampleSets:
        if len(arr) < 4:
            continue
        for _ in range(3):
            n = len(arr)
            runs = []
            for i in range(n):
                if runs and arr[i]["label"] == runs[-1]["label"]:
                    runs[-1]["idx"].append(i)
                else:
                    runs.append({"label": arr[i]["label"], "idx": [i]})
            if len(runs) > 1 and runs[0]["label"] == runs[-1]["label"]:
                runs[0]["idx"] = runs[-1]["idx"] + runs[0]["idx"]
                runs.pop()
            if len(runs) <= 1:
                break
            changed = False
            for r in runs:
                runLen = sum(dist(arr[i]["pt"], arr[(i + 1) % n]["pt"])
                             for i in r["idx"])
                if len(r["idx"]) <= 2 or runLen < 15:
                    prevIdx = (r["idx"][0] - 1 + n) % n
                    for i in r["idx"]:
                        arr[i]["label"] = arr[prevIdx]["label"]
                    changed = True
            if not changed:
                break

    _tick("整体归属与平滑")

    # ------------------------------------------------------------ 矢量切割
    cutPoints = []
    strokeArcs = [[] for _ in range(nStrokes)]
    for ci, c in enumerate(contours):
        arr = sampleSets[ci]
        n = len(arr)
        segCount = len(c["segs"])
        if not n:
            continue
        if all(sm["label"] == arr[0]["label"] for sm in arr):
            strokeArcs[arr[0]["label"]].append(
                {"segs": list(c["segs"]), "closed": True, "contour": ci})
            continue

        def paramAt(s):
            si = min(segCount - 1, int(s))
            t = min(1.0, max(0.0, s - si))
            seg = c["segs"][si]
            return bezPoint(seg, t), bezTangent(seg, t)

        bounds = []
        for i in range(n):
            a, b = arr[i], arr[(i + 1) % n]
            if a["label"] == b["label"]:
                continue
            s0 = a["s"]
            s1 = b["s"] if b["s"] > a["s"] else b["s"] + segCount
            # 切点吸附角点：两样本之间恰有轮廓角点时直接在角点切
            sCut = None
            for cj in sorted(cornerSets[ci]):
                cjj = cj if cj > s0 else cj + segCount
                if s0 < cjj <= s1 + 1e-9:
                    sCut = cjj % segCount
                    break
            if sCut is None:
                for _ in range(22):
                    mid = (s0 + s1) / 2
                    pt, tan = paramAt(mid % segCount)
                    if labelOf(pt, tan, contourAllowed[ci]) == a["label"]:
                        s0 = mid
                    else:
                        s1 = mid
                sCut = ((s0 + s1) / 2) % segCount
            bounds.append({"s": sCut, "to": b["label"]})
            pt, _tan = paramAt(sCut)
            cutPoints.append({"pt": pt, "from": a["label"], "to": b["label"]})
        bounds.sort(key=lambda x: x["s"])
        for bi in range(len(bounds)):
            b0 = bounds[bi]
            b1 = bounds[(bi + 1) % len(bounds)]
            label = b0["to"]
            sStart = b0["s"]
            sEnd = b1["s"] if b1["s"] > b0["s"] else b1["s"] + segCount
            segs = []
            s = sStart
            while s < sEnd - 1e-6:
                si = int(s) % segCount
                t0 = s - int(s)
                segEndS = int(s) + 1
                piece = bezSlice(c["segs"][si], t0, min(1.0, sEnd - int(s)))
                if segLength(piece) > 0.8:
                    segs.append(piece)
                s = min(segEndS, sEnd)
            if segs:
                strokeArcs[label].append({"segs": segs, "closed": False, "contour": ci})

    _tick("矢量切割")

    # ------------------------------------------------------------ 划分式重构
    strokes = []
    for k in range(nStrokes):
        arcs = strokeArcs[k]
        loops = []
        for a in arcs:
            if a["closed"]:
                loops.append({"segs": a["segs"], "bridges": 0,
                              "retained": sum(segLength(x) for x in a["segs"]),
                              "bridgeL": 0.0})
        openArcs = [a for a in arcs if not a["closed"]]
        byContour = {}
        for a in openArcs:
            byContour.setdefault(a["contour"], []).append(a)

        def bridge(prevSeg, nextSeg):
            pA, pB = prevSeg[4], nextSeg[1]
            chord = dist(pA, pB)
            if chord < 1.2:
                return None
            # 笔锋修复：直割线弦切口有"刀削"感，改用三次贝塞尔——两端切线
            # 延长交于 CP，以两个中点为控制点（大力智能/MMH 的桥修复法）。
            # 切线近平行、交点在反方向或过远时退回直线；越界由布尔收口兜底
            if chord >= 14.0:
                tA = bezTangent(prevSeg, 1.0)
                tB = bezTangent(nextSeg, 0.0)
                det = tA[0] * (-tB[1]) - (-tB[0]) * tA[1]
                if abs(det) > 1e-9:
                    rx, ry = pB[0] - pA[0], pB[1] - pA[1]
                    s = (rx * (-tB[1]) - (-tB[0]) * ry) / det
                    u = (tA[0] * ry - tA[1] * rx) / det
                    if s > 0 and u < 0:
                        cp = (pA[0] + tA[0] * s, pA[1] + tA[1] * s)
                        if dist(pA, cp) <= 1.2 * chord and \
                           dist(pB, cp) <= 1.2 * chord:
                            return cubicSeg(
                                pA,
                                ((pA[0] + cp[0]) / 2, (pA[1] + cp[1]) / 2),
                                ((pB[0] + cp[0]) / 2, (pB[1] + cp[1]) / 2),
                                pB)
            return lineSeg(pA, pB)

        chains = []
        for _, chainArcs in byContour.items():
            segs = []
            bridges = 0
            retained = 0.0
            bridgeL = 0.0
            for idx, a in enumerate(chainArcs):
                segs.extend(a["segs"])
                retained += sum(segLength(x) for x in a["segs"])
                if idx < len(chainArcs) - 1:
                    gap = bridge(a["segs"][-1], chainArcs[idx + 1]["segs"][0])
                    if gap:
                        segs.append(gap)
                        bridges += 1
                        bridgeL += segLength(gap)
            chains.append({"segs": segs, "bridges": bridges,
                           "retained": retained, "bridgeL": bridgeL})
        joinLimit = max(widths[k] * 2.5, 160.0)
        while chains:
            cur = chains.pop(0)
            while chains:
                end = cur["segs"][-1][4]
                bestI, bestD = -1, 1e18
                for i, chn in enumerate(chains):
                    d = dist(end, chn["segs"][0][1])
                    if d < bestD:
                        bestD, bestI = d, i
                if bestD > joinLimit:
                    break
                nxt = chains.pop(bestI)
                gap = bridge(cur["segs"][-1], nxt["segs"][0])
                if gap:
                    cur["segs"].append(gap)
                    cur["bridges"] += 1
                    cur["bridgeL"] += segLength(gap)
                cur["segs"].extend(nxt["segs"])
                cur["bridges"] += nxt["bridges"]
                cur["retained"] += nxt["retained"]
                cur["bridgeL"] += nxt["bridgeL"]
            wrap = bridge(cur["segs"][-1], cur["segs"][0])
            if wrap:
                cur["segs"].append(wrap)
                cur["bridges"] += 1
                cur["bridgeL"] += segLength(wrap)
            loops.append(cur)

        # 碎片环剪除：误标样本形成的junk环（保留弧长远小于主环）丢弃，
        # 其面积由布尔收口的残差回填归还给真正的主人
        if len(loops) > 1:
            maxR = max(lp["retained"] for lp in loops)
            kept = [lp for lp in loops
                    if lp["retained"] >= max(60.0, 0.45 * maxR)]
            if kept:
                loops = kept
        retainedLen = sum(lp["retained"] for lp in loops)
        bridgeLen = sum(lp["bridgeL"] for lp in loops)

        pathD = " ".join(contourToPath(lp["segs"]) for lp in loops)
        strokes.append({
            "index": k, "type": kai["strokeTypes"][k], "path": pathD,
            "loops": len(loops),
            "bridges": sum(lp["bridges"] for lp in loops),
            "retainRatio": retainedLen / ((retainedLen + bridgeLen) or 1.0),
            "median": [[round(p[0], 1), round(p[1], 1)] for p in medians[k]],
            "width": widths[k],
            "template": templateSources[k],
            "group": strokeGroup[k],
            "templatePath": templatePaths[k],
            "failed": not pathD,
        })

    _tick("重构")

    # ------------------------------------------------------------ 布尔收口 + 校验
    # 饿死救济先行：切割中颗粒无收/零宽退化环的笔（宾的宀左点曾只得
    # 一段边界线），用骨架走廊∩本组区域补一个实体，再进收口。
    # 饿死救济：走廊在 rescueStarved 内用组局部仿射从楷体中轴线构造
    # （全局仿射/精调种子都会歪，见函数注释）
    _glyph = booleanClamp.glyphRegion(contours)  # 只算一次，收口各环节复用
    # 空洞识别（并集后内环=真实围合空腔；面积>400 滤掉笔画间缝隙噪声）
    _holeBoxes = []
    try:
        _geoms = list(_glyph.geoms) if hasattr(_glyph, "geoms") else [_glyph]
        for _pg in _geoms:
            for _ring in _pg.interiors:
                _xs = [p[0] for p in _ring.coords]
                _ys = [p[1] for p in _ring.coords]
                if (max(_xs) - min(_xs)) * (max(_ys) - min(_ys)) > 400.0:
                    _holeBoxes.append([round(min(_xs)), round(min(_ys)),
                                       round(max(_xs)), round(max(_ys))])
    except Exception:
        pass
    _holeCount = len(_holeBoxes)
    booleanClamp.rescueStarved(contours, strokes, kai["strokes"],
                               kai["medians"], glyph=_glyph)
    unionCheck = None
    if applyBooleanClamp:
        unionCheck = booleanClamp.clampStrokes(contours, strokes, glyph=_glyph)
        # 裁剪可能把"整笔落在墨外"的退化笔置 failed——重走饿死救济
        # （救济走廊∩本组区域必在字形内，不会引入新溢出）后再收口一次
        if any(s["failed"] for s in strokes):
            booleanClamp.rescueStarved(contours, strokes,
                                       kai["strokes"], kai["medians"],
                                       glyph=_glyph)
            unionCheck = booleanClamp.clampStrokes(contours, strokes,
                                                   glyph=_glyph)
        # 楷体不相交笔对的重叠减除：融合日/目/口的封底横被外框整体
        # 包含（亨/亭/勤 100% 重叠）——内横保带、外框让位；减除产生
        # 的孤儿片由随后的 enforceConnectivity 按共享边界归还邻笔
        booleanClamp.resolveKaiDisjointOverlaps(strokes, kai["medians"])
        # 单笔单连通终态收口：回填/减除偶发的断笔在此修复
        booleanClamp.enforceConnectivity(contours, strokes)
        # 终态校验统一按实际路径口径（clampStrokes 返回值基于裁剪区域，
        # 会掩盖未被替换路径的残余溢出）
        unionCheck = booleanClamp.reUnionCheck(contours, strokes, glyph=_glyph)
        # 终态补缝：0.5% 容差内的可见缺口（爱冖区 0.4% 白缝）无条件
        # 回填——此时救济/减除/连通性已尘埃落定，不会乱粘
        if unionCheck.get("cover", 100) < 99.95:
            if booleanClamp.fillResidualGaps(contours, strokes, glyph=_glyph):
                unionCheck = booleanClamp.reUnionCheck(contours, strokes,
                                                       glyph=_glyph)
        # 终态强制收口：连通性搬运/退化环奇偶翻转可能在收口后重引入溢出
        # （流江水曾终态溢出 19%）——超标就再收口一轮，硬保证优先
        if abs(unionCheck.get("excess", 0)) > 0.5 or            unionCheck.get("cover", 100) < 99.5:
            booleanClamp.clampStrokes(contours, strokes, glyph=_glyph)
            if any(s["failed"] for s in strokes):
                # 扫尾裁剪可能新置 failed（整笔在墨外）——再救济一轮，
                # 救济体必在字形内，不会破坏刚修好的溢出
                booleanClamp.rescueStarved(contours, strokes, kai["strokes"],
                                           kai["medians"], glyph=_glyph)
                booleanClamp.clampStrokes(contours, strokes, glyph=_glyph)
            unionCheck = booleanClamp.reUnionCheck(contours, strokes,
                                                   glyph=_glyph)
    if unionCheck is None:
        unionCheck = booleanClamp.reUnionCheck(contours, strokes, glyph=_glyph)

    _tick("收口")

    # 终态中轴线重提：median 若残留折返（交叉区断面居中的伪影），用
    # 切割定稿后的单笔多边形重提干净中轴——此时"笔画本身的中轴线"
    # 才是良定义的。走 B 骨架同款 Voronoi 直径路径+法向矫正+吸直
    # （平滑改良老 median 会被蛇形伪拐角的拐角保护锁死）。只影响
    # median 陈述与回灌种子，不回改切割。
    for s in strokes:
        if s["failed"] or not s["path"]:
            continue
        sb0 = _switchbackCount(s["median"])
        if sb0 == 0:
            continue
        loops = [flattenSegs(c["segs"], 6) for c in parseContours(s["path"])]
        loops = [lp for lp in loops if len(lp) >= 4]
        rm = outlineCenterline(loops) if loops else None
        if rm and len(rm) >= 2:
            old = s["median"]
            if dist(tuple(rm[0]), tuple(old[0])) + \
               dist(tuple(rm[-1]), tuple(old[-1])) > \
               dist(tuple(rm[-1]), tuple(old[0])) + \
               dist(tuple(rm[0]), tuple(old[-1])):
                rm = rm[::-1]
            rm = resamplePolyline([tuple(p) for p in rm], 15)
            rm = midpointRectify(rm, loops)
            rm = straightenSections(rm)
        else:
            rm = _reMedianFromStroke([tuple(p) for p in s["median"]],
                                     s["path"], s["width"])
        if rm and len(rm) >= 2 and _switchbackCount(rm) < sb0:
            s["median"] = [[round(p[0], 1), round(p[1], 1)] for p in rm]

    # 形状匹配（D′ vs 楷体同笔，尺度不变）
    for s in strokes:
        if s["failed"]:
            s["shapeSim"] = 0
        else:
            s["shapeSim"] = shapeSimilarity(
                shapeDescriptor([s["path"]]),
                shapeDescriptor([kai["strokes"][s["index"]]]))

    result = {
        "ch": ch, "font": fontEntry.key,
        # 空洞字标记（拆字时顺带识别，供后期在空洞内叠加独立元素）。
        # 注意不能用轮廓 isHole：鸿蒙等字体的框是两个 L 形件搭接而成，
        # 轮廓层面无孔，"孔"是布尔并集后才涌现的——取 _glyph 的内环。
        "holeCount": _holeCount,
        "holes": _holeBoxes,
        "contours": [{"path": contourToPath(c["segs"]), "isHole": c["isHole"],
                      "group": c["group"], "segCount": len(c["segs"]),
                      "ccw": c["area"] >= 0} for c in contours],
        "samples": [[[round(sm["pt"][0], 1), round(sm["pt"][1], 1), sm["label"]]
                     for sm in arr] for arr in sampleSets],
        "cutPoints": [[round(cp["pt"][0], 1), round(cp["pt"][1], 1)]
                      for cp in cutPoints],
        "strokes": strokes,
        "groups": [{"id": g,
                    "strokes": [k for k in range(nStrokes) if strokeGroup[k] == g],
                    "isolated": sum(1 for k in range(nStrokes)
                                    if strokeGroup[k] == g) == 1}
                   for g in range(nGroups)],
        "groupRemap": groupRemapInfo,
        "semanticClaims": semanticClaims,
        "slotSwaps": slotSwaps,
        "ladderProbe": ladderProbe if LADDER_PROBE else [],
        "ladderRealign": ladderRealign,
        "slotOf": [(kaiMatches0[k][0] if k < len(kaiMatches0) and kaiMatches0[k]
                    else None) for k in range(nStrokes)],
        "unionCheck": unionCheck,
        "kai": {"strokes": kai["strokes"], "medians": kai["medians"],
                "strokeTypes": kai["strokeTypes"],
                "verifyTypes": kai.get("verifyTypes", kai["strokeTypes"]), "radical": kai["radical"],
                "decomposition": kai["decomposition"], "matches": kai["matches"],
                "structure": kai["structure"], "chaiziJt": kai["chaiziJt"],
                "chaiziFt": kai["chaiziFt"],
                "components": kai.get("components", []),
                "etymology": kai.get("etymology"),
                "pinyin": kai.get("pinyin", []),
                "definition": kai.get("definition", "")},
        "pass": 1 if seedMedians is None else 2,
        "timings": _timings,
    }
    # ------------------------------------------------------------ 自洽回灌
    # 第一遍拆完后，用每笔自身几何重提干净中轴作种子重跑一遍匹配；
    # 双指标（失败笔数、retain+shapeSim）择优采用，防止吞并式虚高
    _noFail = not any(s["failed"] for s in strokes)
    if selfConsistent and seedMedians is None and             not (_noFail and _meanOf(result, "retainRatio") >= 0.995):
        seeds = _selfSeeds(result)
        if seeds:
            r2 = runPipeline(dataHub, fontEntry, ch, applyBooleanClamp,
                             seedMedians=seeds, selfConsistent=False)
            expCenters = [affine(((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2))
                          for bb in kaiStrokeBBoxes]
            diag = math.hypot(tb.w, tb.h)
            if "error" not in r2 and _secondPassBetter(r2, result,
                                                      expCenters, diag):
                r2["timings"] = r2.get("timings", []) + [
                    ["第一遍(被自洽二遍替换)",
                     sum(t[1] for t in _timings)]]
                # 二遍走 seeds 路径不再触发执行器——诊断从一遍继承
                # （照 timings 合并先例，验收翻转清单依赖）
                if not r2.get("ladderRealign"):
                    r2["ladderRealign"] = result.get("ladderRealign") or []
                result = r2
            else:
                result["timings"].append(
                    ["自洽二遍(未采纳)",
                     sum(t[1] for t in (r2.get("timings") or []))])

    # ------------------------------------------------------------ 轴向守卫
    # 名义横/竖的切割结果主轴偏差过大（verify TYPE 的第一大失败源）
    # ⇒ 该笔 D 被模板错配/精调带歪。用楷体中轴（位置由结构 C 保证）
    # 作病笔种子、健康笔保留精调中轴，重跑一遍；采纳须病笔数下降且
    # 失败笔/retain/sim/并集全不倒退。只在病字上花第二遍的钱。
    if seedMedians is None:
        _f1 = _axisFails(result)
        if _f1:
            _bad = {k for k, _ in _f1}
            _seeds3 = []
            _ladderK = {m[0] for m in (result.get("ladderRealign") or [])}
            for s in result["strokes"]:
                # 梯队执行器纠正过的笔不回退楷体名义位——名义位正是
                # 刚被纠正掉的错档位置（评审场景④）
                if s["index"] in _bad and s["index"] not in _ladderK or \
                        not s.get("median"):
                    _seeds3.append([affine(tuple(p))
                                    for p in kai["medians"][s["index"]]])
                else:
                    _seeds3.append([tuple(p) for p in s["median"]])
            r3 = runPipeline(dataHub, fontEntry, ch, applyBooleanClamp,
                             seedMedians=_seeds3, selfConsistent=False)
            if "error" not in r3:
                _f3 = _axisFails(r3)
                _u1 = result["unionCheck"] or {}
                _u3 = r3["unionCheck"] or {}
                if (len(_f3) < len(_f1)
                        and sum(1 for s in r3["strokes"] if s["failed"]) <=
                        sum(1 for s in result["strokes"] if s["failed"])
                        and _meanOf(r3, "retainRatio") >=
                        _meanOf(result, "retainRatio") - 0.02
                        and _meanOf(r3, "shapeSim") >=
                        _meanOf(result, "shapeSim") - 0.03
                        and _u3.get("cover", 0) >= _u1.get("cover", 100) - 0.1
                        and abs(_u3.get("excess", 0)) <=
                        abs(_u1.get("excess", 0)) + 0.1):
                    r3["timings"] = (r3.get("timings") or []) + [
                        ["轴向守卫重试(已采纳)",
                         sum(t[1] for t in (result.get("timings") or []))]]
                    if not r3.get("ladderRealign"):
                        r3["ladderRealign"] = \
                            result.get("ladderRealign") or []
                    result = r3
    return result

