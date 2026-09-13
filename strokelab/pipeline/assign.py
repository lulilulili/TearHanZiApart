# -*- coding: utf-8 -*-
"""strokelab.pipeline.assign — 组指派：G0 墨距离代价 + penMatrix 相容性罚
+ G1 匈牙利锚定 + 就近入组 + S1b 空组语义认领。

原有算法注释（诊断元修复结论、事故字例：爺k11/螟k2/騫k11/聞、邸酞濮蜷、
㡭、Noto 竖折等）逐条随代码保留。strokeGroupCostRow 为模块级函数，
cost.strokeGroupCost 以 functools.partial 绑定 groups/pose 视图后供
G8.5 执行器复用（重置后代价评估）。
"""

import functools
import math
import sys

from ..classify import semanticSegments
from ..geometry import (bboxOfPoints, contourToPath, dist, pointInPolygon,
                        nearestOnPolyline, resamplePolyline, shapeDescriptor)
from .helpers import _hungarian


def _traceOn():
    """统一决策迹开关（运行期读包属性，赋值即生效——LADDER_* 同语义）。"""
    _pl = sys.modules["strokelab.pipeline"]
    return bool(getattr(_pl, "TRACE_ON", False) or _pl.LADDER_PROBE)


def strokeGroupCostRow(groups, pose, k):
    """笔画→各组的"墨距离"（墨内=0，否则到组边界最近距离）均值向量。
    inside 占比法对 ⊓ 形带状轮廓失效（鸿蒙"日"的竖中轴悬在空腔里），
    距离法对带状/实心都稳。性能：远组用包围盒距离下界代替精算（远组
    只在匈牙利被迫指派时才可能选中，下界不影响近组排序）；近组的
    边界距离用粗采样折线。"""
    rm = resamplePolyline([tuple(p) for p in pose.initMedians[k]], 20)
    mb = bboxOfPoints(rm)
    row = []
    for g in range(groups.nGroups):
        bb = groups.groupBBoxes[g]
        if bb is None:
            row.append(1e18)
            continue
        gapX = max(0.0, max(mb.x0, bb.x0) - min(mb.x1, bb.x1))
        gapY = max(0.0, max(mb.y0, bb.y0) - min(mb.y1, bb.y1))
        gap = math.hypot(gapX, gapY)
        if gap > 60.0:
            row.append(gap)
            continue
        outers = groups.groupOuters[g]
        holes = groups.groupHoles[g]
        total = 0.0
        for p in rm:
            if any(pointInPolygon(p, poly) for poly in outers) and \
               not any(pointInPolygon(p, hp) for hp in holes):
                d = 0.0
            else:
                d = min(nearestOnPolyline(p, poly)["d"]
                        for poly in groups.groupCoarse[g])
            total += d
        row.append(total / (len(rm) or 1))
    return row


def _splitSections(m, angDeg=40.0):
    """中轴线按拐角（相邻段夹角超 angDeg）切分为语义分段。"""
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


def _semanticClaim(kai, groups, pose, costRows, g):
    """空组 g 的语义认领：按墨轴向找构词里含匹配单元的复合笔，取其 D
    对应分段走廊滑动支撑最强者。返回认领的笔序（子笔身份），无候选/
    异常返回 None（由调用方退回全开竞争兜底）。"""
    bb = groups.groupBBoxes.get(g)
    if bb is None or (bb.w < 6 and bb.h < 6):
        return None
    gAxis = "h" if bb.w > bb.h * 1.5 else ("v" if bb.h > bb.w * 1.5 else "d")
    unitAxis = {"横": "h", "提": "h", "竖": "v"}
    try:
        from shapely.geometry import Polygon as _P1, LineString as _L1
        from shapely.affinity import translate as _t1
        reg = None
        for poly in groups.groupOuters[g]:
            pg = _P1(poly)
            if not pg.is_valid:
                pg = pg.buffer(0)
            reg = pg if reg is None else reg.union(pg)
        if reg is None:
            return None
        for poly in groups.groupHoles[g]:
            pg = _P1(poly)
            if not pg.is_valid:
                pg = pg.buffer(0)
            reg = reg.difference(pg)
        if reg.is_empty:
            return None
        best = None
        for k in range(pose.nStrokes):
            units = semanticSegments(kai["strokeTypes"][k])
            if len(units) < 2 or costRows[k][g] > 220.0:
                continue
            if gAxis != "d" and not any(
                    unitAxis.get(u, "d") in (gAxis, "d") for u in units):
                continue
            for sec in _splitSections(pose.medians[k]):
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
            return None
        return best[1]
    except Exception:
        return None


