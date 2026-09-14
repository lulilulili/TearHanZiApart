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


# 杆容量罚标定参数（矩阵归并2b·设计§2b 标定阶梯"先提门槛再降罚幅"；
# 标定过程与各候选点实测数据见 docs/矩阵归并设计.md 实施记录·2b）：
# RATIO=超长判据门槛（G5 实码 1.08 为判据面原点）；PEN=罚幅（推导：
# 须覆盖"名义压杆代价≈0 vs 真主组代价可达 G5 认领窗 140"的差值）；
# WIN=罚施加的代价窗（"现住户"概念的锚定期翻译——G5 触发主体是已
# 入组的现住户，无窗版把罚铺满全部远距(笔,组)对，sample958 实测
# 54 对/字，匈牙利被迫指派下纯连锁挤位破坏健康字）。
BARCAP_RATIO = 1.15
BARCAP_PEN = 120.0
BARCAP_WIN = 1e18


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


def _penShiftTrace(costRows, penTags, match, nGroups):
    """罚作用面审计仪（矩阵归并·对抗评审裁定#1，仅 TRACE_ON 跑）：
    用**无罚**代价矩阵再解一次匈牙利，与带罚解锚定不同的笔各记一条
    penShift 迹。背景：罚的作用形态是"把笔推离被罚组"，成功时目的组
    必然无罚，只看采纳条目的罚字段永远 0 命中（评审实证 1207 条）——
    带罚/无罚双解的差集才是罚作用面的真实清单。只读不改判（零行为，
    penShift 条目不参与任何判定）；匈牙利 O(n³) 但 n=笔数，实测双解
    增量成本可忽略。列语义与带罚矩阵同构：前 nGroups 列=真实组，
    其余自由列（代价=argmin，锚定→None）。"""
    nStrokes = len(costRows)
    noPenCost = []
    for k in range(nStrokes):
        free = min(costRows[k]) if nGroups else 0.0
        noPenCost.append([costRows[k][g] for g in range(nGroups)] +
                         [free] * (nStrokes - nGroups))
    matchNoPen = _hungarian(noPenCost)
    entries = []
    for k in range(nStrokes):
        withPen = match[k] if match[k] < nGroups else None
        noPen = matchNoPen[k] if matchNoPen[k] < nGroups else None
        if withPen == noPen:
            continue
        # penFrom=无罚解归宿组上的罚（把笔推走的那笔账；空 dict=本笔
        # 无罚、被他笔的罚连锁挤位）,penTo=带罚解归宿组上的残余罚
        tags = penTags.get(k, {})
        entries.append({
            "level": "G1", "action": "penShift", "stroke": k,
            "withPen": withPen, "noPen": noPen, "adopted": True,
            "evidence": {
                "penFrom": {} if noPen is None else tags.get(noPen, {}),
                "penTo": {} if withPen is None else tags.get(withPen, {})}})
    return entries


def run(geom, kaiRef, groups, pose, cost):
    """产出 cost.strokeGroupCost/costRows/penMatrix/penTags +
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
    # ③杆容量罚 +200（矩阵归并2b，PEN_BARCAP 默认关）：若 k 锚进 g，
    #   k 名义弦长 > 1.08× g 的 bbox 沿笔轴投影 barLen=|w·ux|+|h·uy|
    #   （与 G5 barTheftSwap/_axisChordLen 同公式）——"杆装不下它、是
    #   抢来的"。判据=G5 现行触发判据的**锚定期版本**，照抄实码不加它
    #   没有的形状门/笔型门（对抗评审裁定#3：带 _barAx 杆形门+笔型门
    #   对 G5 触发对命中率仅 44-46%）。**"现住户"概念的锚定期翻译含
    #   代价窗 ≤140**（G5 认领窗上限，罚幅 200 的覆盖设计参照同一常数）：
    #   G5 触发主体是已入组的现住户（代价≈0），无窗版把 200 罚铺满全部
    #   远距(笔,组)对（sample958 实测 54 对/字），匈牙利被迫指派下纯
    #   连锁挤位——标定实测 21 个回退字里 14 个无任何 barcap 直接命中，
    #   全是连锁位移；加窗后罚面只剩"确实可能锚进去"的对。威 族病理：
    #   全局仿射把楷体顶横名义压在戌内短横杆上代价≈0 直出抢杆——罚后
    #   匈牙利把杆锚给装得下的同向笔，"过长笔单独直出杆"在锚定层消解
    #   为多成员竞争组；G5 层保留兜漏网（门扩展见 arbitrate.
    #   barTheftSwap）。仅名义遍施行（seedMedians is None，同 G8.5
    #   执行器门，理由见包开关注释）。
    penMatrix = {}
    penTags = {}
    barcapOn = bool(getattr(sys.modules["strokelab.pipeline"],
                            "PEN_BARCAP", False)) and \
        pose.seedMedians is None
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
            # 杆容量罚的笔轴单位向量（无笔型/弦长门——G5 实码对任意笔
            # 评 chord>1.08×barLen；barLen 取 |·| 后与 %180 无关）
            uxCap = uyCap = None
            if barcapOn and chord0 > 0:
                radCap = math.atan2(m0k[-1][1] - m0k[0][1],
                                    m0k[-1][0] - m0k[0][0])
                uxCap, uyCap = math.cos(radCap), math.sin(radCap)
            for g in range(nGroups):
                pens = {}
                ax0 = _barAx.get(g)
                if ax0 is not None and kAng0 is not None:
                    dv0 = abs(ax0 - kAng0) % 180.0
                    if min(dv0, 180.0 - dv0) > 50.0:
                        pens["barPerp"] = 200.0
                if t0 == "点":
                    bb = groupBBoxes.get(g)
                    if bb is not None and _totInk > 0:
                        diag0 = math.hypot(bb.w, bb.h)
                        if diag0 >= 3.5 * max(20.0, chord0) and \
                                _gInk.get(g, 0.0) / _totInk >= 0.07:
                            pens["dotAnchor"] = 250.0
                if uxCap is not None:
                    bb = groupBBoxes.get(g)
                    if bb is not None and costRows[k][g] <= BARCAP_WIN and \
                            chord0 > BARCAP_RATIO * (
                                abs(bb.w * uxCap) + abs(bb.h * uyCap)):
                        pens["barcap"] = BARCAP_PEN
                if pens:
                    # 罚种分道记账（矩阵归并·对抗评审裁定#1）：penTags
                    # 记 {罚种:罚值} 明细，penMatrix 保持"各罚种之和"的
                    # 标量——匈牙利锚定读标量；G8.5 执行器否决门改读
                    # penTags 分量（barcap 分道豁免，裁定#7，见
                    # arbitrate.ladderActStage）。求和顺序=罚种登记
                    # 顺序，数值与原累加逐位同。
                    penTags.setdefault(k, {})[g] = pens
                    penMatrix.setdefault(k, {})[g] = sum(pens.values())
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
        if tOn and penTags:
            trace.extend(_penShiftTrace(costRows, penTags, match, nGroups))
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
                            # 评审实证:旧 pen 字段只记目的组罚,罚成功时
                            # 目的组必然无罚(1207条0命中)。改为按罚种双
                            # 记:penFrom=argmin组(被罚离开的家)的罚,
                            # penTo=匈牙利目的组的罚
                            "penFrom": penTags.get(k, {}).get(
                                strokeGroup[k], {}),
                            "penTo": penTags.get(k, {}).get(match[k], {})}})
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
    cost.penTags = penTags
    groups.strokeGroup = strokeGroup
    groups.groupStrokes = groupStrokes
    return semanticClaims, trace
