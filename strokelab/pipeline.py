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

from .geometry import (dist, lineSeg, cubicSeg, parseContours, contourToPath,
                       nearestBatch,
                       flattenSegs,
                       bboxOfPoints, pointInPolygon, nearestOnPolyline,
                       polylineLength, resamplePolyline, analyzeContours,
                       bezPoint, bezTangent, bezSlice, segLength,
                       shapeDescriptor, shapeSimilarity, refineMedianFit,
                       recenterMedian, corridorPoint,
                       outlineCenterline, midpointRectify,
                       straightenSections, signedArea)
from .classify import findLibEntry, similarTypes, PROBE_TABLE
from . import boolean as booleanClamp

ITERS = 5


def _medianDeviation(placed, kaiPlaced):
    """模板骨架放置后与楷体中轴线的形态偏差：按弧长重采样 24 点对齐的
    平均点距 / 楷体中轴线包围盒对角线。None=无法比较。"""
    if len(placed) < 2 or len(kaiPlaced) < 2:
        return None
    b = bboxOfPoints(kaiPlaced)
    diag = math.hypot(b.w, b.h)
    if diag < 1e-6:
        return None
    n = 24

    def resampleN(poly):
        L = polylineLength(poly)
        if L < 1e-6:
            return [tuple(poly[0])] * n
        step = L / (n - 1)
        out = [tuple(poly[0])]
        carry = 0.0
        for i in range(len(poly) - 1):
            x1, y1 = poly[i]
            x2, y2 = poly[i + 1]
            seg = math.hypot(x2 - x1, y2 - y1)
            if seg < 1e-9:
                continue
            t = step - carry
            while t <= seg and len(out) < n:
                u = t / seg
                out.append((x1 + (x2 - x1) * u, y1 + (y2 - y1) * u))
                t += step
            carry = seg - (t - step)
        while len(out) < n:
            out.append(tuple(poly[-1]))
        return out

    a = resampleN(placed)
    b2 = resampleN(kaiPlaced)
    avg = sum(dist(p, q) for p, q in zip(a, b2)) / n
    return avg / diag


def _hungarian(cost):
    """O(n^3) 匈牙利算法（方阵最小代价完美匹配），返回每行匹配的列号。"""
    n = len(cost)
    INF = 1e18
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    ans = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            ans[p[j] - 1] = j - 1
    return ans


def _switchbackCount(m, thr=75.0):
    """折返计数：相邻段方向角变化超 thr 的内点数（中轴线蛇形伪影探测）。"""
    n = 0
    cosThr = math.cos(math.radians(thr))
    for i in range(1, len(m) - 1):
        ax, ay = m[i][0] - m[i - 1][0], m[i][1] - m[i - 1][1]
        bx, by = m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la > 1e-9 and lb > 1e-9 and \
           (ax * bx + ay * by) / (la * lb) < cosThr:
            n += 1
    return n


