# -*- coding: utf-8 -*-
"""strokelab.pipeline.grouping — 轮廓解析 + 交叠件并组 + 连通组组表。

parseAndMerge：解析目标字形轮廓并做交叠件并组（门控：组数>笔画数）。
buildTables：D 构建后按连通组建立分治所需的组表（外环/孔洞/质心/
包围盒/粗采样折线）。原有算法注释（事故史档案）逐条随代码保留。
"""

from ..geometry import analyzeContours, bboxOfPoints, resamplePolyline


def _ufFind(parent, x):
    """并查集查根（路径减半压缩），parent 就地更新。"""
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def parseAndMerge(ctx):
    """轮廓解析+交叠并组：产出 ctx.geom.contours（含 poly/area/isHole/group）。"""
    kai = ctx.kaiRef.kai
    raw = ctx.raw
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
                            ra, rb = _ufFind(parent, a), _ufFind(parent, b)
                            if ra != rb:
                                parent[rb] = ra
                    except Exception:
                        pass
            remap = {}
            for g in range(nG0):
                r = _ufFind(parent, g)
                if r not in remap:
                    remap[r] = len(remap)
            for c in contours:
                c["group"] = remap[_ufFind(parent, c["group"])]
        except Exception:
            pass
    ctx.geom.contours = contours


def buildTables(ctx):
    """连通组组表：产出 ctx.groups 的 nGroups/groupOuters/groupHoles/
    groupCentroids/groupBBoxes/groupCoarse。"""
    contours = ctx.geom.contours
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
    ctx.groups.nGroups = nGroups
    ctx.groups.groupOuters = groupOuters
    ctx.groups.groupHoles = groupHoles
    ctx.groups.groupCentroids = groupCentroids
    ctx.groups.groupBBoxes = groupBBoxes
    ctx.groups.groupCoarse = groupCoarse
