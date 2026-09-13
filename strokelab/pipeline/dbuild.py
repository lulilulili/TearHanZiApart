# -*- coding: utf-8 -*-
"""strokelab.pipeline.dbuild — 全局对齐 + D 构建。

全局对齐：楷体整字包围盒→目标整字包围盒的轴对齐仿射（ctx.affine）。
D 构建：每笔初始模板中轴线 = B 库同类型骨架按楷体该笔包围盒定位，
候选逐笔过模板-结构一致性检查（本类型优先/借用从严），缺类型退回
楷体中轴线；分段吸直保证同类型 D 拓扑一致。自洽回灌时种子直接顶替。
原有算法注释（含阈值来历与事故字例）逐条随代码保留。
"""

import functools
import math

from ..classify import PROBE_TABLE, similarTypes
from ..geometry import (bboxOfPoints, flattenSegs, parseContours,
                        polylineLength, straightenSections)
from .helpers import _medianDeviation


def _affinePoint(kb, tb, sx, sy, p):
    """楷体坐标→目标坐标的轴对齐仿射（运行期桥本体；ctx 上以
    functools.partial 绑定 kb/tb/sx/sy 后即原 affine(p) 闭包）。"""
    return (tb.x0 + (p[0] - kb.x0) * sx, tb.y0 + (p[1] - kb.y0) * sy)


def run(ctx):
    """产出 ctx.affine/kaiPerims/kaiStrokeBBoxes/kb/tb + ctx.medians/
    initMedians/templateSources/templateEnts/nStrokes,并打首段计时。"""
    dataHub = ctx.dataHub
    fontEntry = ctx.fontEntry
    kai = ctx.kai
    contours = ctx.contours
    seedMedians = ctx.seedMedians
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
    affine = functools.partial(_affinePoint, kb, tb, sx, sy)

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
    ctx.kaiPerims = kaiPerims
    ctx.kaiStrokeBBoxes = kaiStrokeBBoxes
    ctx.kb = kb
    ctx.tb = tb
    ctx.affine = affine
    ctx.medians = medians
    ctx.initMedians = initMedians
    ctx.templateSources = templateSources
    ctx.templateEnts = templateEnts
    ctx.nStrokes = nStrokes
    ctx.tick("解析对齐/D构建")