def run(geom, kaiRef, groups, pose, cost):
    """产出 cost.strokeGroupCost/costRows/penMatrix +
    groups.strokeGroup/groupStrokes；返回 (semanticClaims, trace)
    （诊断面：S1b 认领记录 + G1/S1b 决策迹）。"""
    kai = kaiRef.kai
    contours = geom.contours
    nStrokes = pose.nStrokes
    nGroups = groups.nGroups
    groupBBoxes = groups.groupBBoxes
    initMedians = pose.initMedians
    trace = []
    tOn = _traceOn()

    # 全局最优指派：每组先由匈牙利算法配一个"锚定笔"（保证无空组——逐笔
    # 独立 argmin 曾让㡭的点挤进邻笔的组、正主组空置沦为全开竞争），
    # 余下笔画再就近入组。组数=笔画数时退化为严格一一对应
    costRows = [strokeGroupCostRow(groups, pose, k) for k in range(nStrokes)]
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
                        if diag0 >= 3.5 * max(20.0, chord0) and \
                                _gInk.get(g, 0.0) / _totInk >= 0.07:
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
        cost2 = []
        for k in range(nStrokes):
            free = min(costRows[k]) if nGroups else 0.0
            # 相容性罚只作用于锚定矩阵（argmin 就近入组与各仲裁窗口仍用
            # 纯几何代价）——罚进 costRows 本体曾把爱5竖的回家路也堵死
            pk = penMatrix.get(k, {})
            cost2.append([costRows[k][g] + pk.get(g, 0.0)
                          for g in range(nGroups)] +
                         [free] * (size - nGroups))
        match = _hungarian(cost2)
        for k in range(nStrokes):
            if match[k] < nGroups:
                # 决策迹：匈牙利锚定与逐笔 argmin 不同 = G1 强制改判
                # （锚定恒施行——匈牙利解就是终判，无拒绝分支）
                if tOn and match[k] != strokeGroup[k]:
                    trace.append({
                        "level": "G1", "stroke": k,
                        "from": strokeGroup[k], "to": match[k],
                        "adopted": True,
                        "evidence": {
                            "cArgmin": round(costRows[k][strokeGroup[k]], 1),
                            "cTo": round(costRows[k][match[k]], 1),
                            "pen": penMatrix.get(k, {}).get(match[k], 0.0)}})
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
    for g in range(nGroups):
        if not groupStrokes[g]:  # 无笔画映射到该组
            claimed = _semanticClaim(kai, groups, pose, costRows, g) \
                if nGroups > nStrokes else None
            if claimed is not None:
                groupStrokes[g] = [claimed]
                semanticClaims.append({"group": g, "stroke": claimed})
                if tOn:
                    trace.append({
                        "level": "S1b", "stroke": claimed, "to": g,
                        "action": "claim", "adopted": True,
                        "evidence": {"cost": round(costRows[claimed][g], 1)}})
            else:
                groupStrokes[g] = list(range(nStrokes))  # 兜底：放开限制
                if tOn:
                    # 空组无人认领退全开竞争 = S1b 触发但改判被拒
                    trace.append({
                        "level": "S1b", "group": g,
                        "action": "fallbackOpen", "adopted": False,
                        "evidence": {"nGroups": nGroups,
                                     "nStrokes": nStrokes}})
    cost.strokeGroupCost = functools.partial(strokeGroupCostRow, groups, pose)
    cost.costRows = costRows
    cost.penMatrix = penMatrix
    groups.strokeGroup = strokeGroup
    groups.groupStrokes = groupStrokes
    return semanticClaims, trace