def _axisFails(result):
    """verify TYPE 同款判据：名义横/竖的切割结果 PCA 主轴偏差>32°。
    → [(笔序, 偏差度), ...]"""
    bad = []
    for s in result["strokes"]:
        if s["failed"] or s["type"] not in ("横", "竖"):
            continue
        d = shapeDescriptor([s["path"]])
        if d and d["elong"] >= 1.8:
            ang = abs(math.degrees(d["mainAngle"])) % 180.0
            dev = min(ang, 180.0 - ang) if s["type"] == "横" \
                else abs(ang - 90.0)
            if dev > 32.0:
                bad.append((s["index"], dev))
    return bad


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
            continue
        seeds.append(rm)
        if not changed:
            # 种子与精调中轴几乎重合时不算变化——重跑必然收敛回原样、
            # 过不了采纳门槛，白付一遍全程
            m0 = [tuple(p) for p in s["median"]]
            disp = sum(nearestOnPolyline(p, m0)["d"]
                       for p in rm) / (len(rm) or 1)
            if disp > max(2.5, 0.08 * s["width"]):
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
    # 并集硬保证守卫：第二遍不得引入覆盖/溢出违规（流江"水"的二遍
    # 曾溢出 14.2% 仍被 retain 虚高采纳）
    u1 = r1.get("unionCheck") or {}
    u2 = r2.get("unionCheck") or {}
    if u2.get("cover", 100) < min(99.0, u1.get("cover", 100)) - 0.05:
        return False
    if abs(u2.get("excess", 0)) > max(0.5, abs(u1.get("excess", 0))):
        return False
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

    _timings = []
    _tw = [time.perf_counter()]

    def _tick(name):
        now = time.perf_counter()
        _timings.append([name, round((now - _tw[0]) * 1000)])
        _tw[0] = now

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
    # 同类型可有多个库候选（八的撇/儿的撇形态不同），逐笔由一致性检查挑
    typeEntries = {}
    for e in (fontEntry.libraryBAll or []):
        typeEntries.setdefault(e["type"], []).append(e)
    for k, m in enumerate(kai["medians"]):
        t = kai["strokeTypes"][k]
        kaiPlaced = [affine(p) for p in m]
        placed = None
        # 依次尝试：本类型 → 相似组类型（借用），每个候选都过模板-结构
        # 一致性检查——同名类型分段比例可能迥异（宀的横钩钩段占 13%，
        # 横撇模板撇段占 60%，压进矮扁包围盒后长尾侵入邻笔），而相似
        # 类型的模板反而可能更合身（横折钩↔横折的设计摇摆）
        if fontEntry.libraryB:
            cands = []
            seen = set()
            for tc in [t] + similarTypes(t):
                for ent in typeEntries.get(tc, []):
                    if id(ent) not in seen:
                        seen.add(id(ent))
                        cands.append((tc, ent))
            bestOwn = None   # 本类型最优 (dev, tc, ent, cand)
            bestBor = None   # 借用最优
            for tc, ent in cands:
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
                    cand = [(c0[0] + (p[0] - eb[0]) * tw / ew,
                             c0[1] + (p[1] - eb[1]) * th / ehh) for p in skel]
                    dev = _medianDeviation(cand, kaiPlaced)
                    # 借用相似类型需要更强证据（快乐体日的横曾借提模板酿祸）；
                    # 方言型（规则表外）的映射候选同样从严 0.18——这些类型
                    # 此前走楷体回退（楷体自己的笔形=正版参照），映射的标准
                    # 笔形必须明显贴合才有资格顶替（女1竖捺曾被巡·右㇛模板
                    # 以 0.28 松闸顶掉，打乱女旁划分）
                    if tc == t:
                        thr = 0.18 if (ent.get("kind", "").startswith("map")
                                       and t not in PROBE_TABLE) else 0.28
                    else:
                        thr = 0.20
                    if dev is None or dev > thr:
                        continue
                    if tc == t:
                        if bestOwn is None or dev < bestOwn[0]:
                            bestOwn = (dev, tc, ent, cand)
                    else:
                        if bestBor is None or dev < bestBor[0]:
                            bestBor = (dev, tc, ent, cand)
                except Exception:
                    continue
            # 本类型优先：借用必须比本类型最优再好出明显幅度（0.08）才换——
            # 微弱优势的借用曾把鸿蒙的横全换成提模板、竖换成弯钩，初始 D
            # 歪斜误导；真正的设计摇摆（月的竖实为竖撇）优势显著，仍可借
            best = bestOwn
            if bestBor is not None and                (bestOwn is None or bestBor[0] < bestOwn[0] - 0.08):
                best = bestBor
            if best is not None:
                dev, tc, ent, placed = best
                templateSources.append(
                    ent["source"] + ("" if tc == t else "（借%s）" % tc))
                templateEnts.append(ent)
        if placed is None:
            placed = kaiPlaced
            templateSources.append("楷体中轴线(B库缺类型/形态错配)")
            templateEnts.append(None)
        # 分段吸直：同类型 D 骨架拓扑必须一致（横折=干净的7字两直段，
        # 不因楷体回退带弧弯变C样；直笔=直线；真曲段保持）
        placed = straightenSections(placed)
        medians.append(placed)
    if seedMedians is not None:
        # 自洽回灌：第一遍逐笔干净中轴替换 B 库定位的 D（结构先验已由
        # 第一遍消化进种子里）
        medians = [[tuple(p) for p in m] for m in seedMedians]
        templateSources = ["自洽回灌"] * len(medians)
        templateEnts = [None] * len(medians)
    initMedians = [[tuple(p) for p in m] for m in medians]
    nStrokes = len(medians)

    _tick("解析对齐/D构建")

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

    groupBBoxes = {}
    groupCoarse = {}
    for g in range(nGroups):
        pts = [p for poly in groupOuters[g] for p in poly]
        groupBBoxes[g] = bboxOfPoints(pts) if pts else None
        groupCoarse[g] = [resamplePolyline(poly, 30)
                          for poly in groupOuters[g] + groupHoles[g]]

    def strokeGroupCost(k):
        """笔画→各组的"墨距离"（墨内=0，否则到组边界最近距离）均值向量。
        inside 占比法对 ⊓ 形带状轮廓失效（鸿蒙"日"的竖中轴悬在空腔里），
        距离法对带状/实心都稳。性能：远组用包围盒距离下界代替精算（远组
        只在匈牙利被迫指派时才可能选中，下界不影响近组排序）；近组的
        边界距离用粗采样折线。"""
        rm = resamplePolyline([tuple(p) for p in initMedians[k]], 20)
        mb = bboxOfPoints(rm)
        row = []
        for g in range(nGroups):
            bb = groupBBoxes[g]
            if bb is None:
                row.append(1e18)
                continue
            gapX = max(0.0, max(mb.x0, bb.x0) - min(mb.x1, bb.x1))
            gapY = max(0.0, max(mb.y0, bb.y0) - min(mb.y1, bb.y1))
            gap = math.hypot(gapX, gapY)
            if gap > 60.0:
                row.append(gap)
                continue
            outers = groupOuters[g]
            holes = groupHoles[g]
            total = 0.0
            for p in rm:
                if any(pointInPolygon(p, poly) for poly in outers) and \
                   not any(pointInPolygon(p, hp) for hp in holes):
                    d = 0.0
                else:
                    d = min(nearestOnPolyline(p, poly)["d"]
                            for poly in groupCoarse[g])
                total += d
            row.append(total / (len(rm) or 1))
        return row

    # 全局最优指派：每组先由匈牙利算法配一个"锚定笔"（保证无空组——逐笔
    # 独立 argmin 曾让㡭的点挤进邻笔的组、正主组空置沦为全开竞争），
    # 余下笔画再就近入组。组数=笔画数时退化为严格一一对应
    costRows = [strokeGroupCost(k) for k in range(nStrokes)]
    strokeGroup = [min(range(nGroups), key=lambda g: costRows[k][g])
                   if nGroups else 0 for k in range(nStrokes)]
    if 0 < nGroups <= nStrokes:
        size = nStrokes
        # 方阵：行=笔画；前 nGroups 列=真实组，其余为"自由列"（代价=各笔
        # argmin，代表不锚定任何组、稍后就近入组）
        cost = []
        for k in range(nStrokes):
            free = min(costRows[k]) if nGroups else 0.0
            cost.append([costRows[k][g] for g in range(nGroups)] +
                        [free] * (size - nGroups))
        match = _hungarian(cost)
        for k in range(nStrokes):
            if match[k] < nGroups:
                strokeGroup[k] = match[k]
    groupStrokes = {g: [k for k in range(nStrokes) if strokeGroup[k] == g]
                    for g in range(nGroups)}
    for g in range(nGroups):
        if not groupStrokes[g]:  # 无笔画映射到该组（异常兜底）：放开限制
            groupStrokes[g] = list(range(nStrokes))

    # 走廊可容纳度仲裁：墨距离对"名义位置不落在任何组墨内"的笔会就近
    # 错分——好的提名义位置横穿撇点宽腰（墨距离 59 胜出），真身在撇的
    # 轮廓里但高度有偏（墨距离 86 落选），错分后在撇点组饿死、救济还
    # 从撇点墨里切了条假提。对前两名接近且都不贴合的笔，用走廊滑动
    # 吸附的最大单片面积（与救济同思路）比较两组谁能真正容纳整条笔，
    # 明显更能容纳（≥1.3×）才改判；原组不得因此空置。
    try:
        from shapely.geometry import LineString as _LS, Polygon as _Pg
        from shapely.affinity import translate as _tr
        _gReg = {}

        def _groupRegion(g):
            if g not in _gReg:
                reg = None
                for poly in groupOuters[g]:
                    pg = _Pg(poly)
                    if not pg.is_valid:
                        pg = pg.buffer(0)
                    reg = pg if reg is None else reg.union(pg)
                if reg is not None:
                    for poly in groupHoles[g]:
                        pg = _Pg(poly)
                        if not pg.is_valid:
                            pg = pg.buffer(0)
                        reg = reg.difference(pg)
                    if not reg.is_valid:
                        reg = reg.buffer(0)
                _gReg[g] = reg
            return _gReg[g]

        wEst = max(14.0, min(180.0,
                   sum(abs(c["area"]) for c in contours if not c["isHole"]) /
                   (sum(polylineLength(m) for m in medians) or 1.0)))

        def _support(k, g):
            reg = _groupRegion(g)
            m = initMedians[k]
            if reg is None or reg.is_empty or len(m) < 2:
                return 0.0
            cor = _LS([tuple(p) for p in m]).buffer(wEst * 0.6)
            ddx = m[-1][0] - m[0][0]
            ddy = m[-1][1] - m[0][1]
            L = math.hypot(ddx, ddy) or 1.0
            nx, ny = -ddy / L, ddx / L
            rb = reg.bounds
            span = max(rb[2] - rb[0], rb[3] - rb[1])
            best = 0.0
            for t in range(-5, 6):
                off = span * 0.4 * t / 5.0
                c2 = cor if t == 0 else _tr(cor, xoff=nx * off, yoff=ny * off)
                try:
                    inter = c2.intersection(reg)
                except Exception:
                    continue
                for gm in getattr(inter, "geoms", [inter]):
                    a = getattr(gm, "area", 0.0)
                    if a > best:
                        best = a
            return best

        if nGroups >= 2:
            for k in range(nStrokes):
                row = costRows[k]
                order = sorted(range(nGroups), key=lambda g: row[g])
                g1 = order[0]
                if strokeGroup[k] != g1:
                    continue  # 匈牙利锚定改动过的不碰
                # 候选=代价窗口内的所有组（第二名未必是真主：好的提，
                # 撇组排第三）
                cands = [g for g in order[1:]
                         if row[g] - row[g1] <= max(35.0, row[g1] * 0.6)]
                if row[g1] < 15.0 or not cands:
                    continue
                if len(groupStrokes[g1]) <= 1:
                    continue
                s1 = _support(k, g1)
                bestG, bestS = -1, s1 * 1.3
                for g in cands:
                    sg = _support(k, g)
                    if sg > bestS and sg > 400.0:
                        bestS, bestG = sg, g
                if bestG >= 0:
                    strokeGroup[k] = bestG
                    groupStrokes[g1].remove(k)
                    if k not in groupStrokes[bestG]:
                        groupStrokes[bestG].append(k)
                        groupStrokes[bestG].sort()
    except Exception:
        pass

    # 部件同组仲裁：墨距离对"名义位置穿过他部件长笔"的短笔会错分——
    # 爱的冖左竖名义下半段穿过友的长横（墨内代价≈0）错入长横组，真身
    # （冖左垂）在横钩组。matches 部件路径是结构证据：同部件的笔在设计
    # 上倾向连通同组。当前组无任何同部件笔、代价窗口内的他组有同部件笔
    # 且不空置原组时，改判到含同部件笔的最低代价组。
    kaiMatches = kai.get("matches") or []

    def _compOf(k):
        p = kaiMatches[k] if k < len(kaiMatches) else None
        return tuple(p) if p else None

    if nGroups >= 2 and kaiMatches:
        for k in range(nStrokes):
            g1 = strokeGroup[k]
            row = costRows[k]
            if g1 != min(range(nGroups), key=lambda g: row[g]):
                continue  # 匈牙利锚定改动过的不碰
            comp = _compOf(k)
            if comp is None:
                continue
            mates = [j for j in range(nStrokes)
                     if j != k and _compOf(j) == comp]
            if not mates or any(strokeGroup[j] == g1 for j in mates):
                continue
            if len(groupStrokes[g1]) <= 1:
                continue
            bestH = None
            for j in mates:
                h = strokeGroup[j]
                if h == g1:
                    continue
                if row[h] - row[g1] <= max(35.0, row[g1] * 0.6):
                    if bestH is None or row[h] < row[bestH]:
                        bestH = h
            if bestH is not None:
                groupStrokes[g1].remove(k)
                strokeGroup[k] = bestH
                if k not in groupStrokes[bestH]:
                    groupStrokes[bestH].append(k)
                    groupStrokes[bestH].sort()

    # 单杆组超载重指派：楷体的封底横在现代设计中常并入外框轮廓（貝/酉
    # 的目底、日底），其名义位置又恰压在腔内悬浮横杆上（代价0）——墨
    # 距离把两条楷体横同判给一根杆，杆内竞争必然一伤（質12/醌7 retain
    # 曾掉到50%且100%重叠）。判据：组轮廓=单外环无孔且高伸长（就是一
    # 根杆），组内≥2笔与杆同向。放逐目标=代价邻近、走廊滑动支撑充分的
    # 组（禁选已含同向笔的单杆组——防杆间跳槽再造超载）；放逐者按**序
    # 保持**选：目标在杆哪一侧，就放逐名义位置偏那一侧最远的笔（封底
    # 横名义恰压杆上、按邻近选留必错——楷体次序是唯一可靠证据）。
    try:
        barLike = {}

        def _isBar(g):
            if g not in barLike:
                outers = [c for c in contours
                          if c["group"] == g and not c["isHole"]]
                ok = False
                ang = 0.0
                if len(outers) == 1 and not any(
                        c["group"] == g and c["isHole"] for c in contours):
                    dsc = shapeDescriptor([contourToPath(outers[0]["segs"])])
                    bb = groupBBoxes.get(g)
                    # PCA伸长率对短粗杆偏低（醌酉杆3.4），bbox长宽比兜底
                    aspect = (max(bb.w, bb.h) / max(1.0, min(bb.w, bb.h))
                              if bb else 0.0)
                    if dsc and (dsc["elong"] >= 4.0 or aspect >= 4.0):
                        ok = True
                        ang = math.degrees(dsc["mainAngle"]) % 180.0
                barLike[g] = (ok, ang)
            return barLike[g]

        def _medianAngle(k):
            m = initMedians[k]
            return math.degrees(math.atan2(m[-1][1] - m[0][1],
                                           m[-1][0] - m[0][0])) % 180.0

        def _parallel(a, b):
            d = abs(a - b)
            return min(d, 180.0 - d) <= 30.0

        for g in range(nGroups):
            ok, barAng = _isBar(g)
            if not ok:
                continue
            ss = list(groupStrokes.get(g, []))
            par = [k for k in ss if _parallel(_medianAngle(k), barAng)]
            if len(par) < 2:
                continue
            gc = groupCentroids.get(g)
            if gc is None:
                continue
            # 垂轴坐标：质心在杆法向上的投影（横杆≈y，竖杆≈x）
            rad = math.radians(barAng)
            nx, ny = -math.sin(rad), math.cos(rad)

            def _perp(pt):
                return (pt[0] - gc[0]) * nx + (pt[1] - gc[1]) * ny

            def _cen(k):
                m = initMedians[k]
                return (sum(p[0] for p in m) / len(m),
                        sum(p[1] for p in m) / len(m))

            # 放逐目标：全体笔的候选里支撑最强的组
            bestH, bestS = -1, 400.0
            for h in range(nGroups):
                if h == g:
                    continue
                hOk, hAng = _isBar(h)
                if hOk and any(_parallel(_medianAngle(k2), hAng)
                               for k2 in groupStrokes.get(h, [])):
                    continue
                rows = [costRows[k][h] for k in par]
                if min(rows) > max(60.0, min(costRows[k][g] for k in par) + 60.0):
                    continue
                sup = max(_support(k, h) for k in par)
                if sup > bestS:
                    bestS, bestH = sup, h
            if bestH < 0:
                continue

            def _supportOff(k, h):
                """_support 的带落点版：返回 (最大单片面积, 最优法向位移)。"""
                reg = _groupRegion(h)
                m = initMedians[k]
                if reg is None or reg.is_empty or len(m) < 2:
                    return 0.0, 0.0
                from shapely.geometry import LineString as _LS2
                from shapely.affinity import translate as _tr2
                cor = _LS2([tuple(p) for p in m]).buffer(wEst * 0.6)
                ddx = m[-1][0] - m[0][0]
                ddy = m[-1][1] - m[0][1]
                L = math.hypot(ddx, ddy) or 1.0
                cnx, cny = -ddy / L, ddx / L
                rb = reg.bounds
                span = max(rb[2] - rb[0], rb[3] - rb[1])
                best, bestOff = 0.0, 0.0
                for tt in range(-5, 6):
                    off = span * 0.4 * tt / 5.0
                    c2 = cor if tt == 0 else _tr2(cor, xoff=cnx * off,
                                                  yoff=cny * off)
                    try:
                        inter = c2.intersection(reg)
                    except Exception:
                        continue
                    for gm in getattr(inter, "geoms", [inter]):
                        a = getattr(gm, "area", 0.0)
                        if a > best:
                            best, bestOff = a, off
                # 位移方向换算到杆法向的垂轴坐标增量
                return best, bestOff * (cnx * nx + cny * ny)

            # 落点序一致性：只在上下两个极端里挑放逐者——放逐后其实际
            # 落点（走廊最优滑动位置）必须保持楷体给定的上下次序（酉框
            # 质心在杆上方、可容墨的框底在杆下方，按质心猜方向曾放错笔）
            ordered = sorted(par, key=lambda k: _perp(_cen(k)))
            chosen = None
            for exile in {ordered[0], ordered[-1]}:
                sup, dPerp = _supportOff(exile, bestH)
                if sup <= 400.0:
                    continue
                land = _perp(_cen(exile)) + dPerp
                others = [_perp(_cen(k)) for k in par if k != exile]
                if exile == ordered[-1] and land < max(others) - 10.0:
                    continue
                if exile == ordered[0] and land > min(others) + 10.0:
                    continue
                if chosen is None or sup > chosen[1]:
                    chosen = (exile, sup)
            if chosen is None:
                continue
            exile = chosen[0]
            groupStrokes[g].remove(exile)
            strokeGroup[exile] = bestH
            if exile not in groupStrokes[bestH]:
                groupStrokes[bestH].append(exile)
                groupStrokes[bestH].sort()
    except Exception:
        pass

    # 疑抢杆认领互换：全局仿射会把楷体某横的名义位置恰好压到目标内部
    # 悬浮横杆上（威：楷体顶横名义 y 落在戌内短横杆上，代价0抢走该杆
    # 直出；真身顶横的墨融合在外框组、无人认领→残差回填给竖=竖轴偏
    # 49°；真正的杆主（笔3）被挤去邻组竞争）。判别信号=**笔比杆长**：
    # 直出组里笔的名义轴长明显超过杆长（>1.08×），说明杆装不下它、是
    # 抢来的。处置成链：k1 让出杆、去认领"墨面积远超组内笔画预期"的
    # 孤儿组（走廊支撑最强处落位）；k2（同向、代价窗口内）顺位回填杆；
    # 楷体名义次序与两者落点次序一致才施行，每字至多一链。
    try:
        glyphInk = sum((-abs(c["area"]) if c["isHole"] else abs(c["area"]))
                       for c in contours)
        kaiAreaShares = []
        for sp in kai["strokes"]:
            a = 0.0
            for c in parseContours(sp):
                a += abs(signedArea(flattenSegs(c["segs"], 15)))
            kaiAreaShares.append(a)
        kaiTotA = sum(kaiAreaShares) or 1.0
        expArea = [glyphInk * a / kaiTotA for a in kaiAreaShares]

        def _cenOf(k):
            m = initMedians[k]
            return (sum(p[0] for p in m) / len(m),
                    sum(p[1] for p in m) / len(m))

        def _axisLen(k):
            m = initMedians[k]
            return dist(m[0], m[-1])

        def _supLand(k, h):
            """走廊滑动支撑 + 最优落点位移向量。"""
            reg = _groupRegion(h)
            m = initMedians[k]
            if reg is None or reg.is_empty or len(m) < 2:
                return 0.0, (0.0, 0.0)
            from shapely.geometry import LineString as _L3
            from shapely.affinity import translate as _t3
            cor = _L3([tuple(p) for p in m]).buffer(wEst * 0.6)
            ddx = m[-1][0] - m[0][0]
            ddy = m[-1][1] - m[0][1]
            L = math.hypot(ddx, ddy) or 1.0
            cnx, cny = -ddy / L, ddx / L
            rb = reg.bounds
            span = max(rb[2] - rb[0], rb[3] - rb[1])
            best, bo = 0.0, 0.0
            for tt in range(-6, 7):
                off = span * 0.45 * tt / 6.0
                c2 = cor if tt == 0 else _t3(cor, xoff=cnx * off,
                                             yoff=cny * off)
                try:
                    inter = c2.intersection(reg)
                except Exception:
                    continue
                for gm in getattr(inter, "geoms", [inter]):
                    a = getattr(gm, "area", 0.0)
                    if a > best:
                        best, bo = a, off
            return best, (cnx * bo, cny * bo)

        done = False
        for g0 in range(nGroups):
            if done:
                break
            ss0 = groupStrokes.get(g0, [])
            if len(ss0) != 1:
                continue
            k1 = ss0[0]
            bb0 = groupBBoxes.get(g0)
            if bb0 is None:
                continue
            m1 = initMedians[k1]
            a1 = math.degrees(math.atan2(m1[-1][1] - m1[0][1],
                                         m1[-1][0] - m1[0][0])) % 180.0
            # 杆沿笔轴向的长度
            rad = math.radians(a1)
            ux, uy = math.cos(rad), math.sin(rad)
            barLen = abs(bb0.w * ux) + abs(bb0.h * uy)
            if _axisLen(k1) <= barLen * 1.08:
                continue
            # 认领目标：孤儿墨显著的组（墨面积-组内楷体预期 ≥ 0.5×本笔预期）
            bestU = None
            for gU in range(nGroups):
                if gU == g0 or costRows[k1][gU] > 140.0:
                    continue
                reg = _groupRegion(gU)
                if reg is None or reg.is_empty:
                    continue
                orphan = reg.area - sum(expArea[k]
                                        for k in groupStrokes.get(gU, []))
                if orphan < max(0.5 * expArea[k1], 900.0):
                    continue
                sup, offv = _supLand(k1, gU)
                if sup < max(800.0, 0.35 * expArea[k1]):
                    continue
                if bestU is None or sup > bestU[0]:
                    bestU = (sup, gU, offv)
            if bestU is None:
                continue
            _, gU, off1 = bestU
            c1 = _cenOf(k1)
            land1 = (c1[0] + off1[0], c1[1] + off1[1])
            # 回填者 k2：同向、代价窗口内、原组不空置、楷序=落点序
            bestK2 = None
            for k2 in range(nStrokes):
                if k2 == k1 or strokeGroup[k2] == g0:
                    continue
                if len(groupStrokes.get(strokeGroup[k2], [])) < 2:
                    continue
                m2 = initMedians[k2]
                a2 = math.degrees(math.atan2(m2[-1][1] - m2[0][1],
                                             m2[-1][0] - m2[0][0])) % 180.0
                d = abs(a1 - a2)
                if min(d, 180.0 - d) > 30.0:
                    continue
                if costRows[k2][g0] > 140.0:
                    continue
                sup2, off2 = _supLand(k2, g0)
                if sup2 < 400.0:
                    continue
                c2p = _cenOf(k2)
                land2 = (c2p[0] + off2[0], c2p[1] + off2[1])
                nx1, ny1 = -math.sin(rad), math.cos(rad)
                sKai = (c1[0] - c2p[0]) * nx1 + (c1[1] - c2p[1]) * ny1
                sLand = (land1[0] - land2[0]) * nx1 + (land1[1] - land2[1]) * ny1
                if sKai * sLand < 0:
                    continue
                if bestK2 is None or sup2 > bestK2[0]:
                    bestK2 = (sup2, k2)
            if bestK2 is None:
                continue
            k2 = bestK2[1]
            g2 = strokeGroup[k2]
            groupStrokes[g0].remove(k1)
            strokeGroup[k1] = gU
            groupStrokes[gU].append(k1)
            groupStrokes[gU].sort()
            groupStrokes[g2].remove(k2)
            strokeGroup[k2] = g0
            groupStrokes[g0].append(k2)
            groupStrokes[g0].sort()
            done = True
    except Exception:
        pass

    # 组局部重锚定（失配门控，用户设想：相对位置代替绝对位置）：全局
    # 仿射是绝对定位，部件比例悬殊时组内名义布局整体错位——磷·石口
    # 高瘦，楷体口的三笔名义全挤在组上半段，封底横悬在腔体中间，组下
    # 三分之一无人认领。组内≥2笔、成员名义联合框对组墨框轴向覆盖
    # <0.7 时，改用楷体**相对布局**：联合名义框→组墨框整体仿射重映射
    # （轴对齐缩放，横竖臂保持横竖）。v10 无门控全量复位曾净负收益
    # ——放对的也被搬乱；门控确保只救真错位，正常字零扰动。
    groupRemapInfo = []
    for g in range(nGroups):
        ss = groupStrokes.get(g, [])
        if len(ss) < 2:
            continue
        bb = groupBBoxes.get(g)
        if bb is None or bb.w < 8 or bb.h < 8:
            continue
        pts = [p for k in ss for p in initMedians[k]]
        nb = bboxOfPoints(pts)
        if nb.w < 4 or nb.h < 4:
            continue
        covX = max(0.0, min(nb.x1, bb.x1) - max(nb.x0, bb.x0)) / max(1.0, bb.w)
        covY = max(0.0, min(nb.y1, bb.y1) - max(nb.y0, bb.y0)) / max(1.0, bb.h)
        if min(covX, covY) >= 0.7:
            continue
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
        # 单笔单连通终态收口：回填/减除偶发的断笔在此修复
        booleanClamp.enforceConnectivity(contours, strokes)
        # 终态校验统一按实际路径口径（clampStrokes 返回值基于裁剪区域，
        # 会掩盖未被替换路径的残余溢出）
        unionCheck = booleanClamp.reUnionCheck(contours, strokes, glyph=_glyph)
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
        "unionCheck": unionCheck,
        "kai": {"strokes": kai["strokes"], "medians": kai["medians"],
                "strokeTypes": kai["strokeTypes"], "radical": kai["radical"],
                "decomposition": kai["decomposition"], "matches": kai["matches"],
                "structure": kai["structure"], "chaiziJt": kai["chaiziJt"],
                "chaiziFt": kai["chaiziFt"],
                "components": kai.get("components", [])},
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
            for s in result["strokes"]:
                if s["index"] in _bad or not s.get("median"):
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
                    result = r3
    return result
