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

from ..geometry import (dist, lineSeg, cubicSeg, parseContours, contourToPath,
                       nearestBatch,
                       flattenSegs,
                       bboxOfPoints, pointInPolygon, nearestOnPolyline,
                       polylineLength, resamplePolyline, analyzeContours,
                       bezPoint, bezTangent, bezSlice, segLength,
                       shapeDescriptor, shapeSimilarity, refineMedianFit,
                       recenterMedian, corridorPoint,
                       outlineCenterline, midpointRectify,
                       straightenSections, signedArea)
from ..classify import (findLibEntry, similarTypes, PROBE_TABLE,
                       semanticSegments, matchTier)
from .. import boolean as booleanClamp

ITERS = 5

# G8.5 梯队探针开关：开启时 result 附带 ladderProbe 信号（标定用），
# 探测本身无副作用。执行器待探测精度在 25 字族+健康集上标定后接入。
LADDER_PROBE = False
# G8.5 梯队执行器开关：探测标定达标（家族14/25、健康0/23、抽样0/200）
# 后接入。按 fired 部件的 plan 施行组重排+中轴带重置。
LADDER_ACT = True

from .helpers import (_medianDeviation, _hungarian, _switchbackCount,
                      _axisFails, _reMedianFromStroke, _selfSeeds,
                      _meanOf, _strokeCenter, _secondPassBetter)
from .state import PipelineCtx
from . import grouping
from . import dbuild
from . import assign
from . import arbitrate
from . import anchor
from . import iterate
from . import cutting


def runPipeline(dataHub, fontEntry, ch, applyBooleanClamp=True,
                seedMedians=None, selfConsistent=True):
    kai = dataHub.kai(ch)
    if not kai:
        return {"error": "MakeMeAHanzi 中没有「%s」的笔画数据" % ch}
    raw = fontEntry.glyphContours(ch)
    if not raw:
        return {"error": "字体 %s 中没有「%s」字形" % (fontEntry.key, ch)}

    ctx = PipelineCtx(dataHub=dataHub, fontEntry=fontEntry, ch=ch,
                      applyBooleanClamp=applyBooleanClamp,
                      seedMedians=seedMedians,
                      selfConsistent=selfConsistent, kai=kai, raw=raw)

    grouping.parseAndMerge(ctx)
    contours = ctx.contours

    # 计时器入 ctx（PipelineCtx.tick 与原 _tick 闭包逐字节同款：毫秒取整）
    ctx.startTimer()
    _timings = ctx.timings
    _tick = ctx.tick

    dbuild.run(ctx)
    kaiStrokeBBoxes = ctx.kaiStrokeBBoxes
    tb = ctx.tb
    affine = ctx.affine
    medians = ctx.medians
    initMedians = ctx.initMedians
    templateSources = ctx.templateSources
    templateEnts = ctx.templateEnts
    nStrokes = ctx.nStrokes


    grouping.buildTables(ctx)
    nGroups = ctx.nGroups
    groupOuters = ctx.groupOuters
    groupHoles = ctx.groupHoles
    groupCentroids = ctx.groupCentroids
    groupBBoxes = ctx.groupBBoxes
    groupCoarse = ctx.groupCoarse


    assign.run(ctx)
    strokeGroupCost = ctx.strokeGroupCost
    costRows = ctx.costRows
    penMatrix = ctx.penMatrix
    strokeGroup = ctx.strokeGroup
    groupStrokes = ctx.groupStrokes
    semanticClaims = ctx.semanticClaims

    arbitrate.run(ctx)
    slotSwaps = ctx.slotSwaps
    kaiMatches0 = ctx.kaiMatches0
    ladderProbe = ctx.ladderProbe
    ladderRealign = ctx.ladderRealign
    ladderTouched = ctx.ladderTouched

    anchor.run(ctx)
    groupRemapInfo = ctx.groupRemapInfo


    iterate.run(ctx)
    contourAllowed = ctx.contourAllowed
    w0 = ctx.w0
    widths = ctx.widths
    sampleSets = ctx.sampleSets
    scoreOf = ctx.scoreOf
    labelOf = ctx.labelOf
    templatePaths = ctx.templatePaths


    cutting.run(ctx)
    cutPoints = ctx.cutPoints
    strokes = ctx.strokes


    # ------------------------------------------------------------ 布尔收口 + 校验
    # 饿死救济先行：切割中颗粒无收/零宽退化环的笔（宾的宀左点曾只得
    # 一段边界线），用骨架走廊∩本组区域补一个实体，再进收口。
    # 饿死救济：走廊在 rescueStarved 内用组局部仿射从楷体中轴线构造
    # （全局仿射/精调种子都会歪，见函数注释）
    _glyph = booleanClamp.glyphRegion(contours)  # 只算一次，收口各环节复用
    # 空洞识别（并集后内环=真实围合空腔；面积>400 滤掉笔画间缝隙噪声）
    _holeBoxes = []
    try:
        _geoms = list(_glyph.geoms) if hasattr(_glyph, "geoms") else [_glyph]
        for _pg in _geoms:
            for _ring in _pg.interiors:
                _xs = [p[0] for p in _ring.coords]
                _ys = [p[1] for p in _ring.coords]
                if (max(_xs) - min(_xs)) * (max(_ys) - min(_ys)) > 400.0:
                    _holeBoxes.append([round(min(_xs)), round(min(_ys)),
                                       round(max(_xs)), round(max(_ys))])
    except Exception:
        pass
    _holeCount = len(_holeBoxes)
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
        # 空洞字标记（拆字时顺带识别，供后期在空洞内叠加独立元素）。
        # 注意不能用轮廓 isHole：鸿蒙等字体的框是两个 L 形件搭接而成，
        # 轮廓层面无孔，"孔"是布尔并集后才涌现的——取 _glyph 的内环。
        "holeCount": _holeCount,
        "holes": _holeBoxes,
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
                "components": kai.get("components", []),
                "etymology": kai.get("etymology"),
                "pinyin": kai.get("pinyin", []),
                "definition": kai.get("definition", "")},
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

