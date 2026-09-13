# -*- coding: utf-8 -*-
"""strokelab.pipeline.state — PipelineCtx：各阶段共享状态的显式协议。

原 runPipeline 巨函数（2600 行）靠闭包隐式共享的全部状态收编于此。
用户裁定（2026-09-13）：没必要向所有流程开放全部字段——状态按语义面
拆成子结构，各阶段函数签名只收自己需要的子结构（≤5 参数），不再整包
透传：

  GlyphGeom   目标字形几何输入面（轮廓/整字包围盒）
  GroupTable  连通组归属表（组表 + 笔↔组指派）
  StrokePose  笔画位姿（D 中轴线/笔宽/模板出处）
  KaiRef      楷体结构参照（只读：C 数据 + 名义布局派生量）
  CostModel   指派代价面（墨距离矩阵 + 相容性罚）
  Diagnostics 诊断面（计时 + 各仲裁层的施行记录）
  SampleField 边界采样归属场（iterate 产出、cutting 消费）

子结构协议字段保持纯数据可序列化——子模块边界=未来 C++/FFI 迁移边界。
标注"运行期桥"的字段是 functools.partial 绑定的模块级函数（本体在各
阶段模块），只存活于单次 runPipeline 调用内，迁移时随所属阶段一起
下沉、不进序列化协议。
"""

import time
from dataclasses import dataclass, field


@dataclass
class GlyphGeom:
    """目标字形几何输入面（grouping 产出，全程只读）。"""
    contours: list = None             # [{segs, poly, area, isHole, group}]
    tb: object = None                 # 目标整字包围盒


@dataclass
class GroupTable:
    """连通组归属表：组几何表（buildTables）+ 笔↔组指派（assign/仲裁链改写）。"""
    nGroups: int = 0
    strokeGroup: list = None          # 笔→组指派
    groupStrokes: dict = None         # 组→笔列表
    groupBBoxes: dict = None          # 组→外环包围盒
    groupCentroids: dict = None       # 组→墨点集质心
    groupOuters: dict = None          # 组→外环折线表
    groupHoles: dict = None           # 组→孔洞折线表
    groupCoarse: dict = None          # 组→粗采样折线（近组边界距离用）
    regionOf: object = None           # 运行期桥：组→shapely 区域（带缓存）
    support: object = None            # 运行期桥：笔×组→走廊滑动支撑面积
    barAxisOf: object = None          # 运行期桥：单杆组→主轴角（G6 挂，G8.5 用）


@dataclass
class StrokePose:
    """笔画位姿：D 中轴线及其精调伴生量（dbuild 产出，逐阶段精调）。"""
    nStrokes: int = 0
    medians: list = None              # D：每笔模板中轴线（放置后，逐阶段精调）
    initMedians: list = None          # D 初始快照（精调前）
    widths: list = None               # 每笔精调笔宽
    scoreMedians: list = None         # 评分用延长中轴（iterate 终态，scoreOf 读）
    scoreWidths: list = None          # 评分用夹紧笔宽（iterate 终态，scoreOf 读）
    templateSources: list = None      # 每笔模板出处说明
    templateEnts: list = None         # 每笔 B 库模板条目（可视化用）
    templatePaths: list = None        # S2 模板可视化路径
    seedMedians: list = None          # 自洽回灌/轴向守卫的种子中轴（入参）
    clibHits: int = 0                 # C库部件骨架顶替笔数（dbuild 统计；
                                      # CLIB_ENABLE 开时进 result.clibHits）
    w0: float = 0.0                   # 全字估算笔宽
    wEst: float = 0.0                 # 走廊 buffer 半径基准（G2 估算）


@dataclass
class KaiRef:
    """楷体结构参照（MakeMeAHanzi C 数据 + 名义布局派生量，只读）。"""
    kai: dict = None                  # 楷体结构 C
    kb: object = None                 # 楷体整字包围盒
    kaiPerims: list = None            # 楷体每笔周长（保留字段）
    kaiStrokeBBoxes: list = None      # 楷体每笔包围盒（自洽二遍期望质心用）
    kaiMatches0: list = None          # 楷体部件路径表（G7/G8.5 用）
    kaiCentAll: list = None           # 楷体每笔中轴质心（楷体坐标系）
    slotMembers: dict = None          # 一级槽位→成员笔
    affine: object = None             # 运行期桥：楷体坐标→目标坐标仿射


@dataclass
class CostModel:
    """指派代价面：墨距离矩阵 + 相容性罚（assign 产出）。"""
    costRows: list = None             # 全笔×全组墨距离矩阵
    penMatrix: dict = None            # 指派相容性罚（杆⊥笔/点锚大墨）
    strokeGroupCost: object = None    # 运行期桥：笔→组墨距离行向量


@dataclass
class Diagnostics:
    """诊断面：计时 + 各仲裁层施行记录（result 组装消费）。"""
    timings: list = field(default_factory=list)
    twLast: float = 0.0
    semanticClaims: list = None       # S1b 空组语义认领记录
    slotSwaps: list = None            # G7 槽位互换记录
    groupRemapInfo: list = None       # G9 组重锚定记录
    ladderProbe: list = None          # G8.5 探针信号
    ladderRealign: list = None        # G8.5 执行器施行步骤
    ladderTouched: set = None         # G8.5 动过的组（G9 豁免名单）
    cutPoints: list = None            # 切割点记录

    def startTimer(self):
        self.twLast = time.perf_counter()

    def tick(self, name):
        now = time.perf_counter()
        self.timings.append([name, round((now - self.twLast) * 1000)])
        self.twLast = now


@dataclass
class SampleField:
    """边界采样归属场（iterate 产出、cutting 消费）。"""
    sampleSets: list = None           # 轮廓→边界采样点（含归属标签）
    contourAllowed: list = None       # 轮廓→允许竞争的笔集合（冻结产物）
    cornerSets: list = None           # 轮廓→角点段号集合
    scoreOf: object = None            # 运行期桥：样本×笔→归属得分
    labelOf: object = None            # 运行期桥：样本→最优笔标签


@dataclass
class PipelineCtx:
    # ———— 入参 ————
    dataHub: object = None
    fontEntry: object = None
    ch: str = ""
    applyBooleanClamp: bool = True
    selfConsistent: bool = True
    raw: list = None                  # 目标字形原始轮廓

    # ———— 语义面子结构 ————
    geom: GlyphGeom = field(default_factory=GlyphGeom)
    groups: GroupTable = field(default_factory=GroupTable)
    pose: StrokePose = field(default_factory=StrokePose)
    kaiRef: KaiRef = field(default_factory=KaiRef)
    cost: CostModel = field(default_factory=CostModel)
    diag: Diagnostics = field(default_factory=Diagnostics)
    samples: SampleField = field(default_factory=SampleField)

    # ———— 切割/收口产物（跨面聚合件，直接挂 ctx） ————
    strokeArcs: list = None           # 笔→切出的边弧
    strokes: list = None              # 重构后的笔画结果表
    unionCheck: dict = None           # 并集恒等校验
    holeBoxes: list = None            # 并集后真实空腔包围盒
    holeCount: int = 0
    result: dict = None               # 最终结果（含自洽二遍/轴向守卫择优）
