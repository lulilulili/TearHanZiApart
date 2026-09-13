# -*- coding: utf-8 -*-
"""strokelab.pipeline.iterate — contourAllowed 冻结 + 采样归属迭代精调
+ 终态吸直 + S2 模板可视化。

按冻结的组指派做 ITERS 轮"边界采样点归属 → 笔宽估计 → refine/断面
居中"迭代（numpy 批量化，与标量版逐位一致）；饿死自救退回楷体中轴；
终态统一吸直。scoreSample/labelSample 为模块级函数，samples.scoreOf/
labelOf 以 functools.partial 绑定 pose 视图后供 cutting 的边弧归属/
矢量切割复用（读 pose.scoreMedians/scoreWidths 终态）。ITERS 经
sys.modules 读包属性当前值。事故史注释随代码保留。
"""

import functools
import math
import sys

from ..geometry import (bboxOfPoints, bezPoint, bezTangent, contourToPath,
                        nearestBatch, nearestOnPolyline, parseContours,
                        polylineLength, recenterMedian, refineMedianFit,
                        segLength, straightenSections)


def _pkg():
    """包模块本体：运行期读 ITERS 的当前值（import 时不固化）。"""
    return sys.modules["strokelab.pipeline"]


def extendMedian(m, ext):
    """中轴两端沿端点切向各延长 ext（评分走廊覆盖笔锋出头）。"""
    if len(m) < 2 or ext <= 0:
        return m
    t0x, t0y = m[0][0] - m[1][0], m[0][1] - m[1][1]
    L0 = math.hypot(t0x, t0y) or 1.0
    tnx, tny = m[-1][0] - m[-2][0], m[-1][1] - m[-2][1]
    Ln = math.hypot(tnx, tny) or 1.0
    return [(m[0][0] + t0x / L0 * ext, m[0][1] + t0y / L0 * ext)] + list(m) + \
           [(m[-1][0] + tnx / Ln * ext, m[-1][1] + tny / Ln * ext)]


def scoreSample(pose, pt, tan, k):
    """样本×笔归属得分（原 scoreOf 闭包本体；samples.scoreOf 以 partial
    绑定 pose 后保持 (pt, tan, k) 签名）。"""
    near = nearestOnPolyline(pt, pose.scoreMedians[k])
    halfW = pose.scoreWidths[k] * 0.5 + 6
    dirPen = 1 - abs(tan[0] * near["tan"][0] + tan[1] * near["tan"][1])
    return near["d"] / halfW + 0.5 * dirPen


def labelSample(pose, pt, tan, allowed=None):
    """样本→最优笔标签（原 labelOf 闭包本体；samples.labelOf 以 partial
    绑定 pose 后保持 (pt, tan, allowed) 签名）。"""
    cand = allowed if allowed is not None else range(pose.nStrokes)
    best, bestScore = next(iter(cand)), 1e18
    for k in cand:
        sc = scoreSample(pose, pt, tan, k)
        if sc < bestScore:
            bestScore, best = sc, k
    return best


def freezeAllowed(geom, groups, pose):
    """contourAllowed 冻结：轮廓→允许竞争的笔集合（连通组分治产物）。"""
    return [groups.groupStrokes.get(c["group"], list(range(pose.nStrokes)))
            for c in geom.contours]


def refineLoop(geom, kaiRef, pose, samples):
    """ITERS 轮采样归属迭代精调 + 终态吸直：产出 samples.sampleSets/
    scoreOf/labelOf + pose.w0/widths/scoreMedians/scoreWidths；
    pose.medians 精调至 D' 终态。"""
    _pl = _pkg()
    kai = kaiRef.kai
    contours = geom.contours
    nStrokes = pose.nStrokes
    medians = pose.medians
    initMedians = pose.initMedians
    affine = kaiRef.affine
    contourAllowed = samples.contourAllowed

    # ------------------------------------------------------------ 迭代归属+精调
    glyphArea = sum((-abs(c["area"]) if c["isHole"] else abs(c["area"]))
                    for c in contours)
    totalMedianLen = sum(polylineLength(m) for m in medians) or 1.0
    w0 = max(14.0, min(180.0, abs(glyphArea) / totalMedianLen))
    widths = [w0] * nStrokes
    scoreWidths = list(widths)

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

    usedKaiFallback = [False] * nStrokes
    # 批量评分预备：样本点/切向在迭代间不变，一次性排成数组
    import numpy as _np
    _flatSamples = [sm for arr in sampleSets for sm in arr]
    _flatCi = [ci for ci, arr in enumerate(sampleSets) for _ in arr]
    _ptsArr = _np.array([sm["pt"] for sm in _flatSamples], dtype=_np.float64) \
        if _flatSamples else _np.zeros((0, 2))
    _tanArr = _np.array([sm["tan"] for sm in _flatSamples], dtype=_np.float64) \
        if _flatSamples else _np.zeros((0, 2))
    _ciArr = _np.array(_flatCi, dtype=_np.int64)
    for it in range(_pl.ITERS):
        assigned = [[] for _ in range(nStrokes)]
        # 逐样本评分批量化（语义与 labelSample 等价：分数矩阵按 allowed
        # 顺序 argmin，并列取先者；nearestBatch 与标量版逐位一致）
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

    pose.w0 = w0
    pose.widths = widths
    pose.scoreMedians = scoreMedians
    pose.scoreWidths = scoreWidths
    samples.sampleSets = sampleSets
    samples.scoreOf = functools.partial(scoreSample, pose)
    samples.labelOf = functools.partial(labelSample, pose)


def _mapTemplatePoint(dst, src, p):
    """B 模板轮廓点→精调后 D 位置框的轴对齐仿射（原 mv 闭包本体）。
    dst=(fx0, fy0, fw, fh)，src=(ebx0, eby0, ew, ehh)。"""
    return (dst[0] + (p[0] - src[0]) * dst[2] / src[2],
            dst[1] + (p[1] - src[1]) * dst[3] / src[3])


def buildTemplatePaths(pose):
    """S2 模板可视化：把 B 模板画在精调后的 D 位置（精调后中轴线包围盒
    + 半笔宽），与匹配实际使用的几何一致，避免初始放置的视觉重叠误导。"""
    templatePaths = []
    for k in range(pose.nStrokes):
        ent = pose.templateEnts[k]
        if not ent:
            templatePaths.append("")
            continue
        mb = bboxOfPoints(pose.medians[k])
        half = pose.widths[k] * 0.55
        eb = ent["outlineBBox"]
        dst = (mb.x0 - half, mb.y0 - half, mb.w + 2 * half, mb.h + 2 * half)
        src = (eb[0], eb[1], max(1.0, eb[2] - eb[0]), max(1.0, eb[3] - eb[1]))

        parts = []
        for d in ent["contours"]:
            for c in parseContours(d):
                moved = [(sg[0],
                          _mapTemplatePoint(dst, src, sg[1]),
                          _mapTemplatePoint(dst, src, sg[2]),
                          _mapTemplatePoint(dst, src, sg[3]),
                          _mapTemplatePoint(dst, src, sg[4]))
                         for sg in c["segs"]]
                parts.append(contourToPath(moved))
        templatePaths.append(" ".join(parts))

    pose.templatePaths = templatePaths
