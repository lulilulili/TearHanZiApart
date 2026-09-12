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
from .classify import (findLibEntry, similarTypes, PROBE_TABLE,
                       semanticSegments, matchTier)
from . import boolean as booleanClamp

ITERS = 5

# G8.5 梯队探针开关：开启时 result 附带 ladderProbe 信号（标定用），
# 探测本身无副作用。执行器待探测精度在 25 字族+健康集上标定后接入。
LADDER_PROBE = False
# G8.5 梯队执行器开关：探测标定达标（家族14/25、健康0/23、抽样0/200）
# 后接入。按 fired 部件的 plan 施行组重排+中轴带重置。
LADDER_ACT = True


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
    """横/竖的切割主轴偏离楷体该笔的弦向>32°。
    → [(笔序, 偏差度), ...]"""
    bad = []
    for s in result["strokes"]:
        if s["failed"] or s["type"] not in ("横", "竖"):
            continue
        d = shapeDescriptor([s["path"]])
        if d and d["elong"] >= 1.8:
            ang = math.degrees(d["mainAngle"]) % 180.0
            medians = result.get("kai", {}).get("medians", [])
            km = medians[s["index"]] if s["index"] < len(medians) else []
            if len(km) >= 2 and dist(tuple(km[0]), tuple(km[-1])) > 1e-6:
                ref = math.degrees(math.atan2(km[-1][1] - km[0][1],
                                             km[-1][0] - km[0][0])) % 180.0
            else:
                ref = 0.0 if s["type"] == "横" else 90.0
            dev = abs(ang - ref)
            dev = min(dev, 180.0 - dev)
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

    # 交叠件并组（门控：组数>笔画数才介入，否则完全维持原流程）。
    # 现代字体常不合并交叠轮廓（口=⊓件+底横条角部交叠靠 nonzero 并集
    # 渲染；Noto 尤碎，一笔可拆成 2+ 件）。组数>笔画数时匈牙利锚定
    # 无解、空组兜底退化成全开竞争，四码齐爆。公理的本义（用户裁定）：
    # 同一笔画不会断成**不相交**的连通组——相交的件就是连通的墨，
    # 并为一组；组数≤笔画数时嵌套分组已工作良好，不动。
    nKaiStrokes = len(kai["medians"])
    nG0 = max((c["group"] for c in contours), default=-1) + 1
    if nG0 > nKaiStrokes:
        try:
            from shapely.geometry import Polygon as _Pg0
            groupPoly = {}
            for c in contours:
                if c["isHole"]:
                    continue
                try:
                    pg = _Pg0(c["poly"])
                    if not pg.is_valid:
                        pg = pg.buffer(0)
                except Exception:
                    continue
                g = c["group"]
                groupPoly[g] = pg if g not in groupPoly \
                    else groupPoly[g].union(pg)
            parent = list(range(nG0))

            def _find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            keys = sorted(groupPoly.keys())
            for ai in range(len(keys)):
                for bi in range(ai + 1, len(keys)):
                    a, b = keys[ai], keys[bi]
                    pa, pb = groupPoly[a], groupPoly[b]
                    ba, bb2 = pa.bounds, pb.bounds
                    if ba[2] < bb2[0] or bb2[2] < ba[0] or \
                       ba[3] < bb2[1] or bb2[3] < ba[1]:
                        continue
                    try:
                        if pa.intersection(pb).area > 25.0:
                            ra, rb = _find(a), _find(b)
                            if ra != rb:
                                parent[rb] = ra
                    except Exception:
                        pass
            remap = {}
            for g in range(nG0):
                r = _find(g)
                if r not in remap:
                    remap[r] = len(remap)
            for c in contours:
                c["group"] = remap[_find(c["group"])]
        except Exception:
            pass

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
    # 指派相容性罚（诊断元修复：五个失败簇独立诊断收敛于同一结论——
    # 纯墨距离代价对轴向/尺寸完全失明，verify 的不变量应前置进代价矩阵
    # 而非事后仲裁补救）：
    # ①杆⊥笔罚 +200：杆状组(单外环无孔且elong≥2.5或bbox长宽比≥3)×
    #   定向笔(横/提/竖，名义弦长≥40)且轴向差>50°（爺k11/螟k2/騫k11/
    #   聞竖锚横杆族——匈牙利保底锚定曾把孤儿组锚给荒谬笔）
    # ②点锚大墨罚 +250：点笔×(组bbox对角线≥3.5×名义弦长且组墨占比
    #   ≥7%)（邸酞濮蜷——楷体点名义恰压长笔墨上代价≈0，匈牙利便
    #   "点锚大墨、长笔流放点斑"）
    penMatrix = {}
    try:
        _totInk = 0.0
        _gInk = {}
        for c in contours:
            a0 = abs(c["area"])
            if c["isHole"]:
                _gInk[c["group"]] = _gInk.get(c["group"], 0.0) - a0
            else:
                _gInk[c["group"]] = _gInk.get(c["group"], 0.0) + a0
                _totInk += a0
        _barAx = {}
        for g in range(nGroups):
            bb = groupBBoxes.get(g)
            if bb is None:
                continue
            outers0 = [c for c in contours
                       if c["group"] == g and not c["isHole"]]
            if len(outers0) != 1 or any(
                    c["group"] == g and c["isHole"] for c in contours):
                continue
            aspect0 = max(bb.w, bb.h) / max(1.0, min(bb.w, bb.h))
            if aspect0 >= 3.0:
                _barAx[g] = 0.0 if bb.w >= bb.h else 90.0
            else:
                dsc0 = shapeDescriptor([contourToPath(outers0[0]["segs"])])
                if dsc0 and dsc0["elong"] >= 2.5:
                    _barAx[g] = math.degrees(dsc0["mainAngle"]) % 180.0
        for k in range(nStrokes):
            m0k = initMedians[k]
            chord0 = dist(m0k[0], m0k[-1])
            t0 = kai["strokeTypes"][k]
            kAng0 = None
            if chord0 >= 40 and t0 in ("横", "竖", "提"):
                kAng0 = math.degrees(math.atan2(
                    m0k[-1][1] - m0k[0][1], m0k[-1][0] - m0k[0][0])) % 180.0
            for g in range(nGroups):
                pen = 0.0
                ax0 = _barAx.get(g)
                if ax0 is not None and kAng0 is not None:
                    dv0 = abs(ax0 - kAng0) % 180.0
                    if min(dv0, 180.0 - dv0) > 50.0:
                        pen += 200.0
                if t0 == "点":
                    bb = groupBBoxes.get(g)
                    if bb is not None and _totInk > 0:
                        diag0 = math.hypot(bb.w, bb.h)
                        if diag0 >= 3.5 * max(20.0, chord0) and                                 _gInk.get(g, 0.0) / _totInk >= 0.07:
                            pen += 250.0
                if pen:
                    penMatrix.setdefault(k, {})[g] = pen
    except Exception:
        pass
    strokeGroup = [min(range(nGroups), key=lambda g: costRows[k][g])
                   if nGroups else 0 for k in range(nStrokes)]
    if 0 < nGroups <= nStrokes:
        size = nStrokes
        # 方阵：行=笔画；前 nGroups 列=真实组，其余为"自由列"（代价=各笔
        # argmin，代表不锚定任何组、稍后就近入组）
        cost = []
        for k in range(nStrokes):
            free = min(costRows[k]) if nGroups else 0.0
            # 相容性罚只作用于锚定矩阵（argmin 就近入组与各仲裁窗口仍用
            # 纯几何代价）——罚进 costRows 本体曾把爱5竖的回家路也堵死
            pk = penMatrix.get(k, {})
            cost.append([costRows[k][g] + pk.get(g, 0.0)
                         for g in range(nGroups)] +
                        [free] * (size - nGroups))
        match = _hungarian(cost)
        for k in range(nStrokes):
            if match[k] < nGroups:
                strokeGroup[k] = match[k]
    groupStrokes = {g: [k for k in range(nStrokes) if strokeGroup[k] == g]
                    for g in range(nGroups)}

    # 空组语义认领（用户规则 v14，门控：组数>笔画数）：交叠并组后仍
    # 组数>笔画数 = 字体把复合笔画成了**不相交**的件（Noto 竖折=竖件
    # +横件），公理的连通性单位降到语义单元。空组按墨轴向找构词里含
    # 匹配单元的复合笔，取其 D 对应分段走廊滑动支撑最强者，以"子笔"
    # 身份认领该组（主组不变=多重入组，最终该笔路径为多片）；认领的
    # 内部顺序即构词顺序。找不到候选才退回全开竞争兜底。
    semanticClaims = []

    def _splitSections(m, angDeg=40.0):
        if len(m) < 3:
            return [list(m)]
        cosA = math.cos(math.radians(angDeg))
        secs = []
        cur = [m[0]]
        for i in range(1, len(m) - 1):
            v1 = (m[i][0] - m[i - 1][0], m[i][1] - m[i - 1][1])
            v2 = (m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            cur.append(m[i])
            if l1 > 1e-6 and l2 > 1e-6 and \
               (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) < cosA:
                secs.append(cur)
                cur = [m[i]]
        cur.append(m[-1])
        secs.append(cur)
        return secs

    def _semanticClaim(g):
        bb = groupBBoxes.get(g)
        if bb is None or (bb.w < 6 and bb.h < 6):
            return False
        gAxis = "h" if bb.w > bb.h * 1.5 else ("v" if bb.h > bb.w * 1.5 else "d")
        unitAxis = {"横": "h", "提": "h", "竖": "v"}
        try:
            from shapely.geometry import Polygon as _P1, LineString as _L1
            from shapely.affinity import translate as _t1
            reg = None
            for poly in groupOuters[g]:
                pg = _P1(poly)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                reg = pg if reg is None else reg.union(pg)
            if reg is None:
                return False
            for poly in groupHoles[g]:
                pg = _P1(poly)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                reg = reg.difference(pg)
            if reg.is_empty:
                return False
            best = None
            for k in range(nStrokes):
                units = semanticSegments(kai["strokeTypes"][k])
                if len(units) < 2 or costRows[k][g] > 220.0:
                    continue
                if gAxis != "d" and not any(
                        unitAxis.get(u, "d") in (gAxis, "d") for u in units):
                    continue
                for sec in _splitSections(medians[k]):
                    if len(sec) < 2:
                        continue
                    ddx = sec[-1][0] - sec[0][0]
                    ddy = sec[-1][1] - sec[0][1]
                    L = math.hypot(ddx, ddy)
                    if L < 6:
                        continue
                    sAxis = "h" if abs(ddx) > abs(ddy) * 1.5 else \
                        ("v" if abs(ddy) > abs(ddx) * 1.5 else "d")
                    if gAxis != "d" and sAxis != "d" and sAxis != gAxis:
                        continue
                    cor = _L1([tuple(p) for p in sec]).buffer(
                        max(14.0, min(120.0, min(bb.w, bb.h))) * 0.7)
                    nx1, ny1 = -ddy / L, ddx / L
                    rb = reg.bounds
                    span = max(rb[2] - rb[0], rb[3] - rb[1])
                    sup = 0.0
                    for tt in range(-6, 7):
                        off = span * 0.5 * tt / 6.0
                        c2 = cor if tt == 0 else _t1(cor, xoff=nx1 * off,
                                                     yoff=ny1 * off)
                        try:
                            inter = c2.intersection(reg)
                        except Exception:
                            continue
                        for gm in getattr(inter, "geoms", [inter]):
                            a = getattr(gm, "area", 0.0)
                            if a > sup:
                                sup = a
                    if sup > max(400.0, reg.area * 0.25) and \
                            (best is None or sup > best[0]):
                        best = (sup, k)
            if best is None:
                return False
            groupStrokes[g] = [best[1]]
            semanticClaims.append({"group": g, "stroke": best[1]})
            return True
        except Exception:
            return False

    for g in range(nGroups):
        if not groupStrokes[g]:  # 无笔画映射到该组
            if not (nGroups > nStrokes and _semanticClaim(g)):
                groupStrokes[g] = list(range(nStrokes))  # 兜底：放开限制

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

    def _compMate(a, b):
        """部件相容=一方为另一方前缀（matches 深化层级不齐：尃s3路径
        (0,1) 与 s2 的 (0,1,0) 是同部件，exact 全等曾误判非亲——只
        放宽 mate 认定、收紧迁移条件，不新增移动。"""
        if a is None or b is None:
            return False
        la = min(len(a), len(b))
        return a[:la] == b[:la]

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
                     if j != k and _compMate(_compOf(j), comp)]
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
            # 垂直蹲杆放逐（钾：杆内1平行主+1⊥竖，落在"超载需≥2平行"
            # 与"轴向错家需单笔组"的夹缝）：杆里有平行主时，⊥向的
            # 横/竖直笔且名义长>2×杆厚=墨装不下，放逐到孔腔包含本杆
            # 质心的环组（结构铁证同包含性豁免）
            perp = []
            bbG = groupBBoxes.get(g)
            if bbG is not None and len(par) >= 1:
                thickG = min(bbG.w, bbG.h)
                for k in ss:
                    if k in par:
                        continue
                    tK = kai["strokeTypes"][k]
                    if tK not in ("横", "竖"):
                        continue
                    mk = initMedians[k]
                    chordK = dist(mk[0], mk[-1])
                    aK = _medianAngle(k)
                    dv = abs(aK - barAng) % 180.0
                    if min(dv, 180.0 - dv) > 50.0 and chordK > 2.0 * thickG:
                        perp.append(k)
            if perp:
                gcS = groupCentroids.get(g)
                for kP in perp:
                    bestH2, bestS2 = -1, 400.0
                    for h in range(nGroups):
                        if h == g:
                            continue
                        contained2 = False
                        if gcS is not None:
                            for c0 in contours:
                                if c0["group"] == h and c0["isHole"] and                                         pointInPolygon(gcS, c0["poly"]):
                                    contained2 = True
                                    break
                        if not contained2 and costRows[kP][h] > max(
                                120.0, costRows[kP][g] + 100.0):
                            continue
                        sup2 = _support(kP, h)
                        if contained2:
                            sup2 *= 1.5
                        if sup2 > bestS2:
                            bestS2, bestH2 = sup2, h
                    if bestH2 >= 0:
                        groupStrokes[g].remove(kP)
                        strokeGroup[kP] = bestH2
                        if kP not in groupStrokes[bestH2]:
                            groupStrokes[bestH2].append(kP)
                            groupStrokes[bestH2].sort()
                        ss = groupStrokes.get(g, [])
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
            gcSelf = groupCentroids.get(g)
            for h in range(nGroups):
                if h == g:
                    continue
                hOk, hAng = _isBar(h)
                if hOk and any(_parallel(_medianAngle(k2), hAng)
                               for k2 in groupStrokes.get(h, [])):
                    continue
                # 包含性豁免：候选组带孔且超载杆质心落在其孔腔内——
                # "框包着悬浮杆"是结构铁证（甲早曱野的环底边名义代价
                # 90-150 远超代价窗被拒，恰是真家）
                contained = False
                if gcSelf is not None:
                    for c0 in contours:
                        if c0["group"] == h and c0["isHole"] and                                 pointInPolygon(gcSelf, c0["poly"]):
                            contained = True
                            break
                if not contained:
                    rows = [costRows[k][h] for k in par]
                    if min(rows) > max(60.0,
                                       min(costRows[k][g] for k in par) + 60.0):
                        continue
                sup = max(_support(k, h) for k in par)
                if contained:
                    sup *= 1.5  # 结构证据加权，优先环组
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
            # tie-break：两极端候选对杆的墨距离代价差>25 时直接放逐
            # 代价高者（甲：笔3代价63 vs 笔2代价28→放逐笔3；質/醌类
            # gap≈0 时回退落点序判定）
            cLo = costRows[ordered[0]][g]
            cHi = costRows[ordered[-1]][g]
            forced = None
            if abs(cHi - cLo) > 25.0:
                forced = ordered[-1] if cHi > cLo else ordered[0]
            chosen = None
            for exile in ({forced} if forced is not None
                          else {ordered[0], ordered[-1]}):
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

    # 轴向错家重排：横竖笔直出一根与其楷体轴向**垂直**的杆=铁证错家
    # （博6横直出竖杆、11竖撇直出横杆、8竖占斜片——三笔连环错位，
    # 同型互换救不了跨型链）。收集"单笔直出组×杆轴向与笔楷体弦向
    # 偏差>50°"的错家名单，名单内全排列重指派（轴向匹配代价最小），
    # 总偏差严格下降才施行。名单内排列=家数守恒，不会造成超载/空组。
    def _barAxisOf(g):
        bb = groupBBoxes.get(g)
        if bb is None:
            return None
        if bb.w >= 1.5 * max(1.0, bb.h):
            return 0.0
        if bb.h >= 1.5 * max(1.0, bb.w):
            return 90.0
        outers = [c for c in contours if c["group"] == g and not c["isHole"]]
        if len(outers) != 1:
            return None
        dsc = shapeDescriptor([contourToPath(outers[0]["segs"])])
        if dsc and dsc["elong"] >= 2.0:
            return math.degrees(dsc["mainAngle"]) % 180.0
        return None

    def _kaiChordAxis(k):
        m2 = kai["medians"][k]
        dx = m2[-1][0] - m2[0][0]
        dy = m2[-1][1] - m2[0][1]
        if math.hypot(dx, dy) < 40:
            return None
        return math.degrees(math.atan2(dy, dx)) % 180.0

    def _fold90(a, b):
        d = abs(a - b) % 180.0
        return min(d, 180.0 - d)

    misK = []
    misG = []
    for g in range(nGroups):
        ss = groupStrokes.get(g, [])
        if len(ss) != 1:
            continue
        k = ss[0]
        if kai["strokeTypes"][k] == "点":
            continue
        bAx = _barAxisOf(g)
        kAx = _kaiChordAxis(k)
        if bAx is None or kAx is None:
            continue
        if _fold90(bAx, kAx) > 50.0:
            misK.append(k)
            misG.append((g, bAx))
    if 2 <= len(misK) <= 5:
        import itertools
        axK = [_kaiChordAxis(k) for k in misK]

        def _permCost(perm):
            tot = 0.0
            for a, gi2 in enumerate(perm):
                dev = _fold90(axK[a], misG[gi2][1])
                if dev > 40.0:
                    return None
                tot += dev
            return tot

        cur = sum(_fold90(axK[a], misG[a][1]) for a in range(len(misK)))
        best = None
        for perm in itertools.permutations(range(len(misG))):
            c = _permCost(perm)
            if c is not None and (best is None or c < best[0]):
                best = (c, perm)
        if best is not None and best[0] < cur - 30.0:
            for a, gi2 in enumerate(best[1]):
                k = misK[a]
                gNew = misG[gi2][0]
                gOld = strokeGroup[k]
                if gNew == gOld:
                    continue
                strokeGroup[k] = gNew
            for g, _ in misG:
                groupStrokes[g] = [k for k in misK if strokeGroup[k] == g]

    # G7 槽位互换仲裁（种子字统计：92.2% 部件笔数与种子精确一致，
    # 槽位错位是比轴向/次序更硬的换家证据）：各一级槽位中心 = 该槽
    # 成员所在组墨质心的中位数（排除自身防污染）；互为错位的跨槽笔对
    # （各自到对方槽中心的距离×1.8 仍小于到本槽中心）且互换组的墨
    # 距离总代价不明显恶化(≤+60)则互换家。跨型允许——博的横占竖杆
    # 类连环错位常跨槽跨型。
    slotSwaps = []
    kaiMatches0 = kai.get("matches") or []

    def _slotOf0(k):
        p = kaiMatches0[k] if k < len(kaiMatches0) else None
        return p[0] if p else None

    slotMembers = {}
    for k in range(nStrokes):
        s0 = _slotOf0(k)
        if s0 is not None:
            slotMembers.setdefault(s0, []).append(k)
    if len(slotMembers) >= 2:
        def _gCent0(k):
            return groupCentroids.get(strokeGroup[k])

        def _slotCenter0(s0, excl):
            pts2 = []
            for k in slotMembers[s0]:
                if k == excl:
                    continue
                c = _gCent0(k)
                if c is not None:
                    pts2.append(c)
            if len(pts2) < 2:
                return None
            xs = sorted(p[0] for p in pts2)
            ys = sorted(p[1] for p in pts2)
            return (xs[len(xs) // 2], ys[len(ys) // 2])

        for _round in range(2):
            movedG7 = False
            for i in range(nStrokes):
                si = _slotOf0(i)
                ci = _gCent0(i)
                if si is None or ci is None:
                    continue
                own = _slotCenter0(si, i)
                if own is None:
                    continue
                dOwn = dist(ci, own)
                for j in range(nStrokes):
                    if j == i:
                        continue
                    sj = _slotOf0(j)
                    if sj is None or sj == si:
                        continue
                    cj = _gCent0(j)
                    if cj is None:
                        continue
                    ownJ = _slotCenter0(sj, j)
                    if ownJ is None:
                        continue
                    if dist(ci, ownJ) * 1.8 >= dOwn or \
                       dist(cj, own) * 1.8 >= dist(cj, ownJ):
                        continue
                    gi2, gj2 = strokeGroup[i], strokeGroup[j]
                    if gi2 == gj2:
                        continue
                    oldC = costRows[i][gi2] + costRows[j][gj2]
                    newC = costRows[i][gj2] + costRows[j][gi2]
                    if newC > oldC + 60.0:
                        continue
                    groupStrokes[gi2].remove(i)
                    groupStrokes[gj2].remove(j)
                    strokeGroup[i], strokeGroup[j] = gj2, gi2
                    groupStrokes[gj2].append(i)
                    groupStrokes[gj2].sort()
                    groupStrokes[gi2].append(j)
                    groupStrokes[gi2].sort()
                    slotSwaps.append([i, j])
                    movedG7 = True
                    break
            if not movedG7:
                break

    # 序保持互换仲裁（ORDER 主攻）：墨距离对同型笔在代价接近时会把
    # "家"分反——狗的犭撇与勹撇左右互换（偏差500+）、根的木4点上蹿，
    # median 与切割路径质心一致地违背楷序=指派错而非切割错。组指派
    # 代价里没有楷体次序约束，这里补上：同型/相似型且不同组的笔对，
    # 楷体质心某轴间距≥180 而目标组墨质心反向超60（verify ORDER 同
    # 判据）时，若互换两家的墨距离总代价不明显恶化（≤+30）则互换。
    kaiCentAll = [(sum(p[0] for p in m2) / len(m2),
                   sum(p[1] for p in m2) / len(m2))
                  for m2 in kai["medians"]]
    for _pass in range(2):
        swapped = False
        for i in range(nStrokes):
            for j in range(i + 1, nStrokes):
                gi, gj = strokeGroup[i], strokeGroup[j]
                if gi == gj:
                    continue
                ti, tj = kai["strokeTypes"][i], kai["strokeTypes"][j]
                if ti != tj and not matchTier(ti, tj):
                    continue
                ci2 = groupCentroids.get(gi)
                cj2 = groupCentroids.get(gj)
                if ci2 is None or cj2 is None:
                    continue
                bad = False
                for axis in (0, 1):
                    dK = kaiCentAll[j][axis] - kaiCentAll[i][axis]
                    dT = cj2[axis] - ci2[axis]
                    if abs(dK) >= 180.0 and dK * dT < 0 and abs(dT) > 60.0:
                        bad = True
                        break
                if not bad:
                    continue
                oldCost = costRows[i][gi] + costRows[j][gj]
                newCost = costRows[i][gj] + costRows[j][gi]
                if newCost > oldCost + 30.0:
                    continue
                groupStrokes[gi].remove(i)
                groupStrokes[gj].remove(j)
                strokeGroup[i], strokeGroup[j] = gj, gi
                groupStrokes[gj].append(i)
                groupStrokes[gj].sort()
                groupStrokes[gi].append(j)
                groupStrokes[gi].sort()
                swapped = True
        if not swapped:
            break

    # ------------------------------------------------------------ G8.5 梯队探针
    # 部件梯队秩配对探测层（诊断先行，零副作用，只读组指派与楷体名义
    # 布局，只产 ladderProbe；执行器待接入）。三轮标定教训：
    #  · v1 hostile（横躺非水平组）健康误触 11%——疒/厂/門族横撇融组
    #    是健康常态；
    #  · v2 barMisown+offBar 误触 16%——短横（弦长<100 被排出梯队池）
    #    合法占条被算成"错主"，故 v3 池收笔不设长度门；
    #  · v3 标定（家族25+健康点名23+通过抽样200）：孤立单条争议（事：
    #    彐横折独占扁宽组、稀条对密池配对欠定）是健康常态，触发必须
    #    成"链"（≥2 条占有≠配对）；門左右上横、鱄跨部件条是并排而非
    #    叠放，y 秩配对无意义，须过"竖直梯子"结构门。
    # 病字签名两型：
    #  A/B 型（尃族吞框：搏/博/縛/鎛 + 卑族日中横错档 sigD）：条组按
    #    墨 y 降序占有者 [3,7,10,5] vs 楷横池按楷 y 降序 [3,6,7,10]
    #    ——主序保序但整体错一位（6 困框、5 横折垫底接盘），逆序检不
    #    出，须做横池 × 条组的保序秩配对，比对占有关系与保序最优配对
    #    的差异；
    #  C 型（跨位抢条：睥/埤族）：条组占有者名义 x 走廊与条毫无重叠
    #    （睥·目底横 x≈226 抢走右侧卑底条 x≈500，y 上却贴合），纯 y
    #    模型不可见；而失所的正主（名义 x 走廊与条相容、名义 y 在梯队
    #    一档之内）困在融合组里。省形（鱄）同样有跨位条，但其候选正主
    #    名义 y 距条 1.8~2.6 档（睥/埤 ≤1.05 档）——字体少一档，正主
    #    不存在，档距门 1.2 挡住。
    # 触发即给出修复方案 plan=[[笔, 现组, 应去组], ...] 供执行器用。
    # 终版标定成绩：家族 14/25（博啤埤搏睥碑稗縛萆蜱郫鎛陴髀，含
    # 强制五字搏/博/縛/鎛/睥），健康点名 0/23，通过抽样 0/200 误触；
    # 未覆盖：礴（寸点貌合条剔除后链证据消失，保零误触的代价）、
    # 濞輻首鼽鼾齄（病在竖笔/切割中轴，条占有保序一致，横梯模型不可
    # 见）、痺（横捺占条单争议=事·彐横折健康签名同型）、颦（正主候选
    # 距条 2.6 档与鱄省形不可分）、導（现已通过验收）、鱄（省形，契约
    # 不触发）。
    ladderProbe = []
    if (LADDER_PROBE or (LADDER_ACT and seedMedians is None)) and \
            nStrokes >= 4 and kaiMatches0:
        _dbgLP = (LADDER_PROBE == 2)    # 标定调试：dump 全部件（含未触发）

        # 全字"纯横条组"表：单笔独占 + 宽≥100 + 主轴近水平。sigC 要越
        # 过部件边界看条（抢条者常来自邻部件），故全字收集；部件内
        # 秩配对按占有者∈成员过滤。
        allBars = []                       # [组, 占有者, 墨y, x0, x1]
        for g in sorted(groupStrokes):
            ss = groupStrokes.get(g, [])
            if len(ss) != 1:
                continue
            bbG = groupBBoxes.get(g)
            if bbG is None or bbG.w < 100.0:
                continue
            gax = _barAxisOf(g)
            if gax is None or min(gax, 180.0 - gax) > 32.0:
                continue
            c2 = groupCentroids.get(g)
            if c2 is None:
                continue
            allBars.append([g, ss[0], c2[1], bbG.x0, bbG.x1])

        _nomXRCache = {}

        def _nomXR(k):
            """笔 k 的楷体名义 x 走廊（affine 映射后的中轴 x 范围）。"""
            if k not in _nomXRCache:
                xs = [affine(p)[0] for p in kai["medians"][k]]
                _nomXRCache[k] = (min(xs), max(xs))
            return _nomXRCache[k]

        def _xOvOf(k, bar):
            """笔 k 名义 x 走廊与条组 x 范围的重叠占条宽比例。"""
            lo, hi = _nomXR(k)
            return ((min(hi, bar[4]) - max(lo, bar[3]))
                    / max(1.0, bar[4] - bar[3]))

        # 部件横池预扫：pool = 楷体横/提且弦向≤30°的全部笔（不设长度
        # 门——短横合法占条）；rungs = 池中弦长≥100 者，k≥2 才算梯子
        compPool = {}                      # ci -> (pool, rungs)
        for ci in sorted(slotMembers):
            pool = []
            rungs = []
            for k in slotMembers[ci]:
                if kai["strokeTypes"][k] not in ("横", "提"):
                    continue
                km = kai["medians"][k]
                if len(km) < 2:
                    continue
                dx = km[-1][0] - km[0][0]
                dy = km[-1][1] - km[0][1]
                chord = math.hypot(dx, dy)
                if chord < 1e-6:
                    continue
                ang = abs(math.degrees(math.atan2(dy, dx))) % 180.0
                if min(ang, 180.0 - ang) > 30.0:
                    continue
                pool.append(k)
                if chord >= 100.0:
                    rungs.append(k)
            if pool:
                compPool[ci] = (pool, rungs)

        # —— A/B 型：部件内 横池 × 条组 保序秩配对 ——
        for ci in sorted(compPool):
            pool, rungs = compPool[ci]
            if len(rungs) < 2:
                if _dbgLP:
                    ladderProbe.append({"comp": ci, "pool": pool,
                                        "rungs": rungs, "fired": False,
                                        "why": "rungs<2"})
                continue
            memberSet = set(slotMembers[ci])
            poolSet = set(pool)
            bars = [list(b) for b in allBars if b[1] in memberSet]
            kaiY = {k: affine(kaiCentAll[k])[1] for k in pool}
            poolSorted = sorted(pool, key=lambda k: -kaiY[k])
            gapsP = [kaiY[poolSorted[i]] - kaiY[poolSorted[i + 1]]
                     for i in range(len(poolSorted) - 1)]
            gapP = sum(gapsP) / len(gapsP) if gapsP else 0.0
            # 非池占有者的"貌合条"剔除：占有者虽非池笔，但名义 y 与条
            # 贴合（≤0.75 档）——横折/撇/撇折/竖折/捺类独占扁宽组是
            # 健康形态（鲁·魚头横折 Δ7、罴/握·厶撇折 Δ2~4、慟腫·撇
            # Δ51~58、謊·竖折 Δ14~23），sample200 的 11 例误触全由这类
            # 条强灌配对所致（#bars==#pool 时被迫全双射，把池笔拽向
            # 荒谬档位）；名义远者（搏族·甫横折 Δ403~424 垫底接盘）
            # 才是吞框签名，保留为错主证据
            dropped = []
            if gapP > 0:
                kept = []
                for b in bars:
                    if b[1] not in poolSet and \
                       abs(affine(kaiCentAll[b[1]])[1] - b[2]) <= 0.75 * gapP:
                        dropped.append(b[0])
                    else:
                        kept.append(b)
                bars = kept
            if not bars or len(bars) > len(pool):
                if _dbgLP:
                    ladderProbe.append({
                        "comp": ci, "pool": pool, "rungs": rungs,
                        "bars": [[b[0], b[1], round(b[2], 1)] for b in bars],
                        "dropped": dropped,
                        "fired": False, "why": "bars=%d pool=%d" % (
                            len(bars), len(pool))})
                continue
            bars.sort(key=lambda t: -t[2])           # 墨 y 降序（上→下）
            # 结构门：条组集必须构成"竖直梯子"才有 y 秩配对语义——
            #  · 两两 x 走廊重叠≥0.3×窄条宽（間/問的門左右上横、鱄的
            #    跨部件条是并排而非叠放，y 秩序无意义，标定误触源）；
            #  · 相邻档距≥30（同高并列条的 y 排序不可靠）。
            ladderOK = True
            for bi in range(len(bars)):
                for bj in range(bi + 1, len(bars)):
                    ov = (min(bars[bi][4], bars[bj][4])
                          - max(bars[bi][3], bars[bj][3]))
                    wmin = min(bars[bi][4] - bars[bi][3],
                               bars[bj][4] - bars[bj][3])
                    if ov < 0.3 * wmin:
                        ladderOK = False
            for bi in range(len(bars) - 1):
                if bars[bi][2] - bars[bi + 1][2] < 30.0:
                    ladderOK = False
            if not ladderOK and not _dbgLP:
                continue
            # 保序最小代价配对：dp[i][j]=前 i 条条组只用前 j 根池笔的
            # 最小 Σ|楷名义y-条y|；#bars==#pool 时退化为唯一保序双射
            nb, npl = len(bars), len(poolSorted)
            INF = 1e18
            dp = [[INF] * (npl + 1) for _ in range(nb + 1)]
            for j in range(npl + 1):
                dp[0][j] = 0.0
            for i in range(1, nb + 1):
                for j in range(i, npl + 1):
                    c = dp[i - 1][j - 1] + abs(bars[i - 1][2]
                                               - kaiY[poolSorted[j - 1]])
                    if dp[i][j - 1] < c:
                        c = dp[i][j - 1]
                    dp[i][j] = c
            paired = [None] * nb
            j = npl
            for i in range(nb, 0, -1):
                while j > i and dp[i][j] == dp[i][j - 1]:
                    j -= 1
                paired[i - 1] = poolSorted[j - 1]
                j -= 1
            ownerSet = {b[1] for b in bars}
            offBar = [k for k in pool if k not in ownerSet]
            misownBars = [b[0] for b in bars if b[1] not in poolSet]
            mismatch = [b[0] for b, pk in zip(bars, paired) if b[1] != pk]
            plan = [[pk, strokeGroup[pk], b[0]]
                    for b, pk in zip(bars, paired) if b[1] != pk]
            # 换位步幅门：配对给出的每一步 |楷名义y-条y| 必须 ≤1.15 档
            # ——真病链步幅 ≤0.93 档（搏族 0.82~0.93、卑族 ≤0.36），
            # 貌合条剔除后残留的荒谬配对（痕·艮横折衍生条 2.2 档、
            # 嫘·撇折条 2.9 档）步幅越档，是配对欠定而非换家证据
            planSane = (gapP >= 30.0 and
                        all(abs(kaiY[pk] - b[2]) <= 1.15 * gapP
                            for b, pk in zip(bars, paired) if b[1] != pk))
            sigA = bool(set(misownBars) & set(mismatch))
            sigB = any(b[1] in poolSet and b[1] != pk
                       for b, pk in zip(bars, paired))
            # sigD（卑族日中横错档）：允许单条争议触发的特异化路径——
            # 争议条占有者必须是池笔（排除 事·彐横折独占扁宽组的合法
            # 单争议），且配对首选笔困在"含竖类笔"的融合组（卑的日中
            # 横与贯穿竖融组；疒/厂的横撇融组不含竖，v1 hostile 的
            # 11% 误触由此隔离）
            sigD = False
            planD = []
            for b, pk in zip(bars, paired):
                if b[1] == pk or b[1] not in poolSet:
                    continue
                if pk in ownerSet:
                    continue
                gA = strokeGroup[pk]
                mates = groupStrokes.get(gA, [])
                if any(m != pk and kai["strokeTypes"][m].startswith("竖")
                       for m in mates):
                    sigD = True
                    planD.append([pk, gA, b[0]])
            # 触发门：错位须成"链"（≥2 条组占有≠配对）——孤立单条争议
            # （事：彐横折独占扁宽组、稀条对密池配对欠定）是健康常态，
            # v2 的单条 misown 即触发正是 16% 误触之源；且须 ∃池笔
            # offBar（有笔被挤走——单纯少一根条=省形，不触发）。
            # sigD 结构证据充分，豁免链长门，但要求**恰一根失所**
            # （#bars=#pool-1，梯子被其余条锚定、秩归属确定）——碱·咸
            # 1条对3池2失所是配对欠定，DP 就近猜中过一次健康占有者
            # （在野误伤 AREA 2%→10%，抽样门实证）。
            firedAB = (ladderOK and planSane and (sigA or sigB) and offBar
                       and len(mismatch) >= 2)
            firedD = (ladderOK and planSane and sigD
                      and len(offBar) == 1)
            fired = firedAB or firedD
            if fired or _dbgLP:
                ladderProbe.append({
                    "comp": ci, "pool": poolSorted, "rungs": rungs,
                    "kaiY": {k: round(v, 1) for k, v in kaiY.items()},
                    "gapP": round(gapP, 1), "dropped": dropped,
                    "bars": [[b[0], b[1], round(b[2], 1),
                              round(b[3]), round(b[4]),
                              kai["strokeTypes"][b[1]],
                              round(affine(kaiCentAll[b[1]])[1], 1)]
                             for b in bars],
                    "paired": paired, "offBar": offBar,
                    "misownBars": misownBars, "mismatch": mismatch,
                    "plan": plan if firedAB else planD,
                    "mode": "AB" if firedAB else "D",
                    "sigA": sigA, "sigB": sigB, "sigD": sigD,
                    "ladderOK": ladderOK, "fired": fired,
                })

        # —— C 型：跨位条占用（睥/埤族，纯 y 模型不可见）——
        # 条组占有者名义 x 走廊与条重叠 <0.25 条宽（跨位铁证：睥·目底
        # 横名义在左却独占右侧条），且存在失所正主 r：某梯子部件的长
        # 横，不独占任何 x 相容条，名义 x 走廊与该条重叠 ≥0.5，名义 y
        # 距条 ≤1.2×部件档距（标定：睥 0.99 档/埤 ≤1.04 档为真病，鱄
        # 省形候选 1.8~2.6 档——字体少一档，正主不存在，档距门挡住）
        for bar in allBars:
            tOv = _xOvOf(bar[1], bar)
            if tOv >= 0.25:
                continue
            best = None
            cands = []
            for ci in sorted(compPool):
                pool, rungs = compPool[ci]
                if len(rungs) < 2:
                    continue
                ys = sorted((affine(kaiCentAll[k])[1] for k in pool),
                            reverse=True)
                gaps = [ys[i] - ys[i + 1] for i in range(len(ys) - 1)]
                gap = sum(gaps) / len(gaps) if gaps else 0.0
                if gap < 30.0:
                    continue
                for r in rungs:
                    if r == bar[1]:
                        continue
                    # r 失所 = 不独占任何与自己 x 相容的条
                    if any(b[1] == r and _xOvOf(r, b) >= 0.25
                           for b in allBars):
                        continue
                    rOv = _xOvOf(r, bar)
                    dY = abs(affine(kaiCentAll[r])[1] - bar[2])
                    if _dbgLP:
                        cands.append([r, ci, round(rOv, 2), round(dY, 1),
                                      round(gap, 1)])
                    if rOv < 0.5 or dY > 1.2 * gap:
                        continue
                    if best is None or dY < best[0]:
                        best = (dY, r, ci)
            if best is not None or (_dbgLP and cands):
                ladderProbe.append({
                    "comp": best[2] if best else None, "sigC": True,
                    "mode": "C",
                    "bar": [bar[0], bar[1], round(bar[2], 1),
                            round(bar[3]), round(bar[4]),
                            kai["strokeTypes"][bar[1]], round(tOv, 2)],
                    "cands": cands,
                    "plan": ([[best[1], strokeGroup[best[1]], bar[0]]]
                             if best else []),
                    "fired": best is not None,
                })

    # ------------------------------------------------------------ G8.5 执行器
    # 对 fired 部件按 plan 施行：组重排 + 中轴重置到目标条带 y。
    # 评审红线落实：
    #  · 采纳门不用名义 costRows（对本病失明——名义压错档时旧组代价
    #    ≈0，Σ窗会否决旗舰修复、放行近距误伤），用**重置后**中轴调
    #    strokeGroupCost 评新组贴合度（≤40）；
    #  · all-or-nothing：任一步不达标整包回滚；
    #  · 被顶出的原独占者（搏5横折垫底接盘/睥·目底横跨位窃占）名义
    #    中轴重置后按重置代价 argmin 重指派，叠 penMatrix 杆⊥罚
    #    （≥200 一票否决），禁投本轮已认领的条组；
    #  · 空组断言：不得新增空组（休眠网既有空组豁免——组数>笔数时
    #    S1b 语义认领的空组是常态）；
    #  · ladderTouched 组在 G9 豁免——G9 是整组仿射复位，会把刚锚定
    #    的带位搬走（对抗评审实锤）。
    ladderRealign = []
    ladderTouched = set()
    if LADDER_ACT and seedMedians is None and ladderProbe:
        _preEmpty = {g for g in range(nGroups) if not groupStrokes.get(g)}
        _claimed = set()
        for entry in ladderProbe:
            if entry.get("fired") and entry.get("plan"):
                for _, _, _gN in entry["plan"]:
                    _claimed.add(_gN)
        for entry in ladderProbe:
            if not entry.get("fired") or not entry.get("plan"):
                continue
            plan = entry["plan"]
            barY = {b[0]: b[2] for b in entry.get("bars", [])}
            if entry.get("bar"):
                barY[entry["bar"][0]] = entry["bar"][2]
            movers = {k for k, _, _ in plan}
            # 被顶出者：plan 目标条的现独占者且不在 movers 里
            displaced = []
            for _, _, gNew in plan:
                ss = groupStrokes.get(gNew, [])
                if len(ss) == 1 and ss[0] not in movers:
                    displaced.append(ss[0])
            touch = movers | set(displaced)
            savG = {k: strokeGroup[k] for k in touch}
            savM = {k: (medians[k], initMedians[k]) for k in touch}
            savGS = {}

            def _snapG(g):
                if g not in savGS:
                    savGS[g] = list(groupStrokes.get(g, []))

            def _move(k, gTo):
                gFrom = strokeGroup[k]
                _snapG(gFrom)
                _snapG(gTo)
                if k in groupStrokes.get(gFrom, []):
                    groupStrokes[gFrom].remove(k)
                strokeGroup[k] = gTo
                groupStrokes.setdefault(gTo, []).append(k)
                groupStrokes[gTo].sort()

            ok = True
            rej = None
            steps = []
            for k, gOld, gNew in plan:
                if strokeGroup[k] != gOld:
                    ok = False
                    rej = "drift:%d" % k
                    break
                km = [affine(tuple(p)) for p in kai["medians"][k]]
                if gNew in barY:
                    cy = sum(p[1] for p in km) / len(km)
                    km = [(x, y + (barY[gNew] - cy)) for x, y in km]
                medians[k] = km
                initMedians[k] = [tuple(p) for p in km]
                _move(k, gNew)
                steps.append([k, gOld, gNew])
            if ok:
                _vacated = [gO for _, gO, _ in plan]
                _swapMode = entry.get("mode") in ("D", "C")
                for o in displaced:
                    km = [affine(tuple(p)) for p in kai["medians"][o]]
                    medians[o] = km
                    initMedians[o] = [tuple(p) for p in km]
                    # D/C 型是正主↔窃占者互换：被逐者(啤7/睥·目底横)
                    # 的真档融在正主腾出的融合组里，全局 affine 重置
                    # 代价对它必然虚高（部件内档位错位正是本病），
                    # 直接对调、组内归属交给采样迭代分拣。penMatrix
                    # 杆⊥罚不适用——那是给匈牙利锚定的"猜测"设的，
                    # 互换目的地是正主刚腾出的组（它已实证住得下横；
                    # sigD 的"含竖融合组"证据即结构担保），罚在卑族
                    # 全族(啤碑稗萆蜱郫陴)一票否决过正当互换。
                    if _swapMode and _vacated:
                        dest = _vacated[0]
                    else:
                        row = strokeGroupCost(o)
                        cand = sorted(_vacated, key=lambda g2: row[g2]) + \
                            sorted(range(nGroups), key=lambda g2: row[g2])
                        dest = None
                        for g in cand:
                            if g in _claimed or g == strokeGroup[o]:
                                continue
                            lim = 90.0 if g in _vacated else 60.0
                            if row[g] > lim:
                                continue
                            if penMatrix.get(o, {}).get(g, 0) >= 200:
                                continue
                            dest = g
                            break
                        if dest is None:
                            ok = False
                            rej = "noDest:%d" % o
                            break
                    gO = strokeGroup[o]
                    _move(o, dest)
                    steps.append([o, gO, dest])
            if ok:
                for k, _, gNew in plan:
                    _c = strokeGroupCost(k)[gNew]
                    if _c > 40.0:
                        ok = False
                        rej = "cost:%d=%.0f" % (k, _c)
                        break
            if ok and any(not groupStrokes.get(g)
                          for g in range(nGroups) if g not in _preEmpty):
                ok = False
                rej = "emptyGroup"
            if not ok:
                entry["actReject"] = rej
                for k, g0 in savG.items():
                    strokeGroup[k] = g0
                for k, (m0, im0) in savM.items():
                    medians[k] = m0
                    initMedians[k] = im0
                for g, ss in savGS.items():
                    groupStrokes[g] = ss
                continue
            ladderRealign.extend(steps)
            for k, gOld, gNew in steps:
                ladderTouched.add(gOld)
                ladderTouched.add(gNew)

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
