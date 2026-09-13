# -*- coding: utf-8 -*-
"""strokelab.pipeline.anchor — G9 组局部重锚定 + G10 D 断面吸附。

G9：失配门控下用楷体相对布局对组内名义位整体仿射重映射（覆盖<0.8
才救，梯队执行器动过的组豁免）。G10：对走廊支撑贫瘠的横/竖笔沿法向
细扫最大单片支撑位吸附（同向占位/楷序倒置不吸）。事故史注释随代码
保留（磷·石口、吃/口底横族、甲、肝左竖、钾等）。
"""

import math

from ..geometry import bboxOfPoints


def groupRemap(ctx):
    """G9 组局部重锚定（失配门控的组内相对布局复位）。"""
    contours = ctx.geom.contours
    nGroups = ctx.groups.nGroups
    medians = ctx.pose.medians
    initMedians = ctx.pose.initMedians
    groupStrokes = ctx.groups.groupStrokes
    groupBBoxes = ctx.groups.groupBBoxes
    ladderTouched = ctx.diag.ladderTouched

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

    ctx.diag.groupRemapInfo = groupRemapInfo


def sectionSnap(ctx):
    """G10 D 断面吸附（组内法向滑动预对位）。"""
    kai = ctx.kaiRef.kai
    nGroups = ctx.groups.nGroups
    medians = ctx.pose.medians
    initMedians = ctx.pose.initMedians
    groupStrokes = ctx.groups.groupStrokes
    groupBBoxes = ctx.groups.groupBBoxes
    groupOuters = ctx.groups.groupOuters
    groupHoles = ctx.groups.groupHoles

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


def run(ctx):
    """G9 -> G10 定序执行（G10 依赖 G9 复位后的中轴位）。"""
    groupRemap(ctx)
    sectionSnap(ctx)
