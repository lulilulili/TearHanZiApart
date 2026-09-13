# -*- coding: utf-8 -*-
"""strokelab.pipeline.arbitrate — 指派仲裁链 G2..G8 + G8.5 梯队秩配对。

G2 走廊可容纳度 → G3 部件同组 → G4 单杆组超载重指派 → G5 疑抢杆认领
互换 → G6 轴向错家重排 → G7 槽位互换 → G8 序保持互换 → G8.5 梯队
探针/执行器。每层只在其判据的铁证成立时改判，全部事故史注释随代码
保留。层序即证据强度递增序（编排见包 __init__），不可重排。
LADDER_PROBE/LADDER_ACT 经 sys.modules 读包属性当前值——
tools/ladder_calib3.py 等以 pl.LADDER_PROBE=2 方式运行期改写。

运行期桥本体（groupRegionOf/corridorSupport/barAxisOfGroup）为模块级
函数，functools.partial 绑定子结构视图后挂 groups.regionOf/support/
barAxisOf，供 G4/G5/G8.5 与 anchor.G10 复用。
"""

import functools
import itertools
import math
import sys

from ..classify import matchTier
from ..geometry import (contourToPath, dist, flattenSegs, parseContours,
                        pointInPolygon, polylineLength, shapeDescriptor,
                        signedArea)


def _pkg():
    """包模块本体：运行期读 LADDER_* 开关的当前值（import 时不固化）。"""
    return sys.modules["strokelab.pipeline"]


# ============================================================ 运行期桥本体

def groupRegionOf(cache, groups, g):
    """组→shapely 区域（外环并集−孔洞差集，cache 记忆化）。
    groups.regionOf 以 partial 绑定缓存与组表后即原 _groupRegion 闭包；
    anchor.G10 以独立缓存复用同一本体。"""
    if g not in cache:
        from shapely.geometry import Polygon as _Pg
        reg = None
        for poly in groups.groupOuters[g]:
            pg = _Pg(poly)
            if not pg.is_valid:
                pg = pg.buffer(0)
            reg = pg if reg is None else reg.union(pg)
        if reg is not None:
            for poly in groups.groupHoles[g]:
                pg = _Pg(poly)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                reg = reg.difference(pg)
            if not reg.is_valid:
                reg = reg.buffer(0)
        cache[g] = reg
    return cache[g]


def corridorSupport(regionOf, pose, k, g):
    """笔×组的走廊滑动支撑最大单片面积（原 _support 闭包本体；
    groups.support 以 partial 绑定 regionOf/pose 后保持 (k, g) 签名，
    走廊半径经 pose.wEst 读取）。"""
    reg = regionOf(g)
    m = pose.initMedians[k]
    if reg is None or reg.is_empty or len(m) < 2:
        return 0.0
    from shapely.geometry import LineString as _LS
    from shapely.affinity import translate as _tr
    cor = _LS([tuple(p) for p in m]).buffer(pose.wEst * 0.6)
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


def barAxisOfGroup(geom, groups, g):
    """单杆组→主轴角（原 _barAxisOf 闭包本体；G6 以 partial 绑定
    geom/groups 后挂 groups.barAxisOf，G8.5 探针复用）。"""
    bb = groups.groupBBoxes.get(g)
    if bb is None:
        return None
    if bb.w >= 1.5 * max(1.0, bb.h):
        return 0.0
    if bb.h >= 1.5 * max(1.0, bb.w):
        return 90.0
    outers = [c for c in geom.contours
              if c["group"] == g and not c["isHole"]]
    if len(outers) != 1:
        return None
    dsc = shapeDescriptor([contourToPath(outers[0]["segs"])])
    if dsc and dsc["elong"] >= 2.0:
        return math.degrees(dsc["mainAngle"]) % 180.0
    return None


# ============================================================ 几何小件

def _medianAngle(initMedians, k):
    """笔 k 初始中轴的弦向角（0..180°）。"""
    m = initMedians[k]
    return math.degrees(math.atan2(m[-1][1] - m[0][1],
                                   m[-1][0] - m[0][0])) % 180.0


def _medianCenter(initMedians, k):
    """笔 k 初始中轴的点集质心。"""
    m = initMedians[k]
    return (sum(p[0] for p in m) / len(m),
            sum(p[1] for p in m) / len(m))


