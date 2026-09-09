# -*- coding: utf-8 -*-
"""strokelab.pipeline — 拆解管线：D 构建 → 归属 → 精调 → 矢量切割 → 划分式重构。

流程（v5 架构）：
  D = B库同类型笔画骨架（A↔B 映射产物，目标字体自己的形态）按楷体结构 C 定位；
  D 与目标字形实际路径匹配 → 归属/迭代精调 → 标签跳变处 De Casteljau 精确切割 →
  环路追踪 + 直割线弦 + 跨轮廓并环重构 → 布尔收口（boolean.clampStrokes）保证
  所有笔画并集与原字形恒等（不多不少，交叠区双重归属）。
"""

import math

from .geometry import (dist, lineSeg, cubicSeg, parseContours, contourToPath,
                       flattenSegs,
                       bboxOfPoints, pointInPolygon, nearestOnPolyline,
                       polylineLength, resamplePolyline, analyzeContours,
                       bezPoint, bezTangent, bezSlice, segLength,
                       shapeDescriptor, shapeSimilarity, refineMedianFit,
                       recenterMedian, corridorPoint)
from .classify import findLibEntry
from . import boolean as booleanClamp

ITERS = 5


def _reMedianFromStroke(median, strokePath, width):
    """自洽回灌的种子提取：用笔画自身几何重提中轴——轻平滑（拐角除外）+
    对本笔轮廓断面居中，迭代两轮。B 骨架抖动、只按己方样本拟合造成的
    贴边/锯齿都会被实际笔画区域的断面中点洗掉。"""
    cons = []
    for c in parseContours(strokePath):
        poly = flattenSegs(c["segs"], 10)
        if len(poly) >= 3:
            cons.append({"poly": poly, "isHole": False})
    m = [tuple(p) for p in median]
    if not cons or len(m) < 2:
        return None
    cap = max(1.6 * width, 40.0)
    cos35 = math.cos(math.radians(35))
    for _ in range(2):
        if len(m) >= 3:
            sm = [m[0]]
            for i in range(1, len(m) - 1):
                v1 = (m[i][0] - m[i - 1][0], m[i][1] - m[i - 1][1])
                v2 = (m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1])
                l1, l2 = math.hypot(*v1), math.hypot(*v2)
                if l1 > 1e-6 and l2 > 1e-6 and \
                   (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) < cos35:
                    sm.append(m[i])
                else:
                    sm.append((0.25 * m[i - 1][0] + 0.5 * m[i][0] + 0.25 * m[i + 1][0],
                               0.25 * m[i - 1][1] + 0.5 * m[i][1] + 0.25 * m[i + 1][1]))
            sm.append(m[-1])
            m = sm
        m = recenterMedian(m, cons, cap)
    return m


def _selfSeeds(result):
    seeds = []
    changed = False
    for s in result["strokes"]:
        rm = None
        if not s["failed"] and s["path"]:
            rm = _reMedianFromStroke(s["median"], s["path"], s["width"])
        if rm is None:
            seeds.append(s["median"])
        else:
            seeds.append(rm)
            changed = True
    return seeds if changed else None


def _meanOf(result, key):
    ss = result["strokes"]
    return sum(s[key] for s in ss) / (len(ss) or 1)


def _strokeCenter(path):
    pts = []
    for c in parseContours(path):
        pts.extend(flattenSegs(c["segs"], 40))
    if not pts:
        return None
    b = bboxOfPoints(pts)
    return ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)


def _secondPassBetter(r2, r1, expCenters, diag):
    f1 = sum(1 for s in r1["strokes"] if s["failed"])
    f2 = sum(1 for s in r2["strokes"] if s["failed"])
    # 结构位置守卫：每笔质心对楷体映射位置的偏差不得比第一遍显著恶化——
    # shapeSim 尺度不变、看不见"整笔挪去别人地盘"（爱的竖曾被换到左下角
    # 反而 sim 升高）
    for a, b, exp in zip(r1["strokes"], r2["strokes"], expCenters):
        if a["failed"] or b["failed"] or exp is None:
            continue
        ca, cb = _strokeCenter(a["path"]), _strokeCenter(b["path"])
        if ca is None or cb is None:
            continue
        if dist(cb, exp) > dist(ca, exp) + 0.04 * diag:
            return False
    if f2 != f1:
        return f2 < f1
    # retain 升但 sim 明显降可能是"整块吞并"式虚高，双指标把关
    return (_meanOf(r2, "retainRatio") > _meanOf(r1, "retainRatio") + 0.005
            and _meanOf(r2, "shapeSim") >= _meanOf(r1, "shapeSim") - 1.0)


