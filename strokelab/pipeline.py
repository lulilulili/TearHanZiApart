# -*- coding: utf-8 -*-
"""strokelab.pipeline — 拆解管线：D 构建 → 归属 → 精调 → 矢量切割 → 划分式重构。

流程（v5 架构）：
  D = B库同类型笔画骨架（A↔B 映射产物，目标字体自己的形态）按楷体结构 C 定位；
  D 与目标字形实际路径匹配 → 归属/迭代精调 → 标签跳变处 De Casteljau 精确切割 →
  环路追踪 + 直割线弦 + 跨轮廓并环重构 → 布尔收口（boolean.clampStrokes）保证
  所有笔画并集与原字形恒等（不多不少，交叠区双重归属）。
"""

import math

from .geometry import (dist, lineSeg, parseContours, contourToPath, flattenSegs,
                       bboxOfPoints, pointInPolygon, nearestOnPolyline,
                       polylineLength, resamplePolyline, analyzeContours,
                       bezPoint, bezTangent, bezSlice, segLength,
                       shapeDescriptor, shapeSimilarity, refineMedianFit)
from .classify import findLibEntry
from . import boolean as booleanClamp

ITERS = 5


def runPipeline(dataHub, fontEntry, ch, applyBooleanClamp=True):
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
    templatePaths = []
    for k, m in enumerate(kai["medians"]):
        t = kai["strokeTypes"][k]
        ent = findLibEntry(fontEntry.libraryB, t) if fontEntry.libraryB else None
        if not ent and fontEntry.libraryB and t.endswith("钩"):
            ent = findLibEntry(fontEntry.libraryB, t[:-1])
        placed = None
        placedPath = ""
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

                def mv(p):
                    return (c0[0] + (p[0] - eb[0]) * tw / ew,
                            c0[1] + (p[1] - eb[1]) * th / ehh)

                placed = [mv(p) for p in skel]
                parts = []
                for d in ent["contours"]:
                    for c in parseContours(d):
                        moved = [(c_[0], mv(c_[1]), mv(c_[2]), mv(c_[3]), mv(c_[4]))
                                 for c_ in c["segs"]]
                        parts.append(contourToPath(moved))
                placedPath = " ".join(parts)
                templateSources.append(ent["source"])
            except Exception:
                placed = None
        if placed is None:
            placed = [affine(p) for p in m]
            templateSources.append("楷体中轴线(B库缺类型)")
            placedPath = ""
        medians.append(placed)
        templatePaths.append(placedPath)
    initMedians = [[tuple(p) for p in m] for m in medians]
    nStrokes = len(medians)

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

    def labelOf(pt, tan):
        best, bestScore = 0, 1e18
        for k in range(nStrokes):
            sc = scoreOf(pt, tan, k)
            if sc < bestScore:
                bestScore, best = sc, k
        return best

    for it in range(ITERS):
        assigned = [[] for _ in range(nStrokes)]
        for arr in sampleSets:
            for sm in arr:
                sm["label"] = labelOf(sm["pt"], sm["tan"])
                assigned[sm["label"]].append(sm["pt"])
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
        scoreMedians = [extendMedian(m, min(70.0, widths[k] * 1.1))
                        for k, m in enumerate(medians)]

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
        fracs = []
        for k in range(nStrokes):
            fr = fracInside(resampledMedians[k], c["poly"])
            fi = fracInside(resampledInit[k], c["poly"])
            fracs.append((max(fr, fi), min(fr, fi), k))
        fracs.sort(key=lambda x: -x[0])
        fMax1, fMin1, winner = fracs[0]
        fMax2 = fracs[1][0] if len(fracs) > 1 else 0
        if fMax1 < 0.55 or fMin1 < 0.35 or fMax2 >= 0.45:
            continue
        votes = {}
        for sm in sampleSets[ci]:
            votes[sm["label"]] = votes.get(sm["label"], 0) + 1
        starve = any(k != winner and strokeSampleTotals[k] - v < 6
                     for k, v in votes.items())
        if starve:
            continue
        for sm in sampleSets[ci]:
            if sm["label"] != winner:
                strokeSampleTotals[sm["label"]] -= 1
                strokeSampleTotals[winner] += 1
                sm["label"] = winner

    # 孔洞边界标签径向对应（环形结构内外一致）
    for ci, c in enumerate(contours):
        if not c["isHole"]:
            continue
        for sm in sampleSets[ci]:
            best, bestD = -1, 1e18
            for cj, c2 in enumerate(contours):
                if c2["isHole"]:
                    continue
                for sm2 in sampleSets[cj]:
                    d = dist(sm["pt"], sm2["pt"])
                    if d < bestD:
                        bestD, best = d, sm2["label"]
            if best >= 0:
                sm["label"] = best

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
            for _ in range(22):
                mid = (s0 + s1) / 2
                pt, tan = paramAt(mid % segCount)
                if labelOf(pt, tan) == a["label"]:
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
        retainedLen = 0.0
        bridgeLen = 0.0
        for a in arcs:
            if a["closed"]:
                loops.append({"segs": a["segs"], "bridges": 0})
                retainedLen += sum(segLength(x) for x in a["segs"])
        openArcs = [a for a in arcs if not a["closed"]]
        byContour = {}
        for a in openArcs:
            byContour.setdefault(a["contour"], []).append(a)

        def bridge(pA, pB):
            if dist(pA, pB) < 1.2:
                return None
            return lineSeg(pA, pB)

        chains = []
        for _, chainArcs in byContour.items():
            segs = []
            bridges = 0
            for idx, a in enumerate(chainArcs):
                segs.extend(a["segs"])
                retainedLen += sum(segLength(x) for x in a["segs"])
                if idx < len(chainArcs) - 1:
                    gap = bridge(a["segs"][-1][4], chainArcs[idx + 1]["segs"][0][1])
                    if gap:
                        segs.append(gap)
                        bridges += 1
                        bridgeLen += segLength(gap)
            chains.append({"segs": segs, "bridges": bridges})
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
                gap = bridge(end, nxt["segs"][0][1])
                if gap:
                    cur["segs"].append(gap)
                    cur["bridges"] += 1
                    bridgeLen += segLength(gap)
                cur["segs"].extend(nxt["segs"])
                cur["bridges"] += nxt["bridges"]
            wrap = bridge(cur["segs"][-1][4], cur["segs"][0][1])
            if wrap:
                cur["segs"].append(wrap)
                cur["bridges"] += 1
                bridgeLen += segLength(wrap)
            loops.append(cur)

        pathD = " ".join(contourToPath(lp["segs"]) for lp in loops)
        strokes.append({
            "index": k, "type": kai["strokeTypes"][k], "path": pathD,
            "loops": len(loops),
            "bridges": sum(lp["bridges"] for lp in loops),
            "retainRatio": retainedLen / ((retainedLen + bridgeLen) or 1.0),
            "median": [[round(p[0], 1), round(p[1], 1)] for p in medians[k]],
            "width": widths[k],
            "template": templateSources[k],
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

    return {
        "ch": ch, "font": fontEntry.key,
        "contours": [{"path": contourToPath(c["segs"]), "isHole": c["isHole"],
                      "group": c["group"], "segCount": len(c["segs"]),
                      "ccw": c["area"] >= 0} for c in contours],
        "samples": [[[round(sm["pt"][0], 1), round(sm["pt"][1], 1), sm["label"]]
                     for sm in arr] for arr in sampleSets],
        "cutPoints": [[round(cp["pt"][0], 1), round(cp["pt"][1], 1)]
                      for cp in cutPoints],
        "strokes": strokes,
        "unionCheck": unionCheck,
        "kai": {"strokes": kai["strokes"], "medians": kai["medians"],
                "strokeTypes": kai["strokeTypes"], "radical": kai["radical"],
                "decomposition": kai["decomposition"], "matches": kai["matches"],
                "structure": kai["structure"], "chaiziJt": kai["chaiziJt"],
                "chaiziFt": kai["chaiziFt"]},
    }