def _parallel(a, b):
    """两轴向角是否近平行（差 ≤30°，模 180）。"""
    d = abs(a - b)
    return min(d, 180.0 - d) <= 30.0


def _fold90(a, b):
    """两轴向角的折叠差（0..90°）。"""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


# ============================================================ G2 走廊可容纳度

def corridorFit(geom, groups, pose, cost):
    """G2 走廊可容纳度仲裁（并挂 groups.regionOf/support、pose.wEst）。"""
    contours = geom.contours
    medians = pose.medians
    nGroups = groups.nGroups
    nStrokes = pose.nStrokes
    costRows = cost.costRows
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes

    # 走廊可容纳度仲裁：墨距离对"名义位置不落在任何组墨内"的笔会就近
    # 错分——好的提名义位置横穿撇点宽腰（墨距离 59 胜出），真身在撇的
    # 轮廓里但高度有偏（墨距离 86 落选），错分后在撇点组饿死、救济还
    # 从撇点墨里切了条假提。对前两名接近且都不贴合的笔，用走廊滑动
    # 吸附的最大单片面积（与救济同思路）比较两组谁能真正容纳整条笔，
    # 明显更能容纳（≥1.3×）才改判；原组不得因此空置。
    try:
        from shapely.geometry import LineString  # noqa: F401 —— 可用性探测

        wEst = max(14.0, min(180.0,
                   sum(abs(c["area"]) for c in contours if not c["isHole"]) /
                   (sum(polylineLength(m) for m in medians) or 1.0)))
        pose.wEst = wEst

        # 桥挂载：单杆超载/疑抢杆/G8.5 复用同一 region 缓存与走廊支撑
        _groupRegion = functools.partial(groupRegionOf, {}, groups)
        _support = functools.partial(corridorSupport, _groupRegion, pose)
        groups.regionOf = _groupRegion
        groups.support = _support

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


# ============================================================ G3 部件同组

def _componentOf(kaiMatches, k):
    """笔 k 的楷体部件路径（tuple，无则 None）。"""
    p = kaiMatches[k] if k < len(kaiMatches) else None
    return tuple(p) if p else None


def _componentMate(a, b):
    """部件相容=一方为另一方前缀（matches 深化层级不齐：尃s3路径
    (0,1) 与 s2 的 (0,1,0) 是同部件，exact 全等曾误判非亲——只
    放宽 mate 认定、收紧迁移条件，不新增移动。"""
    if a is None or b is None:
        return False
    la = min(len(a), len(b))
    return a[:la] == b[:la]


def componentMate(kaiRef, groups, pose, cost):
    """G3 部件同组仲裁（matches 部件路径为结构证据）。"""
    kai = kaiRef.kai
    nGroups = groups.nGroups
    nStrokes = pose.nStrokes
    strokeGroup = groups.strokeGroup
    costRows = cost.costRows
    groupStrokes = groups.groupStrokes

    # 部件同组仲裁：墨距离对"名义位置穿过他部件长笔"的短笔会错分——
    # 爱的冖左竖名义下半段穿过友的长横（墨内代价≈0）错入长横组，真身
    # （冖左垂）在横钩组。matches 部件路径是结构证据：同部件的笔在设计
    # 上倾向连通同组。当前组无任何同部件笔、代价窗口内的他组有同部件笔
    # 且不空置原组时，改判到含同部件笔的最低代价组。
    kaiMatches = kai.get("matches") or []

    if nGroups >= 2 and kaiMatches:
        for k in range(nStrokes):
            g1 = strokeGroup[k]
            row = costRows[k]
            if g1 != min(range(nGroups), key=lambda g: row[g]):
                continue  # 匈牙利锚定改动过的不碰
            comp = _componentOf(kaiMatches, k)
            if comp is None:
                continue
            mates = [j for j in range(nStrokes)
                     if j != k and _componentMate(
                         _componentOf(kaiMatches, j), comp)]
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


# ============================================================ G4 单杆组超载