def runPipeline(dataHub, fontEntry, ch, applyBooleanClamp=True,
                seedMedians=None, selfConsistent=True):
    kai = dataHub.kai(ch)
    if not kai:
        return {"error": "MakeMeAHanzi 中没有「%s」的笔画数据" % ch}
    raw = fontEntry.glyphContours(ch)
    if not raw:
        return {"error": "字体 %s 中没有「%s」字形" % (fontEntry.key, ch)}

    contours = [{"segs": c["segs"]} for c in raw]
    analyzeContours(contours)

    # ------------------------------------------------------------ 全局对齐
    kaiPts = []
    kaiPerims = []
    kaiStrokeBBoxes = []
    for sp in kai["strokes"]:
        strokePts = []
        perim = 0.0
        for c in parseContours(sp):
            fl = flattenSegs(c["segs"], 25)
            strokePts.extend(fl)
            perim += polylineLength(fl)
        kaiPts.extend(strokePts)
        kaiPerims.append(perim)
        kaiStrokeBBoxes.append(bboxOfPoints(strokePts))
    kb = bboxOfPoints(kaiPts)
    allPts = []
    for c in contours:
        allPts.extend(c["poly"])
    tb = bboxOfPoints(allPts)
    sx, sy = tb.w / kb.w, tb.h / kb.h

    def affine(p):
        return (tb.x0 + (p[0] - kb.x0) * sx, tb.y0 + (p[1] - kb.y0) * sy)

    # ------------------------------------------------------------ D 构建
    # 每笔初始模板中轴线 = B库同类型骨架按楷体该笔包围盒定位；缺类型退回楷体中轴线
    medians = []
    templateSources = []
    templateEnts = []
    for k, m in enumerate(kai["medians"]):
        t = kai["strokeTypes"][k]
        ent = findLibEntry(fontEntry.libraryB, t) if fontEntry.libraryB else None
        if not ent and fontEntry.libraryB and t.endswith("钩"):
            ent = findLibEntry(fontEntry.libraryB, t[:-1])
        placed = None
        if ent:
            try:
                skel = fontEntry.ensureSkeleton(ent, dataHub)
                eb = ent["outlineBBox"]
                kb2 = kaiStrokeBBoxes[k]
                c0 = affine((kb2.x0, kb2.y0))
                c1 = affine((kb2.x1, kb2.y1))
                tw = max(1.0, c1[0] - c0[0])
                th = max(1.0, c1[1] - c0[1])
                ew = max(1.0, eb[2] - eb[0])
                ehh = max(1.0, eb[3] - eb[1])
                placed = [(c0[0] + (p[0] - eb[0]) * tw / ew,
                           c0[1] + (p[1] - eb[1]) * th / ehh) for p in skel]
                templateSources.append(ent["source"])
                templateEnts.append(ent)
            except Exception:
                placed = None
        if placed is None:
            placed = [affine(p) for p in m]
            templateSources.append("楷体中轴线(B库缺类型)")
            templateEnts.append(None)
        medians.append(placed)
    if seedMedians is not None:
        # 自洽回灌：第一遍逐笔干净中轴替换 B 库定位的 D（结构先验已由
        # 第一遍消化进种子里）
        medians = [[tuple(p) for p in m] for m in seedMedians]
        templateSources = ["自洽回灌"] * len(medians)
        templateEnts = [None] * len(medians)
    initMedians = [[tuple(p) for p in m] for m in medians]
    nStrokes = len(medians)

    # ------------------------------------------------------------ 连通组分治
    # 公理（用户校验①②）：正常字体设计中，同一笔画不会断成两个孤立连通组。
    # 按初始设计位置把每笔指定到唯一连通组，归属评分只允许本组笔画竞争本组
    # 轮廓 —— 跨组污染结构上不可能；组数=笔画数时每组恰一笔，自动退化为
    # "整组直出"（保留率100%、零切割）；部分孤立时自动分组分治。
    nGroups = max((c["group"] for c in contours), default=-1) + 1
    groupOuters = {g: [c["poly"] for c in contours
                       if c["group"] == g and not c["isHole"]]
                   for g in range(nGroups)}
    groupHoles = {g: [c["poly"] for c in contours
                      if c["group"] == g and c["isHole"]]
                  for g in range(nGroups)}
    groupCentroids = {}
    for g, polys in groupOuters.items():
        pts = [p for poly in polys for p in poly]
        if pts:
            groupCentroids[g] = (sum(p[0] for p in pts) / len(pts),
                                 sum(p[1] for p in pts) / len(pts))

    def strokeGroupOf(k):
        """笔画→组：中轴线各点到组的"墨距离"（墨内=0，否则到组内任一轮廓
        边界的最近距离）取均值，argmin。inside 占比法对 ⊓ 形带状轮廓失效
        （鸿蒙"日"的竖中轴悬在空腔里），距离法对带状/实心都稳。"""
        rm = resamplePolyline([tuple(p) for p in initMedians[k]], 20)
        best, bestAvg = 0, 1e18
        for g in range(nGroups):
            outers = groupOuters[g]
            holes = groupHoles[g]
            polys = outers + holes
            if not polys:
                continue
            total = 0.0
            for p in rm:
                if any(pointInPolygon(p, poly) for poly in outers) and \
                   not any(pointInPolygon(p, hp) for hp in holes):
                    d = 0.0
                else:
                    d = min(nearestOnPolyline(p, poly)["d"] for poly in polys)
                total += d
            avg = total / (len(rm) or 1)
            if avg < bestAvg:
                bestAvg, best = avg, g
        return best

    strokeGroup = [strokeGroupOf(k) for k in range(nStrokes)]
    groupStrokes = {g: [k for k in range(nStrokes) if strokeGroup[k] == g]
                    for g in range(nGroups)}
    for g in range(nGroups):
        if not groupStrokes[g]:  # 无笔画映射到该组（异常兜底）：放开限制
            groupStrokes[g] = list(range(nStrokes))
    contourAllowed = [groupStrokes.get(c["group"], list(range(nStrokes)))
                      for c in contours]

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
    for it in range(ITERS):
        assigned = [[] for _ in range(nStrokes)]
        for ci, arr in enumerate(sampleSets):
            for sm in arr:
                sm["label"] = labelOf(sm["pt"], sm["tan"], contourAllowed[ci])
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
            ds = sorted(nearestOnPolyline(p, medians[k])["d"] for p in pts)
            widths[k] = max(10.0, min(220.0, 2 * ds[len(ds) // 2]))
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
                # 垂直断面居中：矫正只按己方样本拟合造成的贴边
                medians[k] = recenterMedian(
                    medians[k], contours,
                    max(1.6 * widths[k], 1.2 * w0, 40.0))
        scoreMedians = [extendMedian(m, min(70.0, widths[k] * 1.1))
                        for k, m in enumerate(medians)]

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

    # ------------------------------------------------------------ 布尔收口 + 校验
    unionCheck = None
    if applyBooleanClamp:
        unionCheck = booleanClamp.clampStrokes(contours, strokes)
    if unionCheck is None:
        unionCheck = booleanClamp.reUnionCheck(contours, strokes)

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
        "unionCheck": unionCheck,
        "kai": {"strokes": kai["strokes"], "medians": kai["medians"],
                "strokeTypes": kai["strokeTypes"], "radical": kai["radical"],
                "decomposition": kai["decomposition"], "matches": kai["matches"],
                "structure": kai["structure"], "chaiziJt": kai["chaiziJt"],
                "chaiziFt": kai["chaiziFt"]},
        "pass": 1 if seedMedians is None else 2,
    }
    # ------------------------------------------------------------ 自洽回灌
    # 第一遍拆完后，用每笔自身几何重提干净中轴作种子重跑一遍匹配；
    # 双指标（失败笔数、retain+shapeSim）择优采用，防止吞并式虚高
    if selfConsistent and seedMedians is None:
        seeds = _selfSeeds(result)
        if seeds:
            r2 = runPipeline(dataHub, fontEntry, ch, applyBooleanClamp,
                             seedMedians=seeds, selfConsistent=False)
            expCenters = [affine(((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2))
                          for bb in kaiStrokeBBoxes]
            diag = math.hypot(tb.w, tb.h)
            if "error" not in r2 and _secondPassBetter(r2, result,
                                                      expCenters, diag):
                return r2
    return result
