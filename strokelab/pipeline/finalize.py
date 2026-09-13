# -*- coding: utf-8 -*-
"""strokelab.pipeline.finalize — 布尔收口 + 终态中轴重提 + result 组装
+ 自洽二遍 + 轴向守卫。

收口链（救济→裁剪→减除→连通→补缝→强制收口）保证笔画并集与原字形
恒等；终态中轴重提只影响 median 陈述与回灌种子，不回改切割；自洽
二遍与轴向守卫经 sys.modules 调 runPipeline 当前值（audit 工具的
monkeypatch 语义与原单文件版一致），LADDER_PROBE/_axisFails 同理
运行期取包属性。重试遍经 FrontReuse 复用首遍前段产物（解析并组/
全局对齐/整字区域），见类注释。全部事故史注释（流江水、爱冖白缝
等）随代码保留。
"""

import math
import sys
from dataclasses import dataclass

from ..geometry import (contourToPath, dist, flattenSegs, midpointRectify,
                        outlineCenterline, parseContours, resamplePolyline,
                        shapeDescriptor, shapeSimilarity, straightenSections)
from .. import boolean as booleanClamp
from .helpers import (_meanOf, _reMedianFromStroke, _secondPassBetter,
                      _selfSeeds, _switchbackCount)


def _pkg():
    """包模块本体：runPipeline/_axisFails/LADDER_PROBE 取运行期当前值。"""
    return sys.modules["strokelab.pipeline"]


@dataclass
class FrontReuse:
    """跨遍复用的前段产物（自洽二遍/轴向守卫重试遍免复算）。

    收录判据 = 与 seedMedians 无关且确定性重算恒同：轮廓解析+交叠并组
    （只依赖字形 raw）、全局对齐派生量（kb/tb/affine/kaiPerims/
    kaiStrokeBBoxes，只依赖楷体 C 与字形包围盒）、收口用整字区域
    glyph 及其空腔（只依赖 contours）。重试遍原本整段重算这些，且
    dbuild 的 B 候选扫描结果在种子路径会被整批顶替丢弃——复用直接
    跳过 parseAndMerge+dbuild.run（见 runPipeline 的复用分支）。

    contours 不能共享 dict 本体：下游会原地改写（cutting/几何层挂
    _edgeArr 缓存字段等）——照 geometry.parseContours 记忆化先例存
    不可变元组中间层，重试遍浅重构全新 dict（值等价，parity 硬门为
    证）。tb/kb/affine/kaiPerims/kaiStrokeBBoxes 全程只读直接共享；
    glyph 为 shapely 2.x 不可变几何，收口各环节只做交并差（boolean
    _pathRegion 同款审计结论），共享安全。"""
    contourSnap: tuple      # ((segs元组, poly元组, area, isHole, group), ...)
    tb: object              # 目标整字包围盒（只读）
    kb: object              # 楷体整字包围盒（只读）
    affine: object          # 楷体→目标 仿射桥（纯函数 partial）
    kaiPerims: list         # 楷体每笔周长（只读）
    kaiStrokeBBoxes: list   # 楷体每笔包围盒（只读）
    glyph: object           # 整字 shapely 区域（可为 None，语义同首遍）
    holeBoxes: list         # 并集内环空腔包围盒（重试遍拷贝后复用）

    @classmethod
    def fromCtx(cls, ctx, glyph):
        """首遍收口后抓快照：contours 的协议字段此时与并组刚结束时
        逐值相同（group 终值在 parseAndMerge 内定稿，下游只读）。"""
        return cls(
            contourSnap=tuple((tuple(c["segs"]), tuple(c["poly"]), c["area"],
                               c["isHole"], c["group"])
                              for c in ctx.geom.contours),
            tb=ctx.geom.tb, kb=ctx.kaiRef.kb, affine=ctx.kaiRef.affine,
            kaiPerims=ctx.kaiRef.kaiPerims,
            kaiStrokeBBoxes=ctx.kaiRef.kaiStrokeBBoxes,
            glyph=glyph, holeBoxes=ctx.holeBoxes)

    def applyTo(self, geom, kaiRef, pose, seedMedians):
        """重试遍前段回填：逐字段复刻 parseAndMerge+dbuild.run 种子
        路径的产物（种子整体顶替 D、templateSources 恒"自洽回灌"、
        clibHits 恒 0——与 dbuild 原种子分支逐字节一致）。"""
        geom.contours = [{"segs": list(segs), "poly": list(poly),
                          "area": area, "isHole": isHole, "group": group}
                         for segs, poly, area, isHole, group
                         in self.contourSnap]
        geom.tb = self.tb
        kaiRef.kb = self.kb
        kaiRef.affine = self.affine
        kaiRef.kaiPerims = self.kaiPerims
        kaiRef.kaiStrokeBBoxes = self.kaiStrokeBBoxes
        pose.medians = [[tuple(p) for p in m] for m in seedMedians]
        pose.initMedians = [[tuple(p) for p in m] for m in pose.medians]
        pose.templateSources = ["自洽回灌"] * len(pose.medians)
        pose.templateEnts = [None] * len(pose.medians)
        pose.nStrokes = len(pose.medians)
        pose.clibHits = 0