def _isBarGroup(cache, geom, groups, g):
    """组 g 是否单杆状（单外环无孔且高伸长），返回 (ok, 主轴角)，
    cache 记忆化（原 _isBar 闭包本体）。"""
    if g not in cache:
        outers = [c for c in geom.contours
                  if c["group"] == g and not c["isHole"]]
        ok = False
        ang = 0.0
        if len(outers) == 1 and not any(
                c["group"] == g and c["isHole"] for c in geom.contours):
            dsc = shapeDescriptor([contourToPath(outers[0]["segs"])])
            bb = groups.groupBBoxes.get(g)
            # PCA伸长率对短粗杆偏低（醌酉杆3.4），bbox长宽比兜底
            aspect = (max(bb.w, bb.h) / max(1.0, min(bb.w, bb.h))
                      if bb else 0.0)
            if dsc and (dsc["elong"] >= 4.0 or aspect >= 4.0):
                ok = True
                ang = math.degrees(dsc["mainAngle"]) % 180.0
        cache[g] = (ok, ang)
    return cache[g]


def _perpCoord(gc, nrm, pt):
    """点 pt 相对 gc 在杆法向 nrm=(nx,ny) 上的垂轴坐标。"""
    return (pt[0] - gc[0]) * nrm[0] + (pt[1] - gc[1]) * nrm[1]


def _supportOffset(regionOf, pose, nrm, k, h):
    """corridorSupport 的带落点版：返回 (最大单片面积, 最优法向位移)，
    位移已换算到杆法向 nrm 的垂轴坐标增量（原 _supportOff 闭包本体）。"""
    reg = regionOf(h)
    m = pose.initMedians[k]
    if reg is None or reg.is_empty or len(m) < 2:
        return 0.0, 0.0
    from shapely.geometry import LineString as _LS2
    from shapely.affinity import translate as _tr2
    cor = _LS2([tuple(p) for p in m]).buffer(pose.wEst * 0.6)
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
    return best, bestOff * (cnx * nrm[0] + cny * nrm[1])


def barOverload(geom, kaiRef, groups, pose, cost):
    """G4 单杆组超载重指派 + 垂直蹲杆放逐。"""
    contours = geom.contours
    kai = kaiRef.kai
    nGroups = groups.nGroups
    initMedians = pose.initMedians
    costRows = cost.costRows
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes
    groupBBoxes = groups.groupBBoxes
    groupCentroids = groups.groupCentroids
    _groupRegion = groups.regionOf
    _support = groups.support

    # 单杆组超载重指派：楷体的封底横在现代设计中常并入外框轮廓（貝/酉
    # 的目底、日底），其名义位置又恰压在腔内悬浮横杆上（代价0）——墨
    # 距离把两条楷体横同判给一根杆，杆内竞争必然一伤（質12/醌7 retain
    # 曾掉到50%且100%重叠）。判据：组轮廓=单外环无孔且高伸长（就是一
    # 根杆），组内≥2笔与杆同向。放逐目标=代价邻近、走廊滑动支撑充分的
    # 组（禁选已含同向笔的单杆组——防杆间跳槽再造超载）；放逐者按**序
    # 保持**选：目标在杆哪一侧，就放逐名义位置偏那一侧最远的笔（封底
    # 横名义恰压杆上、按邻近选留必错——楷体次序是唯一可靠证据）。
    try:
        barCache = {}
        for g in range(nGroups):
            ok, barAng = _isBarGroup(barCache, geom, groups, g)
            if not ok:
                continue
            ss = list(groupStrokes.get(g, []))
            par = [k for k in ss
                   if _parallel(_medianAngle(initMedians, k), barAng)]
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
                    aK = _medianAngle(initMedians, k)
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
                                if c0["group"] == h and c0["isHole"] and \
                                        pointInPolygon(gcS, c0["poly"]):
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
            nrm = (-math.sin(rad), math.cos(rad))

            # 放逐目标：全体笔的候选里支撑最强的组
            bestH, bestS = -1, 400.0
            gcSelf = groupCentroids.get(g)
            for h in range(nGroups):
                if h == g:
                    continue
                hOk, hAng = _isBarGroup(barCache, geom, groups, h)
                if hOk and any(_parallel(_medianAngle(initMedians, k2), hAng)
                               for k2 in groupStrokes.get(h, [])):
                    continue
                # 包含性豁免：候选组带孔且超载杆质心落在其孔腔内——
                # "框包着悬浮杆"是结构铁证（甲早曱野的环底边名义代价
                # 90-150 远超代价窗被拒，恰是真家）
                contained = False
                if gcSelf is not None:
                    for c0 in contours:
                        if c0["group"] == h and c0["isHole"] and \
                                pointInPolygon(gcSelf, c0["poly"]):
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

            # 落点序一致性：只在上下两个极端里挑放逐者——放逐后其实际
            # 落点（走廊最优滑动位置）必须保持楷体给定的上下次序（酉框
            # 质心在杆上方、可容墨的框底在杆下方，按质心猜方向曾放错笔）
            ordered = sorted(par, key=lambda k: _perpCoord(
                gc, nrm, _medianCenter(initMedians, k)))
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
                sup, dPerp = _supportOffset(_groupRegion, pose, nrm,
                                            exile, bestH)
                if sup <= 400.0:
                    continue
                land = _perpCoord(gc, nrm,
                                  _medianCenter(initMedians, exile)) + dPerp
                others = [_perpCoord(gc, nrm, _medianCenter(initMedians, k))
                          for k in par if k != exile]
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


