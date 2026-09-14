# -*- coding: utf-8 -*-
"""strokelab.pipeline — 拆解管线：D 构建 → 归属 → 精调 → 矢量切割 → 划分式重构。

流程（v5 架构）：
  D = B库同类型笔画骨架（A↔B 映射产物，目标字体自己的形态）按楷体结构 C 定位；
  D 与目标字形实际路径匹配 → 归属/迭代精调 → 标签跳变处 De Casteljau 精确切割 →
  环路追踪 + 直割线弦 + 跨轮廓并环重构 → 布尔收口（boolean.clampStrokes）保证
  所有笔画并集与原字形恒等（不多不少，交叠区双重归属）。

包化布局（原 2862 行单文件按流程切分，见 docs/重构设计.md 模块一）：
  state.py     PipelineCtx——七个语义面子结构的组合（GlyphGeom/
               GroupTable/StrokePose/KaiRef/CostModel/Diagnostics/
               SampleField）；各阶段函数只收自己需要的子结构（≤5 参数），
               finalize 除外（result 组装/复跑需要全景）
  helpers.py   模块级纯函数（匈牙利/折返计数/轴向判据/回灌种子…）
  grouping.py  轮廓解析 + 交叠并组 + 组表
  dbuild.py    全局对齐 + D 构建（模板选择/放置/吸直）
  assign.py    G0 代价/penMatrix/G1 匈牙利/S1b 语义认领
  arbitrate.py G2..G8 仲裁链 + G8.5 梯队（探针+执行器）
  anchor.py    G9 组重锚定 / G10 断面吸附
  iterate.py   contourAllowed 冻结 + 采样归属迭代 + 终态吸直
  cutting.py   主人判定/边弧归属/标签平滑/矢量切割/划分重构
  finalize.py  布尔收口/终态中轴重提/result 组装/自洽二遍/轴向守卫

兼容入口：import strokelab.pipeline as pl 后，pl.runPipeline /
pl.ITERS / pl.LADDER_PROBE / pl.LADDER_ACT / pl._axisFails 等属性
可读可赋值；运行期开关（ITERS/LADDER_*）与 runPipeline/_axisFails
在阶段模块内经 sys.modules["strokelab.pipeline"] 取当前值，赋值即生效
（tools/ladder_calib3.py、audit 复跑、reverify 流程依赖此语义）。
"""

ITERS = 5

import os as _os

