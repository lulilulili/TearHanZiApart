# -*- coding: utf-8 -*-
"""strokelab.pipeline — 拆解管线：D 构建 → 归属 → 精调 → 矢量切割 → 划分式重构。

流程（v5 架构）：
  D = B库同类型笔画骨架（A↔B 映射产物，目标字体自己的形态）按楷体结构 C 定位；
  D 与目标字形实际路径匹配 → 归属/迭代精调 → 标签跳变处 De Casteljau 精确切割 →
  环路追踪 + 直割线弦 + 跨轮廓并环重构 → 布尔收口（boolean.clampStrokes）保证
  所有笔画并集与原字形恒等（不多不少，交叠区双重归属）。

包化布局（原 2862 行单文件按流程切分，见 docs/重构设计.md 模块一）：
  state.py     PipelineCtx——各阶段共享状态的显式协议
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

# G8.5 梯队探针开关：开启时 result 附带 ladderProbe 信号（标定用），
# 探测本身无副作用。执行器待探测精度在 25 字族+健康集上标定后接入。
LADDER_PROBE = False
# G8.5 梯队执行器开关：探测标定达标（家族14/25、健康0/23、抽样0/200）
# 后接入。按 fired 部件的 plan 施行组重排+中轴带重置。
LADDER_ACT = True

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
                seedMedians=None, selfConsistent=True):
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

    grouping.parseAndMerge(ctx)   # 轮廓解析 + 交叠件并组
    ctx.diag.startTimer()         # 计时基点与原单文件版一致（并组后起表）
    dbuild.run(ctx)               # 全局对齐 + D 构建
    grouping.buildTables(ctx)     # 连通组组表
    # G0/G1 指派 + S1b 语义认领
    ctx.diag.semanticClaims = assign.run(ctx.geom, ctx.kaiRef, ctx.groups,
                                         ctx.pose, ctx.cost)
    arbitrate.run(ctx)            # G2..G8 仲裁链 + G8.5 梯队
    anchor.run(ctx)               # G9 组重锚定 + G10 断面吸附
    iterate.run(ctx)              # 归属迭代精调 + 终态吸直
    cutting.run(ctx)              # 主人判定→矢量切割→划分重构
    finalize.run(ctx)             # 收口→result→自洽二遍→轴向守卫
    return ctx.result