# ============================================================ G5 疑抢杆认领

def _axisChordLen(initMedians, k):
    """笔 k 初始中轴的名义轴长（首末点弦长）。"""
    m = initMedians[k]
    return dist(m[0], m[-1])


def _supportLanding(regionOf, pose, k, h):
    """走廊滑动支撑 + 最优落点位移向量（原 _supLand 闭包本体）。"""
    reg = regionOf(h)
    m = pose.initMedians[k]
    if reg is None or reg.is_empty or len(m) < 2:
        return 0.0, (0.0, 0.0)
    from shapely.geometry import LineString as _L3
    from shapely.affinity import translate as _t3
    cor = _L3([tuple(p) for p in m]).buffer(pose.wEst * 0.6)
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


def barTheftSwap(geom, kaiRef, groups, pose, cost):
    """G5 疑抢杆认领互换（笔比杆长=抢占铁证，成链处置）。"""
    contours = geom.contours
    kai = kaiRef.kai
    nGroups = groups.nGroups
    nStrokes = pose.nStrokes
    initMedians = pose.initMedians
    costRows = cost.costRows
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes
    groupBBoxes = groups.groupBBoxes
    _groupRegion = groups.regionOf

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
            if _axisChordLen(initMedians, k1) <= barLen * 1.08:
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
                sup, offv = _supportLanding(_groupRegion, pose, k1, gU)
                if sup < max(800.0, 0.35 * expArea[k1]):
                    continue
                if bestU is None or sup > bestU[0]:
                    bestU = (sup, gU, offv)
            if bestU is None:
                continue
            _, gU, off1 = bestU
            c1 = _medianCenter(initMedians, k1)
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
                sup2, off2 = _supportLanding(_groupRegion, pose, k2, g0)
                if sup2 < 400.0:
                    continue
                c2p = _medianCenter(initMedians, k2)
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


# ============================================================ G6 轴向错家

def _kaiChordAxis(kaiMedians, k):
    """笔 k 楷体中轴的弦向角（弦长<40 视为无向，返回 None）。"""
    m2 = kaiMedians[k]
    dx = m2[-1][0] - m2[0][0]
    dy = m2[-1][1] - m2[0][1]
    if math.hypot(dx, dy) < 40:
        return None
    return math.degrees(math.atan2(dy, dx)) % 180.0


def _permAxisCost(axK, misG, perm):
    """错家名单重排 perm 的总轴向偏差（任一步 >40° 直接判不可行）。"""
    tot = 0.0
    for a, gi2 in enumerate(perm):
        dev = _fold90(axK[a], misG[gi2][1])
        if dev > 40.0:
            return None
        tot += dev
    return tot


