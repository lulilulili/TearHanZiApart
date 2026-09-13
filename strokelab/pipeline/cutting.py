# -*- coding: utf-8 -*-
"""strokelab.pipeline.cutting — 主人判定 → 边弧整体归属 → 标签平滑 →
标签跳变处 De Casteljau 矢量切割 → 环路追踪/桥接划分式重构。

边界样本标签在此定稿并转为矢量弧段：整轮廓主人判定（饿死保护）、
按角点切边弧整体归属（孔洞断面探针/径向对应）、短游程平滑、二分
精确切点（吸附角点）、开弧桥接成环（笔锋三次贝塞尔修复）与碎片环
剪除。全部事故史注释（天的捺、口左竖、日孔顶边、TC 中左竖等）保留。
"""

import math

from ..geometry import (bezPoint, bezSlice, bezTangent, contourToPath,
                        corridorPoint, cubicSeg, dist, lineSeg,
                        pointInPolygon, resamplePolyline, segLength)


def ownerJudge(ctx):
    """主人判定整体归属（设计位+样本份额双证据，饿死保护）。"""
    contours = ctx.contours
    nStrokes = ctx.nStrokes
    medians = ctx.medians
    initMedians = ctx.initMedians
    sampleSets = ctx.sampleSets
    contourAllowed = ctx.contourAllowed

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


def consolidateArcs(ctx):
    """边弧整体归属：外轮廓收敛→孔洞径向对应→孔洞整弧。"""
    contours = ctx.contours
    nStrokes = ctx.nStrokes
    widths = ctx.widths
    w0 = ctx.w0
    sampleSets = ctx.sampleSets
    contourAllowed = ctx.contourAllowed
    scoreOf = ctx.scoreOf

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

    ctx.cornerSets = cornerSets


def labelSmooth(ctx):
    """标签平滑：短游程并入前邻，消除零星误标。"""
    sampleSets = ctx.sampleSets

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

    ctx.tick("整体归属与平滑")


def vectorCut(ctx):
    """矢量切割：标签跳变二分定位切点（吸附角点），产出边弧。"""
    contours = ctx.contours
    nStrokes = ctx.nStrokes
    sampleSets = ctx.sampleSets
    contourAllowed = ctx.contourAllowed
    cornerSets = ctx.cornerSets
    labelOf = ctx.labelOf

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

    ctx.cutPoints = cutPoints
    ctx.strokeArcs = strokeArcs
    ctx.tick("矢量切割")


def reconstruct(ctx):
    """划分式重构：环路追踪+桥接+碎片环剪除，产出 strokes。"""
    kai = ctx.kai
    nStrokes = ctx.nStrokes
    widths = ctx.widths
    medians = ctx.medians
    strokeArcs = ctx.strokeArcs
    templateSources = ctx.templateSources
    templatePaths = ctx.templatePaths
    strokeGroup = ctx.strokeGroup

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

    ctx.strokes = strokes
    ctx.tick("重构")


def run(ctx):
    """主人判定→边弧归属→平滑→切割→重构定序执行。"""
    ownerJudge(ctx)
    consolidateArcs(ctx)
    labelSmooth(ctx)
    vectorCut(ctx)
    reconstruct(ctx)