# G8.5 梯队探针开关：开启时 result 附带 ladderProbe 信号（标定用），
# 探测本身无副作用。执行器待探测精度在 25 字族+健康集上标定后接入。
LADDER_PROBE = False
# G8.5 梯队执行器开关：探测标定达标（家族14/25、健康0/23、抽样0/200）
# 后接入。按 fired 部件的 plan 施行组重排+中轴带重置。
LADDER_ACT = True
# C库（偏旁部件模板层）注入开关：架构评审第6项的评估性原型，**默认
# False**（关闭时 dbuild 只多一次布尔判断，结果逐字节与基线一致，
# parity 硬门为证）。开启时对一级结构槽位命中 C 条目的笔，用载体字
# 实拆骨架顶替 B 模板放置（逐笔守卫回退，见 dbuild._clibSlotMap）。
# 支持环境变量 STROKELAB_CLIB=1 开启：verify 批跑的 spawn worker 子
# 进程读不到主进程的包属性赋值，环境变量可随子进程继承
# （tools/eval_clib.py / verify_batch 开C评估依赖此语义）。
CLIB_ENABLE = _os.environ.get("STROKELAB_CLIB", "") == "1"
# 仲裁 Tracer 统一决策迹开关（架构评审#1）：开启时各仲裁级把改判提议
# （含被拒绝的，adopted=False——胜率表分子分母都要）组装进
# diag.trace，result 挂 "trace" 键；关闭时 result["trace"]=[]。
# 埋点只读不回写，任何判定/顺序/浮点与关闭时逐位一致（parity 硬门）。
# 默认开——trace 本身轻量（只在病征触发处记录，健康字近零条目）。
# 环境变量 STROKELAB_TRACE=0 可关（spawn worker 继承语义同 CLIB）。
TRACE_ON = _os.environ.get("STROKELAB_TRACE", "1") != "0"
# G8 序保持互换执行开关（矩阵归并 2a，docs/矩阵归并设计.md 实施记录）：
# sample958 胜率表曾判死刑（双字体采纳率 3.7%/0.8%、采纳字通过率 0%，仅
# 攮/銲且现状均失败），但全库枚举（tools/enum_g8.py，9574 字×双字体）
# **推翻该判决**：采纳字实为 SC 19 + simsun 4 = 23 字，其中 11 字现通过；
# 撤除执行会砸 9 字（乹啇喼嫜掉狗谿/澱鬒 通过→ORDER 等硬失败，含 G8
# 设计动机字"狗"的犭撇勹撇互换），仅修 5 字（睜睢瞘瞟瞪 目族）——净 -4。
# 抽样死刑=stride-10 只采到攮/銲的伪象。按裁定"出现通过→失败即回退阀"，
# 默认保持 True=执行（与基线逐位一致）。False=撤除态（诊断实验用）：判据
# 评估照跑，满足互换条件时不施行，迹记 action="demoted"（adopted=False，
# evidence 含 wouldSwap 明细），且不置 swapped——恒单遍、按首遍未换状态
# 评估（与执行态双 pass 口径不逐位对应，见 arbitrate.py G8 段注释）。
# 环境变量 STROKELAB_G8_EXEC=0 切撤除态（spawn worker 继承语义同
# CLIB_ENABLE——批跑子进程读不到主进程包属性赋值）。
ARB_G8_EXEC = _os.environ.get("STROKELAB_G8_EXEC", "1") != "0"
# 杆容量罚开关（矩阵归并 2b，docs/矩阵归并设计.md·对抗评审裁定#3/#4/#7）：
# G5"杆容量"证据折进锚定罚——若 k 锚进组 g，k 名义弦长 > 1.08× g 的
# bbox 沿笔轴投影（判据=G5 疑抢杆触发判据的锚定期版本，照抄实码、不加
# 它没有的形状门/笔型门），penTags['barcap']=200 只进匈牙利锚定矩阵
# （红线：不进 costRows 本体；G8.5 执行器否决门分道豁免该罚种）。仅
# 名义遍施行（seedMedians is None，同 G8.5 执行器门）——种子遍中轴已
# 实测落位，容量罚是对锚定期名义猜测的修正，不对实证证据二次猜测；
# 该门同时保证"首遍前段两态同判 ⇒ 全管线两态同判"的归纳链成立（全库
# 复核可用前段截跑，tools/enum_barcap.py）。默认 False（基线逐位一致，
# parity 硬门），六门标定达标后转 True 提交。环境变量
# STROKELAB_PEN_BARCAP=1 开（spawn worker 继承语义同 CLIB_ENABLE）。
PEN_BARCAP = _os.environ.get("STROKELAB_PEN_BARCAP", "1") == "1"

from .helpers import (_medianDeviation, _hungarian, _switchbackCount,
                      _axisFails, _reMedianFromStroke, _selfSeeds,
                      _meanOf, _strokeCenter, _secondPassBetter)
from .state import (PipelineCtx, GlyphGeom, GroupTable, StrokePose,
                    KaiRef, CostModel, Diagnostics, SampleField)
from . import grouping
from . import dbuild
from . import assign
from . import arbitrate
from . import anchor
from . import iterate
from . import cutting
from . import finalize