def axisMisplace(geom, kaiRef, groups):
    """G6 轴向错家重排（并挂 groups.barAxisOf 供 G8.5 复用）。"""
    kai = kaiRef.kai
    nGroups = groups.nGroups
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes

    # 轴向错家重排：横竖笔直出一根与其楷体轴向**垂直**的杆=铁证错家
    # （博6横直出竖杆、11竖撇直出横杆、8竖占斜片——三笔连环错位，
    # 同型互换救不了跨型链）。收集"单笔直出组×杆轴向与笔楷体弦向
    # 偏差>50°"的错家名单，名单内全排列重指派（轴向匹配代价最小），
    # 总偏差严格下降才施行。名单内排列=家数守恒，不会造成超载/空组。
    _barAxisOf = functools.partial(barAxisOfGroup, geom, groups)

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
        kAx = _kaiChordAxis(kai["medians"], k)
        if bAx is None or kAx is None:
            continue
        if _fold90(bAx, kAx) > 50.0:
            misK.append(k)
            misG.append((g, bAx))
    if 2 <= len(misK) <= 5:
        axK = [_kaiChordAxis(kai["medians"], k) for k in misK]

        cur = sum(_fold90(axK[a], misG[a][1]) for a in range(len(misK)))
        best = None
        for perm in itertools.permutations(range(len(misG))):
            c = _permAxisCost(axK, misG, perm)
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

    groups.barAxisOf = _barAxisOf


# ============================================================ G7 槽位互换

def _slotOf(kaiMatches, k):
    """笔 k 的一级槽位（matches 路径首元素，无则 None）。"""
    p = kaiMatches[k] if k < len(kaiMatches) else None
    return p[0] if p else None


def _slotGroupCent(groups, k):
    """笔 k 当前指派组的墨质心。"""
    return groups.groupCentroids.get(groups.strokeGroup[k])


