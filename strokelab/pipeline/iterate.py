# -*- coding: utf-8 -*-
"""strokelab.pipeline.iterate — contourAllowed 冻结 + 采样归属迭代精调
+ 终态吸直 + S2 模板可视化。

按冻结的组指派做 ITERS 轮"边界采样点归属 → 笔宽估计 → refine/断面
居中"迭代（numpy 批量化，与标量版逐位一致）；饿死自救退回楷体中轴；
终态统一吸直。scoreOf/labelOf 闭包挂 ctx 供 cutting 的边弧归属/矢量
切割复用。ITERS 经 sys.modules 读包属性当前值。事故史注释随代码保留。
"""

import math
import sys

from ..geometry import (bboxOfPoints, bezPoint, bezTangent, contourToPath,
                        nearestBatch, nearestOnPolyline, parseContours,
                        polylineLength, recenterMedian, refineMedianFit,
                        segLength, straightenSections)


def _pkg():
    """包模块本体：运行期读 ITERS 的当前值（import 时不固化）。"""
    return sys.modules["strokelab.pipeline"]


def run(ctx):
    """产出 ctx.samples.contourAllowed/w0/widths/sampleSets/scoreOf/labelOf/
    templatePaths；medians 精调至 D' 终态。"""
    _pl = _pkg()
    kai = ctx.kaiRef.kai
    contours = ctx.geom.contours
    nStrokes = ctx.pose.nStrokes
    medians = ctx.pose.medians
    initMedians = ctx.pose.initMedians
    groupStrokes = ctx.groups.groupStrokes
    templateEnts = ctx.pose.templateEnts
    affine = ctx.kaiRef.affine
    contourAllowed = [groupStrokes.get(c["group"], list(range(nStrokes)))
                      for c in contours]

    ctx.diag.tick("连通组分治")

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
    for it in range(_pl.ITERS):
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
        if it >= _pl.ITERS - 1:
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

    ctx.diag.tick("归属迭代精调")

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

    ctx.samples.contourAllowed = contourAllowed
    ctx.pose.w0 = w0
    ctx.pose.widths = widths
    ctx.samples.sampleSets = sampleSets
    ctx.samples.scoreOf = scoreOf
    ctx.samples.labelOf = labelOf
    ctx.pose.templatePaths = templatePaths