def sealUnion(geom, kaiRef, strokes, applyBooleanClamp, reuse=None):
    """布尔收口 + 并集恒等校验（含空洞识别与饿死救济循环）；
    返回 (unionCheck, holeBoxes, glyph)。reuse 给出时整字区域与空腔
    直接复用首遍产物（值恒同，见 FrontReuse 注释）。"""
    kai = kaiRef.kai
    contours = geom.contours

    # ------------------------------------------------------------ 布尔收口 + 校验
    # 饿死救济先行：切割中颗粒无收/零宽退化环的笔（宾的宀左点曾只得
    # 一段边界线），用骨架走廊∩本组区域补一个实体，再进收口。
    # 饿死救济：走廊在 rescueStarved 内用组局部仿射从楷体中轴线构造
    # （全局仿射/精调种子都会歪，见函数注释）
    if reuse is not None:
        _glyph = reuse.glyph
        _holeBoxes = [list(b) for b in reuse.holeBoxes]
    else:
        _glyph = booleanClamp.glyphRegion(contours)  # 只算一次，收口各环节复用
        # 空洞识别（并集后内环=真实围合空腔；面积>400 滤掉笔画间缝隙噪声）
        _holeBoxes = []
        try:
            _geoms = list(_glyph.geoms) if hasattr(_glyph, "geoms") \
                else [_glyph]
            for _pg in _geoms:
                for _ring in _pg.interiors:
                    _xs = [p[0] for p in _ring.coords]
                    _ys = [p[1] for p in _ring.coords]
                    if (max(_xs) - min(_xs)) * (max(_ys) - min(_ys)) > 400.0:
                        _holeBoxes.append([round(min(_xs)), round(min(_ys)),
                                           round(max(_xs)), round(max(_ys))])
        except Exception:
            pass
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

    return unionCheck, _holeBoxes, _glyph


def remedianAndSim(kaiRef, strokes):
    """终态中轴线重提（折返矫正）+ 形状匹配打分。"""
    kai = kaiRef.kai

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


def buildResult(ctx):
    """result 组装：拆解结果 JSON 协议（viewer/verify 消费端）。"""
    _pl = _pkg()
    kai = ctx.kaiRef.kai
    ch = ctx.ch
    fontEntry = ctx.fontEntry
    contours = ctx.geom.contours
    nStrokes = ctx.pose.nStrokes
    nGroups = ctx.groups.nGroups
    strokes = ctx.strokes
    strokeGroup = ctx.groups.strokeGroup
    sampleSets = ctx.samples.sampleSets
    cutPoints = ctx.diag.cutPoints
    groupRemapInfo = ctx.diag.groupRemapInfo
    semanticClaims = ctx.diag.semanticClaims
    slotSwaps = ctx.diag.slotSwaps
    ladderProbe = ctx.diag.ladderProbe
    ladderRealign = ctx.diag.ladderRealign
    kaiMatches0 = ctx.kaiRef.kaiMatches0
    unionCheck = ctx.unionCheck
    seedMedians = ctx.pose.seedMedians
    _holeCount = ctx.holeCount
    _holeBoxes = ctx.holeBoxes
    _timings = ctx.diag.timings

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
        "ladderProbe": ladderProbe if _pl.LADDER_PROBE else [],
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

    ctx.result = result