def _slotCenter(groups, slotMembers, s0, excl):
    """槽位 s0 的中心 = 成员所在组墨质心的中位数（排除 excl 防自身污染；
    有效成员 <2 时不可用，返回 None）。"""
    pts2 = []
    for k in slotMembers[s0]:
        if k == excl:
            continue
        c = _slotGroupCent(groups, k)
        if c is not None:
            pts2.append(c)
    if len(pts2) < 2:
        return None
    xs = sorted(p[0] for p in pts2)
    ys = sorted(p[1] for p in pts2)
    return (xs[len(xs) // 2], ys[len(ys) // 2])


def slotSwap(kaiRef, groups, pose, cost):
    """G7 槽位互换仲裁（槽位错位是最硬的换家证据）；返回 slotSwaps，
    并挂 kaiRef.kaiMatches0/slotMembers 供 G8.5 复用。"""
    kai = kaiRef.kai
    nStrokes = pose.nStrokes
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes
    costRows = cost.costRows

    # G7 槽位互换仲裁（种子字统计：92.2% 部件笔数与种子精确一致，
    # 槽位错位是比轴向/次序更硬的换家证据）：各一级槽位中心 = 该槽
    # 成员所在组墨质心的中位数（排除自身防污染）；互为错位的跨槽笔对
    # （各自到对方槽中心的距离×1.8 仍小于到本槽中心）且互换组的墨
    # 距离总代价不明显恶化(≤+60)则互换家。跨型允许——博的横占竖杆
    # 类连环错位常跨槽跨型。
    slotSwaps = []
    kaiMatches0 = kai.get("matches") or []

    slotMembers = {}
    for k in range(nStrokes):
        s0 = _slotOf(kaiMatches0, k)
        if s0 is not None:
            slotMembers.setdefault(s0, []).append(k)
    if len(slotMembers) >= 2:
        for _round in range(2):
            movedG7 = False
            for i in range(nStrokes):
                si = _slotOf(kaiMatches0, i)
                ci = _slotGroupCent(groups, i)
                if si is None or ci is None:
                    continue
                own = _slotCenter(groups, slotMembers, si, i)
                if own is None:
                    continue
                dOwn = dist(ci, own)
                for j in range(nStrokes):
                    if j == i:
                        continue
                    sj = _slotOf(kaiMatches0, j)
                    if sj is None or sj == si:
                        continue
                    cj = _slotGroupCent(groups, j)
                    if cj is None:
                        continue
                    ownJ = _slotCenter(groups, slotMembers, sj, j)
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

    kaiRef.kaiMatches0 = kaiMatches0
    kaiRef.slotMembers = slotMembers
    return slotSwaps


# ============================================================ G8 序保持互换

def orderPreserve(kaiRef, groups, pose, cost):
    """G8 序保持互换仲裁（楷体次序约束补进组指派）；并挂 kaiRef.kaiCentAll。"""
    kai = kaiRef.kai
    nStrokes = pose.nStrokes
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes
    groupCentroids = groups.groupCentroids
    costRows = cost.costRows

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

    kaiRef.kaiCentAll = kaiCentAll


# ============================================================ G8.5 梯队探针

def _nomXRange(cache, affine, kaiMedians, k):
    """笔 k 的楷体名义 x 走廊（affine 映射后的中轴 x 范围，cache 记忆化）。"""
    if k not in cache:
        xs = [affine(p)[0] for p in kaiMedians[k]]
        cache[k] = (min(xs), max(xs))
    return cache[k]


def _xOverlapRatio(nomXR, k, bar):
    """笔 k 名义 x 走廊与条组 x 范围的重叠占条宽比例。"""
    lo, hi = nomXR(k)
    return ((min(hi, bar[4]) - max(lo, bar[3]))
            / max(1.0, bar[4] - bar[3]))


def ladderProbeStage(kaiRef, groups, pose):
    """G8.5 梯队探针（A/B/C/D 型病字签名，零副作用）；返回 ladderProbe。"""
    _pl = _pkg()
    kai = kaiRef.kai
    nStrokes = pose.nStrokes
    seedMedians = pose.seedMedians
    kaiMatches0 = kaiRef.kaiMatches0
    slotMembers = kaiRef.slotMembers
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes
    groupBBoxes = groups.groupBBoxes
    groupCentroids = groups.groupCentroids
    affine = kaiRef.affine
    kaiCentAll = kaiRef.kaiCentAll
    _barAxisOf = groups.barAxisOf

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
    if (_pl.LADDER_PROBE or (_pl.LADDER_ACT and seedMedians is None)) and \
            nStrokes >= 4 and kaiMatches0:
        _dbgLP = (_pl.LADDER_PROBE == 2)    # 标定调试：dump 全部件（含未触发）

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

        _nomXR = functools.partial(_nomXRange, {}, affine, kai["medians"])

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
            tOv = _xOverlapRatio(_nomXR, bar[1], bar)
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
                    if any(b[1] == r and _xOverlapRatio(_nomXR, r, b) >= 0.25
                           for b in allBars):
                        continue
                    rOv = _xOverlapRatio(_nomXR, r, bar)
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

    return ladderProbe


# ============================================================ G8.5 执行器

def _snapGroup(savGS, groupStrokes, g):
    """组成员表快照（首次触及才存，供 all-or-nothing 回滚）。"""
    if g not in savGS:
        savGS[g] = list(groupStrokes.get(g, []))


def _moveStroke(savGS, groups, k, gTo):
    """笔 k 迁往组 gTo（迁出/迁入组先快照，成员表保持有序）。"""
    gFrom = groups.strokeGroup[k]
    _snapGroup(savGS, groups.groupStrokes, gFrom)
    _snapGroup(savGS, groups.groupStrokes, gTo)
    if k in groups.groupStrokes.get(gFrom, []):
        groups.groupStrokes[gFrom].remove(k)
    groups.strokeGroup[k] = gTo
    groups.groupStrokes.setdefault(gTo, []).append(k)
    groups.groupStrokes[gTo].sort()


def ladderActStage(kaiRef, groups, pose, cost, diag):
    """G8.5 梯队执行器（all-or-nothing 组重排+中轴带重置）；读
    diag.ladderProbe，写 diag.ladderRealign/ladderTouched。"""
    _pl = _pkg()
    kai = kaiRef.kai
    nGroups = groups.nGroups
    seedMedians = pose.seedMedians
    ladderProbe = diag.ladderProbe
    strokeGroup = groups.strokeGroup
    groupStrokes = groups.groupStrokes
    medians = pose.medians
    initMedians = pose.initMedians
    affine = kaiRef.affine
    strokeGroupCost = cost.strokeGroupCost
    penMatrix = cost.penMatrix

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
    if _pl.LADDER_ACT and seedMedians is None and ladderProbe:
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
                _moveStroke(savGS, groups, k, gNew)
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
                    _moveStroke(savGS, groups, o, dest)
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

    diag.ladderRealign = ladderRealign
    diag.ladderTouched = ladderTouched