def runPipeline(dataHub, fontEntry, ch, applyBooleanClamp=True,
                seedMedians=None, selfConsistent=True, frontReuse=None):
    """frontReuse（仅重试遍，finalize 内部传递）：首遍前段产物快照
    （finalize.FrontReuse）。种子重跑时前段（轮廓解析并组/全局对齐/
    D 构建）与首遍完全相同且 dbuild 的 B 候选扫描结果会被种子整体
    顶替——复用快照直接跳过这两个阶段（值等价，parity 34 例硬门），
    重试字省下前段复算。外部调用方不传此参，行为不变。"""
    kai = dataHub.kai(ch)
    if not kai:
        return {"error": "MakeMeAHanzi 中没有「%s」的笔画数据" % ch}
    raw = fontEntry.glyphContours(ch)
    if not raw:
        return {"error": "字体 %s 中没有「%s」字形" % (fontEntry.key, ch)}

    ctx = PipelineCtx(dataHub=dataHub, fontEntry=fontEntry, ch=ch,
                      applyBooleanClamp=applyBooleanClamp,
                      selfConsistent=selfConsistent, raw=raw,
                      kaiRef=KaiRef(kai=kai),
                      pose=StrokePose(seedMedians=seedMedians))
    geom, groups, pose = ctx.geom, ctx.groups, ctx.pose
    kaiRef, cost, diag, samples = ctx.kaiRef, ctx.cost, ctx.diag, ctx.samples

    if frontReuse is not None and seedMedians is not None:
        # 重试遍前段复用：快照回填 contours/tb/affine 等 + 种子顶替 D
        # （applyTo 逐字段复刻 parseAndMerge+dbuild 种子路径产物）。
        # timings 阶段名保持与首遍一致，值近零。
        diag.startTimer()
        frontReuse.applyTo(geom, kaiRef, pose, seedMedians)
        diag.tick("解析对齐/D构建")
    else:
        grouping.parseAndMerge(geom, kaiRef, raw)   # 轮廓解析 + 交叠件并组
        diag.startTimer()         # 计时基点与原单文件版一致（并组后起表）
        dbuild.run(dataHub, fontEntry, geom, kaiRef, pose)  # 全局对齐 + D 构建
        diag.tick("解析对齐/D构建")
    grouping.buildTables(geom, groups)          # 连通组组表
    # G0/G1 指派 + S1b 语义认领（G1/S1b 决策迹随返回值并入 diag.trace）
    diag.semanticClaims, _tr = assign.run(geom, kaiRef, groups, pose, cost)
    diag.trace += _tr
    # G2..G8 仲裁链 + G8.5 梯队——层序即证据强度递增序，不可重排
    # （各级决策迹随返回值并入 diag.trace；G8.5/G9/G10 直接写 diag）
    diag.trace += arbitrate.corridorFit(geom, groups, pose, cost)
    diag.trace += arbitrate.componentMate(kaiRef, groups, pose, cost)
    diag.trace += arbitrate.barOverload(geom, kaiRef, groups, pose, cost)
    diag.trace += arbitrate.barTheftSwap(geom, kaiRef, groups, pose, cost)
    diag.trace += arbitrate.axisMisplace(geom, kaiRef, groups)
    diag.slotSwaps, _tr = arbitrate.slotSwap(kaiRef, groups, pose, cost)
    diag.trace += _tr
    diag.trace += arbitrate.orderPreserve(kaiRef, groups, pose, cost)
    diag.ladderProbe = arbitrate.ladderProbeStage(kaiRef, groups, pose)
    arbitrate.ladderActStage(kaiRef, groups, pose, cost, diag)
    # G9 组重锚定 + G10 断面吸附
    anchor.run(geom, kaiRef, groups, pose, diag)
    # 归属迭代精调 + 终态吸直 + S2 模板可视化
    samples.contourAllowed = iterate.freezeAllowed(geom, groups, pose)
    diag.tick("连通组分治")
    iterate.refineLoop(geom, kaiRef, pose, samples)
    diag.tick("归属迭代精调")
    iterate.buildTemplatePaths(pose)
    # 主人判定 → 边弧归属 → 标签平滑 → 矢量切割 → 划分重构
    cutting.ownerJudge(geom, pose, samples)
    cutting.consolidateArcs(geom, pose, samples)
    cutting.labelSmooth(samples)
    diag.tick("整体归属与平滑")
    ctx.strokeArcs = cutting.vectorCut(geom, pose, samples, diag)
    diag.tick("矢量切割")
    ctx.strokes = cutting.reconstruct(kaiRef, groups, pose, ctx.strokeArcs)
    diag.tick("重构")
    finalize.run(ctx, frontReuse)  # 收口→result→自洽二遍→轴向守卫
    # C库诊断：clibHits = 本次调用第一遍 D 构建被 C 骨架顶替的笔数
    # （自洽二遍/轴向守卫复跑走种子路径恒为 0，采纳复跑结果后仍以
    # 外层首遍计数覆盖——评估口径是"D构建命中"而非"终态出处"）。
    # 开关关闭时不加键：result 与基线逐字节一致（parity 硬门语义）。
    if CLIB_ENABLE and isinstance(ctx.result, dict) and \
            "error" not in ctx.result:
        ctx.result["clibHits"] = ctx.pose.clibHits
    # 统一决策迹（架构评审#1）：仲裁全谱埋点挂 result["trace"]。口径
    # 与 clibHits 同——自洽二遍/轴向守卫采纳复跑结果后仍以**外层首遍**
    # 的迹覆盖（重跑遍走种子路径，其仲裁面对的不是名义位，不代表本级
    # 病征真实触发；胜率表评估"首遍名义指派的仲裁决策"）。关闭时恒 []
    # （键仍在，消费端免判存在性；parity 只比 paths/ladder/holes，不受影响）。
    if isinstance(ctx.result, dict) and "error" not in ctx.result:
        ctx.result["trace"] = ctx.diag.trace \
            if (TRACE_ON or LADDER_PROBE) else []
    return ctx.result