def selfConsistentPass(ctx, reuse):
    """自洽回灌二遍（干净中轴种子重跑，双指标择优采纳）。
    reuse：首遍前段产物快照，随种子传入重跑遍免复算（FrontReuse）。"""
    _pl = _pkg()
    dataHub = ctx.dataHub
    fontEntry = ctx.fontEntry
    ch = ctx.ch
    applyBooleanClamp = ctx.applyBooleanClamp
    seedMedians = ctx.pose.seedMedians
    selfConsistent = ctx.selfConsistent
    strokes = ctx.strokes
    affine = ctx.kaiRef.affine
    kaiStrokeBBoxes = ctx.kaiRef.kaiStrokeBBoxes
    tb = ctx.geom.tb
    _timings = ctx.diag.timings
    result = ctx.result

    # ------------------------------------------------------------ 自洽回灌
    # 第一遍拆完后，用每笔自身几何重提干净中轴作种子重跑一遍匹配；
    # 双指标（失败笔数、retain+shapeSim）择优采用，防止吞并式虚高
    _noFail = not any(s["failed"] for s in strokes)
    if selfConsistent and seedMedians is None and \
            not (_noFail and _meanOf(result, "retainRatio") >= 0.995):
        seeds = _selfSeeds(result)
        if seeds:
            r2 = _pl.runPipeline(dataHub, fontEntry, ch, applyBooleanClamp,
                             seedMedians=seeds, selfConsistent=False,
                             frontReuse=reuse)
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

    ctx.result = result


def axisGuard(ctx, reuse):
    """轴向守卫重试（病笔退楷体种子重跑，全指标不倒退才采纳）。
    reuse：首遍前段产物快照，随种子传入重跑遍免复算（FrontReuse）。"""
    _pl = _pkg()
    dataHub = ctx.dataHub
    fontEntry = ctx.fontEntry
    ch = ctx.ch
    applyBooleanClamp = ctx.applyBooleanClamp
    seedMedians = ctx.pose.seedMedians
    kai = ctx.kaiRef.kai
    affine = ctx.kaiRef.affine
    result = ctx.result

    # ------------------------------------------------------------ 轴向守卫
    # 名义横/竖的切割结果主轴偏差过大（verify TYPE 的第一大失败源）
    # ⇒ 该笔 D 被模板错配/精调带歪。用楷体中轴（位置由结构 C 保证）
    # 作病笔种子、健康笔保留精调中轴，重跑一遍；采纳须病笔数下降且
    # 失败笔/retain/sim/并集全不倒退。只在病字上花第二遍的钱。
    if seedMedians is None:
        _f1 = _pl._axisFails(result)
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
            r3 = _pl.runPipeline(dataHub, fontEntry, ch, applyBooleanClamp,
                             seedMedians=_seeds3, selfConsistent=False,
                             frontReuse=reuse)
            if "error" not in r3:
                _f3 = _pl._axisFails(r3)
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

    ctx.result = result


def run(ctx, frontReuse=None):
    """收口→中轴重提→组装→自洽二遍→轴向守卫定序执行，产出 ctx.result。

    本阶段是唯一收 ctx 整包的阶段（其余阶段只收所需子结构）：result
    组装=全景状态序列化，自洽二遍/轴向守卫要经 runPipeline 复跑、需要
    dataHub/fontEntry 等全部入参句柄。frontReuse：本调用为重试遍时由
    runPipeline 透传的首遍前段快照（收口区域复用）；首遍为 None，
    收口后在此抓快照传给两个重试阶段。"""
    ctx.unionCheck, ctx.holeBoxes, _glyph = sealUnion(ctx.geom, ctx.kaiRef,
                                                      ctx.strokes,
                                                      ctx.applyBooleanClamp,
                                                      reuse=frontReuse)
    ctx.holeCount = len(ctx.holeBoxes)
    ctx.diag.tick("收口")
    remedianAndSim(ctx.kaiRef, ctx.strokes)
    buildResult(ctx)
    # 重试遍（seedMedians 给定）不再嵌套重试，快照无消费者，不抓
    reuse = FrontReuse.fromCtx(ctx, _glyph) \
        if ctx.pose.seedMedians is None else None
    selfConsistentPass(ctx, reuse)
    axisGuard(ctx, reuse)
