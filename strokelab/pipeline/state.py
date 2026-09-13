# -*- coding: utf-8 -*-
"""strokelab.pipeline.state — PipelineCtx：各阶段共享状态的显式协议。

原 runPipeline 巨函数（2600 行）靠闭包隐式共享的全部状态收编于此，
阶段函数统一签名 stage(ctx)。字段按阶段分节，即"改一段不伤整体"的
协议边界（用户裁定的架构约束②），也是未来 C++/FFI 迁移的切面：
协议字段保持纯数据可序列化；标注"运行期桥"的闭包字段只存活于单次
runPipeline 调用内，迁移时随所属阶段一起下沉、不进序列化协议。
"""

import time
from dataclasses import dataclass, field


@dataclass
class PipelineCtx:
    # ———— 入参 ————
    dataHub: object = None
    fontEntry: object = None
    ch: str = ""
    applyBooleanClamp: bool = True
    seedMedians: list = None          # 自洽回灌/轴向守卫的种子中轴
    selfConsistent: bool = True
    kai: dict = None                  # 楷体结构 C（MakeMeAHanzi）
    raw: list = None                  # 目标字形原始轮廓

    # ———— 计时 ————
    timings: list = field(default_factory=list)
    twLast: float = 0.0

    # ———— 轮廓解析/交叠并组/组表（grouping） ————
    contours: list = None             # [{segs, poly, area, isHole, group}]
    nGroups: int = 0
    groupOuters: dict = None          # 组→外环折线表
    groupHoles: dict = None           # 组→孔洞折线表
    groupCentroids: dict = None       # 组→墨点集质心
    groupBBoxes: dict = None          # 组→外环包围盒
    groupCoarse: dict = None          # 组→粗采样折线（近组边界距离用）

    # ———— 全局对齐 + D 构建（dbuild） ————
    kaiPerims: list = None            # 楷体每笔周长（保留字段）
    kaiStrokeBBoxes: list = None      # 楷体每笔包围盒（自洽二遍期望质心用）
    kb: object = None                 # 楷体整字包围盒
    tb: object = None                 # 目标整字包围盒
    affine: object = None             # 运行期桥：楷体坐标→目标坐标闭包
    medians: list = None              # D：每笔模板中轴线（放置后，逐阶段精调）
    initMedians: list = None          # D 初始快照（精调前）
    templateSources: list = None      # 每笔模板出处说明
    templateEnts: list = None         # 每笔 B 库模板条目（可视化用）
    nStrokes: int = 0

    # ———— 指派（assign：G0 代价/G1 匈牙利/S1b 语义认领） ————
    strokeGroupCost: object = None    # 运行期桥：笔→组墨距离行向量闭包
    costRows: list = None             # 全笔×全组墨距离矩阵
    penMatrix: dict = None            # 指派相容性罚（杆⊥笔/点锚大墨）
    strokeGroup: list = None          # 笔→组指派
    groupStrokes: dict = None         # 组→笔列表
    semanticClaims: list = None       # S1b 空组语义认领记录

    # ———— 仲裁（arbitrate：G2..G8 + G8.5 梯队） ————
    wEst: float = 0.0                 # 估算笔宽（走廊 buffer 半径基准）
    groupRegion: object = None        # 运行期桥：组→shapely 区域（带缓存）
    support: object = None            # 运行期桥：笔×组→走廊滑动支撑面积
    barAxisOf: object = None          # 运行期桥：单杆组→主轴角（G6 挂，G8.5 用）
    kaiMatches0: list = None          # 楷体部件路径表（G7/G8.5 用）
    slotMembers: dict = None          # 一级槽位→成员笔
    slotSwaps: list = None            # G7 槽位互换记录
    kaiCentAll: list = None           # 楷体每笔中轴质心（楷体坐标系）
    ladderProbe: list = None          # G8.5 探针信号
    ladderRealign: list = None        # G8.5 执行器施行步骤
    ladderTouched: set = None         # G8.5 动过的组（G9 豁免名单）

    # ———— 组重锚定/断面吸附（anchor：G9/G10） ————
    groupRemapInfo: list = None       # G9 组重锚定记录

    # ———— 冻结+采样归属迭代（iterate） ————
    contourAllowed: list = None       # 轮廓→允许竞争的笔集合（冻结产物）
    w0: float = 0.0                   # 全字估算笔宽
    widths: list = None               # 每笔精调笔宽
    sampleSets: list = None           # 轮廓→边界采样点（含归属标签）
    scoreOf: object = None            # 运行期桥：样本×笔→归属得分闭包
    labelOf: object = None            # 运行期桥：样本→最优笔标签闭包
    templatePaths: list = None        # S2 模板可视化路径

    # ———— 切割（cutting） ————
    cornerSets: list = None           # 轮廓→角点段号集合
    cutPoints: list = None            # 切割点记录
    strokeArcs: list = None           # 笔→切出的边弧
    strokes: list = None              # 重构后的笔画结果表

    # ———— 收口（finalize） ————
    unionCheck: dict = None           # 并集恒等校验
    holeBoxes: list = None            # 并集后真实空腔包围盒
    holeCount: int = 0
    result: dict = None               # 最终结果（含自洽二遍/轴向守卫择优）

    def startTimer(self):
        self.twLast = time.perf_counter()

    def tick(self, name):
        now = time.perf_counter()
        self.timings.append([name, round((now - self.twLast) * 1000)])
        self.twLast = now
