# -*- coding: utf-8 -*-
"""strokelab.boolean — shapely 布尔收口：保证所有笔画并集与原字形恒等。

clampStrokes：
  1. 裁剪（保证“不多”）：每笔区域与原字形求交，越界部分删除；
  2. 残差回填（保证“不少”）：原形中未被任何笔覆盖的面片，按共享边界长度
     回填给相邻笔画；
  3. 仅对确有问题的笔画替换为裁剪后的多边形路径（细分 2 单位），
     无问题的笔画保留原始贝塞尔精确切片路径。
"""

import math
import os as _os

from functools import lru_cache

from shapely.geometry import Point, Polygon, MultiPolygon, LineString
from shapely.ops import unary_union

from .geometry import (parseContours, flattenSegs, shapeDescriptor,
                       signedArea, _medialAdjacency)

_FLAT = 3.0  # 布尔运算用的细分步长

# ---- 形态修复器·交界毛刺置换开关（2026-09-15）。标定达标（提交
# c7689f8：病例集 morphJunc -16.5%、健康百字零翻转）+四门全绿后默认
# 开（门禁数字见启用提交）。环境变量 STROKELAB_MORPH_REPAIR=0 关
# （verify 批跑 spawn worker 读不到主进程的模块属性赋值，环境变量随
# 子进程继承——pipeline.CLIB_ENABLE 先例）。开关放 boolean 模块而非
# pipeline 包 __init__（本轮授权路径约束，arbitrate.LADDER_F 先例）；
# finalize.sealUnion 经 booleanClamp 属性运行期取值，标定脚本赋值即生效。
MORPH_REPAIR = _os.environ.get("STROKELAB_MORPH_REPAIR", "1") != "0"
MORPH_RETAIN_GATE = 0.95   # 检测门：保留率低于此（切割动过交界带）才审
MORPH_BRIDGE_GATE = 2      # 或 桥数≥此值（交界带靠桥缝合=毛刺高发）
MORPH_SPUR_FLOOR = 40.0    # 短叶枝阈下限（照 verify.MORPH_SPUR_MIN 口径）
MORPH_PATCH_MAX = 0.35     # 单补丁面积上限（占本笔比）。曾设 0.10——
                           # 入侵残片型病理（simkai 草 #5横被邻笔残片
                           # 压顶，残片≈38% 本笔面积）整类漏修；真防线
                           # 是 接触>颈宽+形态改善+单连通 三守卫，面积
                           # 上限只挡"整半边身子搬家"的失控场景
MORPH_SHARE_MAX = 0.45     # 单笔转让总面积上限——retainRatio 字段收口链
                           # 从不改写，"retain 不得下降"守卫按面积化口径
                           # 落地：置换后本笔至少留 55% 面积
MORPH_RECV_MAX = 0.6       # 补丁不得超过受让笔面积的 0.6 倍（挂在别人
                           # 身上的肉不会比别人的身子还大）
MORPH_RECV_SICK = 30       # 受让笔当前形态分（交叉+短叶枝）超过此值不
                           # 受让：重病笔的轮廓不可信，往它身上归肉是
                           # 垃圾上摞垃圾（simkai 草 #5横 分 88 实证，
                           # 收下残片后 verify 全字 junc 30→37 净恶化）。
                           # 阈值标定：宋体健康-中噪笔实测 1-17（強 的
                           # 受让候选 14-17 属衬线锯齿底噪，12 曾误拦），
                           # 重病实例 88——取 30 分界
MORPH_CONTACT_MIN = 4.0    # 邻笔接触长度下限（数值噪声级接触不构成转让证据）
MORPH_SEAM_TOL = 0.6       # 接触判定膨胀带（切割缝两侧 0.1 舍入+细分噪声）
MORPH_ARM_MAX = 250.0      # 伪臂候选链长上限：交界粘连尾巴（永 横折钩沿捺
                           # 左flank 的楔实测 187）在此之下；真笔臂（横杆/
                           # 竖身，geometry.medialJunctions 实测真 T 臂 354+）
                           # 在此之上——同一尺度先例（角噪二段剪除 <250）
MORPH_MIN_GAIN = 5         # 本笔手术收益（交叉+短叶枝净降）下限。标定：
                           # 赢家 永 5 / simhei草 15，噪声触发 我 1 / 婕 3
                           # ——收益≤重扫噪声量级(±2-3)的置换纯属搬椅子，
                           # 级联(种子→自洽二遍)方差还会外溢（健康集 筻
                           # 曾因小收益置换 retain -0.027）
MORPH_VFLAT = 4.0          # verify 审计域细分步长（IOU_FLAT 口径）——曲线
                           # 路径在 3.0/4.0 两种展平下的中轴拓扑可以天差
                           # 地别（SC 筻 #9 退化缎带 164 vs 268），守卫的
                           # "前值"参照必须按 verify 口径量
MORPH_OVL_CAP = 0.55       # 置换后任一同组笔对重叠比(交/较小者)上限——
                           # 略低于 verify OVERLAP 门 0.6 留余量。sample958
                           # 实测两类越线：蝮 donor 缩身后与邻笔比值飙到
                           # 96%（交集没变、分母变小）；鄱 受让方吞并第三
                           # 方（补丁落在与他笔双重归属带上，受让后 100%
                           # 包含）。置换前已越线的笔对不追责（存量病）

# ---- 受害者取回（毛刺置换的反向执行域，2026-09-26）。donor 主导的
# 置换只见得到 donor 中轴图上的短叶枝/伪臂；simsun 威·笔0 病理相反：
# 顶横左端 x148-208 被撇硬切走（两件交叠=0），悬肉长在**健康邻笔的
# 中轴终端链**上（撇 剪稳后是纯路径图 junc=0，楔是主链末端，spur/arm
# 框架按定义不可见）。取回=受害者主导：贴壁前缀找楔+垂直刀截断+同一
# 张"挂在别人身上的肉"判据（接触>切颈）反向转让。
MORPH_RECLAIM_RETAIN = 0.93  # 受害者门：retain 低于此才许取回（威笔0
                           # 0.887；健康衬线笔多在 0.93-0.99，取回是
                           # 大块肉手术，门须比 donor 检测门 0.95 更严）
MORPH_RECLAIM_MIN = 150.0  # 缺口/补丁面积下限——取回是补大块缺肉，
                           # 碎渣级搬运归 donor 置换/上游补缝
MORPH_RECLAIM_BUDGET = 1.5  # 补丁 ≤ 1.5×受害者缺口（缺口=area×(1/retain
                           # -1)，威 缺口 2763 vs 楔 1869=0.68；圆盘粗
                           # 割曾取 4615=1.67 连撇肩都拿走，刀截后达标）
MORPH_RECLAIM_MARGIN = 1.15  # 接触须 > 1.15×切颈。威 楔：贴缝接触 89 vs
                           # 垂直刀切颈 58=1.53；丁字对接的自然笔端
                           # 接触≈自身颈宽（比值≈1.0），1.15 拒之
MORPH_RECLAIM_HUG = 1.1    # 贴壁判定 d≤1.1×余隙+2：中轴点到受害者的
                           # 距离≈自身余隙=受害者边界就是本笔的一面墙
                           # （楔沿切割缝生长的几何本质）。曾取 1.2——
                           # 前缀多吞 20 单位撇肩（切点 y598 vs 611）
MORPH_RECLAIM_FILL = 0.5   # 治愈门①：取回增肉须 ≥0.5×缺口。部分缝补=
                           # 搬椅子（MIN_GAIN 同理）：simsun 草 竖→底横
                           # 0.22/威 竖捺 0.04 全是噪声级抓肉，赢家 威笔0
                           # 1.07 / 我笔0 0.65
MORPH_RECLAIM_HEAL = MORPH_RECLAIM_RETAIN  # 治愈门②：取回后 retain 须
                           # 跨回健康线（威 0.887→0.963、我 0.911→0.968；
                           # 草 底横 0.467→0.586 治不好=病根在别处，不动）


def _loopPolys(pathStr):
    polys = []
    for c in parseContours(pathStr):
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) >= 4:
            try:
                pg = Polygon(pts)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                if not pg.is_empty:
                    polys.append(pg)
            except Exception:
                pass
    return polys


def _evenOddRegion(polys):
    """多环按奇偶合成区域（环形段=外环⊕孔环）。"""
    region = None
    for pg in polys:
        if region is None:
            region = pg
        else:
            try:
                region = region.symmetric_difference(pg)
            except Exception:
                region = region.buffer(0).symmetric_difference(pg.buffer(0))
    if region is None:
        return None
    if not region.is_valid:
        region = region.buffer(0)
    return region


@lru_cache(maxsize=256)
def _pathRegion(pathStr):
    """路径串 → 奇偶合成区域，模块级 LRU 只读共享。同一笔的区域在
    rescueStarved/clampStrokes/enforceConnectivity/reUnionCheck 之间
    曾重建 4-5 次。键=路径串，笔画路径被收口改写即换新串、自然失效。
    Shapely 2.x 几何不可变，所有调用点只做 intersection/difference/
    union（均产新对象）后重绑定，无就地改写（已逐点审计）。容量
    2048：单次收口触达路径至多数百，批量按 LRU 淘汰。"""
    return _evenOddRegion(_loopPolys(pathStr))


def glyphRegion(contours):
    """孔洞按所属组减除后再组间取并（≈nonzero 填充）：孔只挖自己外环的
    区域，穿过该孔的其它实体轮廓经并集仍保持实心——全局减孔曾把"中"的
    中竖在口字内腔处挖断。"""
    byGroup = {}
    for c in contours:
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) < 4:
            continue
        try:
            pg = Polygon(pts)
            if not pg.is_valid:
                pg = pg.buffer(0)
            g = byGroup.setdefault(c.get("group", 0), {"outers": [], "holes": []})
            (g["holes"] if c.get("isHole") else g["outers"]).append(pg)
        except Exception:
            pass
    regions = []
    for g in byGroup.values():
        if not g["outers"]:
            continue
        r = unary_union(g["outers"])
        if g["holes"]:
            r = r.difference(unary_union(g["holes"]))
        if not r.is_valid:
            r = r.buffer(0)
        if not r.is_empty:
            regions.append(r)
    if not regions:
        return None
    region = unary_union(regions)
    if not region.is_valid:
        region = region.buffer(0)
    return region


def _regionToPath(region):
    geoms = []
    if isinstance(region, Polygon):
        geoms = [region]
    elif isinstance(region, MultiPolygon):
        geoms = list(region.geoms)
    else:
        try:
            geoms = [g for g in region.geoms if isinstance(g, Polygon)]
        except Exception:
            return ""
    parts = []
    for g in geoms:
        if g.area < 4:
            continue
        rings = [g.exterior] + list(g.interiors)
        for ring in rings:
            coords = list(ring.coords)
            if len(coords) < 4:
                continue
            d = "M %.1f %.1f " % coords[0][:2]
            d += " ".join("L %.1f %.1f" % c[:2] for c in coords[1:-1])
            parts.append(d + " Z")
    return " ".join(parts)


def _piecesOf(region):
    if region is None or region.is_empty:
        return []
    if isinstance(region, Polygon):
        return [region]
    if isinstance(region, MultiPolygon):
        return list(region.geoms)
    return [g for g in getattr(region, "geoms", []) if isinstance(g, Polygon)]


def _groupRegionOf(contours, g):
    """组 g 的 shapely 区域：外环并集 − 孔洞并集。rescueStarved 与
    clampStrokes 共用（曾各写一份等价逻辑）。"""
    outs = None
    hs = None
    for c in contours:
        if c.get("group") != g:
            continue
        pts = flattenSegs(c["segs"], _FLAT)
        if len(pts) < 4:
            continue
        pg = Polygon(pts)
        if not pg.is_valid:
            pg = pg.buffer(0)
        if c.get("isHole"):
            hs = pg if hs is None else hs.union(pg)
        else:
            outs = pg if outs is None else outs.union(pg)
    if outs is not None and hs is not None:
        outs = outs.difference(hs)
        if not outs.is_valid:
            outs = outs.buffer(0)
    return outs


def rescueStarved(contours, strokes, kaiStrokePaths, kaiMedians=None, glyph=None):
    """饿死救济：重构后面积不足楷体占比预期 35% 的笔（含零宽退化环），
    用骨架走廊（中轴线按笔宽 buffer）∩ 本组轮廓区域作为救济区域——
    纯矢量、必在字形内。走廊中轴线用楷体中轴线经**组局部仿射**映射
    （楷体同组笔画包围盒→目标组轮廓包围盒）：全局仿射在部件比例
    差异大时会把楷体底横映到目标腔体中间（鸿蒙咋的口字旁全高瘦长、
    楷体口字旁在中上部）；精调中轴线被杂散样本带歪更不可用。
    与邻笔重叠=双重归属，允许，并从侵占邻笔区域减掉救济体恢复真
    划分。并集恒等仍由随后的 clampStrokes 保证。返回被救济笔序号。"""
    from shapely.geometry import LineString

    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return []
    kaiAreas = []
    for p in kaiStrokePaths:
        r = _pathRegion(p)
        kaiAreas.append(r.area if r is not None else 0.0)
    kaiTotal = sum(kaiAreas) or 1.0

    groupRegions = {}

    def regionOfGroup(g):
        if g not in groupRegions:
            groupRegions[g] = _groupRegionOf(contours, g)
        return groupRegions[g]

    rescued = []
    widthsAll = sorted(float(s.get("width") or 0.0) for s in strokes
                       if s.get("width"))
    medWidth = widthsAll[len(widthsAll) // 2] if widthsAll else 60.0

    groupStrokeIdx = {}
    for i, s in enumerate(strokes):
        groupStrokeIdx.setdefault(s.get("group"), []).append(i)

    def structMedian(i, g):
        """楷体中轴线经组局部仿射（楷体同组笔画包围盒→目标组区域包围盒）。"""
        if kaiMedians is None or i >= len(kaiMedians) or not kaiMedians[i]:
            return None
        region = regionOfGroup(g)
        if region is None or region.is_empty:
            return None
        tb = region.bounds
        ks = groupStrokeIdx.get(g, [i])
        xs = [p[0] for k in ks if k < len(kaiMedians) for p in kaiMedians[k]]
        ys = [p[1] for k in ks if k < len(kaiMedians) for p in kaiMedians[k]]
        if not xs:
            return None
        kx0, kx1 = min(xs), max(xs)
        ky0, ky1 = min(ys), max(ys)
        sx = (tb[2] - tb[0]) / max(1.0, kx1 - kx0)
        sy = (tb[3] - tb[1]) / max(1.0, ky1 - ky0)
        return [(tb[0] + (p[0] - kx0) * sx, tb[1] + (p[1] - ky0) * sy)
                for p in kaiMedians[i]]

    for i, s in enumerate(strokes):
        cur = None if s["failed"] else _pathRegion(s["path"])
        curArea = cur.area if cur is not None else 0.0
        expect = glyph.area * (kaiAreas[i] / kaiTotal if i < len(kaiAreas) else 0.0)
        starved = curArea < max(100.0, expect * 0.35)
        # 轴向病判：横/竖笔的轮廓 PCA 主轴严重背离（>45°）= 切错拿了
        # 邻笔的竖片/横片（鸿蒙咋的口底横曾拿到壁上竖条），同样重建
        misAxis = False
        if not starved and cur is not None and s.get("type") in ("横", "竖"):
            d = shapeDescriptor([s["path"]])
            if d and d["elong"] >= 1.8:
                ang = abs(math.degrees(d["mainAngle"])) % 180.0
                dev = min(ang, 180.0 - ang) if s["type"] == "横" \
                    else abs(ang - 90.0)
                misAxis = dev > 45.0
        if not starved and not misAxis:
            continue
        median = structMedian(i, s.get("group"))
        if median is None or len(median) < 2:
            median = s.get("median")
        if not median or len(median) < 2:
            continue
        region = regionOfGroup(s.get("group"))
        if region is None or region.is_empty:
            region = glyph
        # 饿死笔自身的宽度估计同样是饿的（样本少）——用全字笔宽中位数兜底
        w = max(float(s.get("width") or 0.0), medWidth, 30.0)
        try:
            corridor = LineString([tuple(p) for p in median]).buffer(w * 0.6)
            # 位置吸附：bbox 线性映射在部件比例非线性差异时会把走廊放
            # 进腔体（楷体口矮胖底横占30%高、鸿蒙口瘦高占12%，映射落在
            # 孔洞中，与两壁的交仍不小但全是碎竖片）。沿法向滑动搜索，
            # 评分用"最大单片面积"（墨带整片必胜壁上碎片），取"达最优
            # 80%中位移最小"的位置。
            from shapely.affinity import translate

            def biggestPiece(geom):
                ps = _piecesOf(geom)
                return max((g.area for g in ps), default=0.0)

            ddx = median[-1][0] - median[0][0]
            ddy = median[-1][1] - median[0][1]
            LL = math.hypot(ddx, ddy) or 1.0
            nx, ny = -ddy / LL, ddx / LL
            rb = region.bounds
            span = max(rb[2] - rb[0], rb[3] - rb[1])
            cands = []
            for t in range(-10, 11):
                off = span * 0.5 * t / 10.0
                c2 = corridor if t == 0 else \
                    translate(corridor, xoff=nx * off, yoff=ny * off)
                try:
                    a = biggestPiece(c2.intersection(region))
                except Exception:
                    a = 0.0
                cands.append((a, abs(off), off))
            bestA = max(a for a, _, _ in cands)
            if bestA <= 0:
                continue
            good = min((ab, off) for a, ab, off in cands if a >= bestA * 0.8)
            if abs(good[1]) > 1e-9:
                corridor = translate(corridor, xoff=nx * good[1],
                                     yoff=ny * good[1])
            body = corridor.intersection(region)
            if not body.is_valid:
                body = body.buffer(0)
        except Exception:
            continue
        pieces = _piecesOf(body)
        if pieces:
            # 走廊穿过孔洞/间隙会碎成多片，只留最大片（真身）防 SPLIT
            body = max(pieces, key=lambda g: g.area)
        if body.is_empty or body.area < 25:
            continue
        # 饿死救济要求救济体明显大于现状；轴向病重建则是形状置换，
        # 只要求救济体像样（≥楷体占比预期的1/4）
        if starved and body.area < curArea * 1.5:
            continue
        if misAxis and body.area < expect * 0.25:
            continue
        p = _regionToPath(body)
        if not p:
            continue
        s["path"] = p
        s["failed"] = False
        s["clamped"] = True
        s["rescued"] = True
        rescued.append(i)
        # 救济体所在的墨原先整块判给了邻笔（这正是饿死的原因）——从
        # 侵占邻笔的区域里减掉救济体，把双重归属改回真正的划分。减完
        # 会碎成多片（走廊横穿邻笔）则放弃减除，保留双重归属。
        for j, s2 in enumerate(strokes):
            if j == i or s2["failed"] or s2.get("group") != s.get("group"):
                continue
            r2 = _pathRegion(s2["path"])
            if r2 is None or r2.is_empty:
                continue
            inter = r2.intersection(body)
            if inter.area < body.area * 0.25:
                continue
            cand = r2.difference(body)
            if not cand.is_valid:
                cand = cand.buffer(0)
            big = [g for g in _piecesOf(cand)
                   if g.area >= max(25.0, cand.area * 0.02)]
            if len(big) != 1:
                continue
            p2 = _regionToPath(cand)
            if p2:
                s2["path"] = p2
                s2["clamped"] = True
    return rescued



def resolveKaiDisjointOverlaps(strokes, kaiMedians, ratio=0.6, kaiDist=40.0):
    """楷体不相交笔对的重叠减除：同组两笔在楷体中轴线互不接近（最近
    距离>kaiDist）却切出大面积重叠（>较小者60%）——融合日/目/口的
    封底横被外框整体包含（亨/亭/勤 100%）。把重叠带从**较大**笔减除
    （内横保带、外框让位）；减除后较大笔碎裂则放弃（宁留双重归属）。
    返回修正的笔序号列表。"""
    rs = []
    for m in kaiMedians:
        step = max(1, len(m) // 12)
        rs.append([tuple(p) for p in m[::step]] + [tuple(m[-1])])

    def kaiMin(i, j):
        return min(math.hypot(p[0] - q[0], p[1] - q[1])
                   for p in rs[i] for q in rs[j])

    regions = [None if s["failed"] else _pathRegion(s["path"])
               for s in strokes]
    fixed = []
    for i in range(len(strokes)):
        for j in range(i + 1, len(strokes)):
            if strokes[i]["failed"] or strokes[j]["failed"]:
                continue
            if strokes[i].get("group") != strokes[j].get("group"):
                continue
            ri, rj = regions[i], regions[j]
            if ri is None or rj is None or ri.is_empty or rj.is_empty:
                continue
            if i < len(rs) and j < len(rs) and kaiMin(i, j) <= kaiDist:
                continue
            try:
                inter = ri.intersection(rj)
            except Exception:
                continue
            small = min(ri.area, rj.area)
            if small < 25 or inter.area < ratio * small:
                continue
            big, sml = (i, j) if ri.area >= rj.area else (j, i)
            try:
                cand = regions[big].difference(regions[sml])
                if not cand.is_valid:
                    cand = cand.buffer(0)
            except Exception:
                continue
            pieces = [g for g in _piecesOf(cand)
                      if g.area >= max(25.0, cand.area * 0.02)]
            if len(pieces) > 2 or cand.area < 25:
                continue
            p = _regionToPath(cand)
            if not p:
                continue
            strokes[big]["path"] = p
            strokes[big]["clamped"] = True
            regions[big] = cand
            fixed.append(big)
    return fixed


def fillResidualGaps(contours, strokes, glyph=None, minPiece=25.0):
    """终态补缝：未覆盖残片（≥minPiece）无条件回填给同组共享边界最长
    的笔。与 clampStrokes 的回填同逻辑，但没有 0.5% 总量闸——那是
    校验容差不是跳过闸，0.4% 的缺口曾在爱的冖区留白缝。只该在救济/
    减除/连通性全部尘埃落定后的终态调用（中间轮次调用会把救济前的
    大残差乱粘给邻笔——質12横曾被粘26639）。返回是否有改动。"""
    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return False
    regions = []
    for s in strokes:
        r = None if s["failed"] else _pathRegion(s["path"])
        regions.append(r)
    valid = [r for r in regions if r is not None and not r.is_empty]
    if not valid:
        return False
    union = unary_union(valid)
    if not union.is_valid:
        union = union.buffer(0)
    residual = glyph.difference(union)
    if residual.is_empty or residual.area <= 4.0:
        return False
    changed = False
    pieces = list(residual.geoms) if hasattr(residual, "geoms") else [residual]
    for piece in pieces:
        if piece.area < minPiece:
            continue
        pb = piece.buffer(1.5)
        # 归属组=与残片交叠最大的轮廓组
        pieceG, pgArea = None, 0.0
        for g in sorted({c.get("group", 0) for c in contours}):
            reg = _groupRegionOf(contours, g)
            if reg is None:
                continue
            try:
                a = reg.intersection(piece).area
            except Exception:
                a = 0.0
            if a > pgArea:
                pgArea, pieceG = a, g
        bestI, bestL = -1, 0.0
        for i, r in enumerate(regions):
            if r is None or r.is_empty:
                continue
            if pieceG is not None and strokes[i].get("group") != pieceG:
                continue
            try:
                shared = pb.intersection(r).area
            except Exception:
                shared = 0.0
            if shared > bestL:
                bestL, bestI = shared, i
        if bestI < 0:
            for i, r in enumerate(regions):
                if r is None or r.is_empty:
                    continue
                try:
                    shared = pb.intersection(r).area
                except Exception:
                    shared = 0.0
                if shared > bestL:
                    bestL, bestI = shared, i
        if bestI < 0:
            continue
        merged = regions[bestI].union(piece)
        if not merged.is_valid:
            merged = merged.buffer(0)
        p2 = _regionToPath(merged)
        if p2:
            strokes[bestI]["path"] = p2
            strokes[bestI]["clamped"] = True
            regions[bestI] = merged
            changed = True
    return changed


def enforceConnectivity(contours, strokes, maxRounds=3, glyph=None):
    """单笔单连通终态收口（公理：同一笔画不会断成两个孤立连通组）。
    多片笔画只留最大片，其余显著片按共享边界最长原则划给相邻笔；
    无人接壤的片留回原主（宁可 SPLIT 不丢墨——并集恒等优先）。
    残差回填/邻笔减除等上游环节偶发的断笔在此统一修复。
    返回是否有改动。"""
    regions = [None if s["failed"] else _pathRegion(s["path"])
               for s in strokes]
    dirty = set()
    for _ in range(maxRounds):
        changed = False
        for i in range(len(strokes)):
            r = regions[i]
            if r is None or r.is_empty:
                continue
            big = [g for g in _piecesOf(r) if g.area >= 25.0]
            if len(big) <= 1:
                continue
            big.sort(key=lambda g: -g.area)
            keep = r
            for piece in big[1:]:
                # 独立字体轮廓可以互相叠印：碎片若已由其它笔完整覆盖，
                # 删除重复归属即可，不必跨组搬运。否则門内独立横上的
                # 碎片会被组隔离挡住，在框的两笔之间来回转移。
                covered = [r2 for j, r2 in enumerate(regions)
                           if j != i and r2 is not None and not r2.is_empty
                           and r2.intersects(piece)]
                if covered and piece.difference(unary_union(covered)).area < 1e-6:
                    keep = keep.difference(piece)
                    dirty.add(i)
                    changed = True
                    continue
                # 探测半径放宽到 4：裁剪的数值缝隙曾让残片"无人接壤"而
                # 滞留原主（TC威的竖捺）；仍找不到接壤者则给距离最近的笔
                pb = piece.buffer(4.0)
                bestJ, bestShare = -1, 1.0
                gi = strokes[i].get("group")
                for j, r2 in enumerate(regions):
                    # 图层隔离：面片只捐给同组的笔——跨组捐赠会让直出
                    # 组被外组碎片污染（悬浮部件与外组孔洞共享边界）
                    if j == i or r2 is None or r2.is_empty or                             strokes[j].get("group") != gi:
                        continue
                    try:
                        share = pb.intersection(r2).area
                    except Exception:
                        share = 0.0
                    if share > bestShare:
                        bestShare, bestJ = share, j
                if bestJ < 0:
                    bestD = 1e18
                    for j, r2 in enumerate(regions):
                        if j == i or r2 is None or r2.is_empty or                                 strokes[j].get("group") != gi:
                            continue
                        try:
                            d = piece.distance(r2)
                        except Exception:
                            continue
                        if d < bestD:
                            bestD, bestJ = d, j
                    if bestJ >= 0 and bestD > 15.0:
                        bestJ = -1
                    # 同组全空/全远时的接壤跨组捐赠（勤：竖2残留1936孤儿
                    # 片距同组邻笔178，而他组笔与其 buffer(4) 交286/311——
                    # 融合条带的墨本就属于邻部件）。门控：仅当同组无人
                    # 接壤且他组确有实接壤者；防跨组污染的原禁令针对的
                    # "悬浮部件与外组孔洞共享边界"场景同组也接壤，不入此支
                    if bestJ < 0:
                        bestX, bestXs = -1, 25.0
                        for j, r2 in enumerate(regions):
                            if j == i or r2 is None or r2.is_empty or \
                                    strokes[j].get("group") == gi:
                                continue
                            try:
                                share = pb.intersection(r2).area
                            except Exception:
                                continue
                            if share > bestXs:
                                bestXs, bestX = share, j
                        bestJ = bestX
                if bestJ < 0:
                    continue
                keep = keep.difference(piece)
                if not keep.is_valid:
                    keep = keep.buffer(0)
                merged = regions[bestJ].union(piece)
                if not merged.is_valid:
                    merged = merged.buffer(0)
                regions[bestJ] = merged
                dirty.add(bestJ)
                changed = True
            if changed:
                regions[i] = keep
                dirty.add(i)
        if not changed:
            break
    for k in dirty:
        if regions[k] is None or regions[k].is_empty:
            continue
        p = _regionToPath(regions[k])
        if p:
            strokes[k]["path"] = p
            strokes[k]["clamped"] = True
    return bool(dirty)


def reUnionCheck(contours, strokes, glyph=None):
    """覆盖率/溢出率（shapely 面积精确计算）。"""
    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return {"cover": 0, "excess": 0}
    regions = []
    for s in strokes:
        if s["failed"]:
            continue
        r = _pathRegion(s["path"])
        if r is not None and not r.is_empty:
            regions.append(r)
    if not regions:
        return {"cover": 0, "excess": 0}
    union = unary_union(regions)
    if not union.is_valid:
        union = union.buffer(0)
    cover = union.intersection(glyph).area / glyph.area * 100
    excess = (union.area - union.intersection(glyph).area) / union.area * 100
    return {"cover": round(cover, 1), "excess": round(excess, 1)}


def clampStrokes(contours, strokes, excessTol=0.5, coverTol=99.5, glyph=None):
    """裁剪 + 残差回填。就地修改 strokes 的 path，返回收口后的 unionCheck。"""
    if glyph is None:
        glyph = glyphRegion(contours)
    if glyph is None or glyph.area < 1:
        return {"cover": 0, "excess": 0}

    regions = []
    for s in strokes:
        r = None
        if not s["failed"]:
            r = _pathRegion(s["path"])
        regions.append(r)

    # 1) 裁剪：越界删除；与字形交集为空（整笔落在墨外——退化重构环的
    #    奇偶区域可能整体翻到界外）时置 failed，交由饿死救济重建
    clamped = []
    replaced = [False] * len(strokes)
    for i, r in enumerate(regions):
        if r is None or r.is_empty:
            strokes[i]["path"] = ""
            strokes[i]["failed"] = True
            clamped.append(None)
            continue
        inter = r.intersection(glyph)
        if not inter.is_valid:
            inter = inter.buffer(0)
        if inter.is_empty or inter.area < 4.0:
            strokes[i]["path"] = ""
            strokes[i]["failed"] = True
            clamped.append(None)
            continue
        excess = r.area - inter.area
        if excess > max(4.0, r.area * excessTol / 100):
            replaced[i] = True
        clamped.append(inter)

    # 2) 残差回填：未覆盖面片给共享边界最长的笔。**图层隔离**：残差
    #    面片先按最大交叠定所属连通组，只允许该组的笔认领（悬浮部件
    #    与外组孔洞共享边界，跨组回填会把面片粘给别组的笔——直出组
    #    因此被外组污染）；本组无人接壤才放开全局。
    valid = [c for c in clamped if c is not None and not c.is_empty]
    if valid:
        union = unary_union(valid)
        if not union.is_valid:
            union = union.buffer(0)
        residual = glyph.difference(union)
        if not residual.is_empty and residual.area > max(4.0, glyph.area * (100 - coverTol) / 100):
            groupRegionCache = {}

            def _regionOfGroup(g):
                if g not in groupRegionCache:
                    groupRegionCache[g] = _groupRegionOf(contours, g)
                return groupRegionCache[g]

            allGroups = sorted({c.get("group", 0) for c in contours})
            pieces = list(residual.geoms) if hasattr(residual, "geoms") else [residual]
            for piece in pieces:
                if piece.area < 4:
                    continue
                pieceG, pgArea = None, 0.0
                for g in allGroups:
                    reg = _regionOfGroup(g)
                    if reg is None:
                        continue
                    try:
                        a = reg.intersection(piece).area
                    except Exception:
                        a = 0.0
                    if a > pgArea:
                        pgArea, pieceG = a, g
                pb = piece.buffer(1.5)

                def bestOwner(candidates):
                    bi, bl = -1, 0.0
                    for i in candidates:
                        c = clamped[i]
                        if c is None or c.is_empty:
                            continue
                        try:
                            shared = pb.intersection(c).area
                        except Exception:
                            shared = 0.0
                        if shared > bl:
                            bl, bi = shared, i
                    return bi

                sameG = [i for i in range(len(strokes))
                         if strokes[i].get("group") == pieceG]
                bestI = bestOwner(sameG) if pieceG is not None else -1
                if bestI < 0:
                    bestI = bestOwner(range(len(strokes)))
                if bestI >= 0:
                    merged = clamped[bestI].union(piece)
                    if not merged.is_valid:
                        merged = merged.buffer(0)
                    clamped[bestI] = merged
                    replaced[bestI] = True

    # 3) 仅替换确有问题的笔画路径（其余保留贝塞尔精确切片）
    for i, s in enumerate(strokes):
        if replaced[i] and clamped[i] is not None and not clamped[i].is_empty:
            p = _regionToPath(clamped[i])
            if p:
                # 小数舍入可能把刚好贴着边界的薄片压成往返线。判定
                # 必须针对写出的路径，否则 failed=False 会阻止后续
                # 饿死救济，造成整字并集正常但某一笔为空。
                written = _pathRegion(p)
                if written is None or written.is_empty or written.area < 4.0:
                    s["path"] = ""
                    s["failed"] = True
                    clamped[i] = None
                else:
                    s["path"] = p
                    s["clamped"] = True

    # 4) 收口后校验
    finalRegions = [c for c in clamped if c is not None and not c.is_empty]
    if not finalRegions:
        return {"cover": 0, "excess": 0}
    union = unary_union(finalRegions)
    if not union.is_valid:
        union = union.buffer(0)
    cover = union.intersection(glyph).area / glyph.area * 100
    excess = (union.area - union.intersection(glyph).area) / max(1.0, union.area) * 100
    return {"cover": round(cover, 1), "excess": round(excess, 1)}


# ---------------------------------------------------------------- 形态修复器：交界毛刺置换

def _nonzeroLargest(pathStr, flat=None):
    """切割路径 → nonzero 语义单笔区域最大片（中轴图审计域）。切割路径
    继承字体原始绕向，奇偶合成会在保留片搭接处误挖假孔，Voronoi 中轴
    会被假孔搅出伪分叉——审计域用 nonzero（外环并集减反绕环并集）；
    手术域仍用 _pathRegion（奇偶=reUnionCheck/verify UNION 的消费语义，
    写出路径的区域按此恒等）。语义同 verify.strokeMorphRegion，独立
    实现（verify 与生产代码路径强制隔离，不能互相 import）。"""
    pairs = []
    for c in parseContours(pathStr):
        pts = flattenSegs(c["segs"], flat or _FLAT)
        if len(pts) < 4:
            continue
        try:
            pg = Polygon(pts)
            if not pg.is_valid:
                pg = pg.buffer(0)
            if not pg.is_empty:
                pairs.append((pg, signedArea(pts)))
        except Exception:
            pass
    if not pairs:
        return None
    outerSign = 1.0 if max(pairs, key=lambda t: abs(t[1]))[1] >= 0 else -1.0
    outers = [pg for pg, a in pairs if a * outerSign >= 0]
    holes = [pg for pg, a in pairs if a * outerSign < 0]
    try:
        region = unary_union(outers)
        if holes:
            region = region.difference(unary_union(holes))
    except Exception:
        return None
    if not region.is_valid:
        region = region.buffer(0)
    pieces = _piecesOf(region)
    if not pieces:
        return None
    region = max(pieces, key=lambda g: g.area)
    return region if region.area >= 25.0 else None


def _walkLeafChain(adj, leaf):
    """度1叶沿度≤2链走到头 → (链节点表, 链长, 挂点交叉节点|None)。
    与 geometry.medialJunctions / verify._morphWalkLeaf 同法——三处
    阈值语义不同（全字级120 / verify 单笔审计 / 本处修复域），且
    verify 与生产隔离不能互相 import，各自独立持有。"""
    chain = [leaf]
    chainLen = 0.0
    cur, prev = leaf, None
    while True:
        nbrs = [(v, w) for v, w in adj[cur].items() if v != prev]
        if not nbrs:
            return chain, chainLen, None
        v, w = nbrs[0]
        chainLen += w
        prev, cur = cur, v
        if len(adj[cur]) != 2:
            break
        chain.append(cur)
    return chain, chainLen, (cur if len(adj[cur]) >= 3 else None)


def _dropLeafChain(adj, chain):
    """从无向图 adj 中整链摘除（含挂边）。"""
    for node in chain:
        for v in list(adj.get(node, {})):
            adj[v].pop(node, None)
        adj.pop(node, None)


def _morphScan(region):
    """单笔中轴图形态勘察 → (交叉节点数, 短叶枝记录表, 伪臂候选表)
    | None。剪枝口径照 verify.morphAuditStroke（链长 < max(2×挂点余隙,
    40)）；剪稳后剩余图里 链长<MORPH_ARM_MAX 的叶链是伪臂候选（交界
    粘连尾巴长于笔宽、逃过 spur 阈，正是 verify 注释里"保留计入交叉
    节点"的形态病本体）。交叉节点数/短叶枝数与 verify 审计同口径，
    伪臂只作修复候选不进指标。"""
    adj = _medialAdjacency(region)
    if not adj:
        return None
    adj = {u: dict(vs) for u, vs in adj.items()}
    rings = [region.exterior] + list(region.interiors)

    def clearance(p):
        pt = Point(p)
        return min(r.distance(pt) for r in rings)

    spurs = []
    changed = True
    while changed:
        changed = False
        for leaf in [u for u, vs in adj.items() if len(vs) == 1]:
            if leaf not in adj or len(adj[leaf]) != 1:
                continue
            chain, chainLen, junc = _walkLeafChain(adj, leaf)
            if junc is None:
                continue
            if chainLen < max(2.0 * clearance(junc), MORPH_SPUR_FLOOR):
                spurs.append({"tip": leaf, "chain": chain,
                              "clrs": [clearance(p) for p in chain],
                              "len": chainLen})
                _dropLeafChain(adj, chain)
                changed = True
    arms = []
    for leaf in [u for u, vs in adj.items() if len(vs) == 1]:
        chain, chainLen, junc = _walkLeafChain(adj, leaf)
        if junc is None or chainLen >= MORPH_ARM_MAX:
            continue
        arms.append({"tip": leaf, "chain": chain,
                     "clrs": [clearance(p) for p in chain], "len": chainLen})
    juncN = sum(1 for vs in adj.values() if len(vs) >= 3)
    return juncN, spurs, arms


def _burrPatch(cur, spur):
    """毛刺补丁提取：叶枝沿链逐点圆盘（半径=2×该点余隙）并集 ∩ 本笔
    当前区域，取含链末端的那片。末端单圆盘不够——交界毛刺常是沿邻笔
    缝隙下垂的长条楔（永 横折钩沿捺左flank 187 长、楔尖余隙仅 0.4-3），
    补丁须罩住整条走廊且半径须随局部余隙走。两处几何卫生（永 标定
    实测）：①从末端起取**连续前缀**，余隙陡升处截断（>max(2×链余隙
    中位, 6)=已进交界肥厚区，散点保留会留缺口）；②圆盘并集先做闭运算
    （+r/−r 缓冲）再求交——不同半径圆盘拼接的扇贝形切口会让重扫
    Voronoi 喷出几十条假短叶枝，改善守卫恒否决。"""
    chain, clrs = spur["chain"], spur["clrs"]
    med = sorted(clrs)[len(clrs) // 2]
    cap = max(2.0 * med, 6.0)
    pref = []
    for p, c in zip(chain, clrs):
        if c > cap and len(pref) >= 2:
            break
        pref.append((p, c))
    disks = [Point(p).buffer(2.0 * max(c, 1.5), 8) for p, c in pref[::2]]
    disks.append(Point(pref[-1][0]).buffer(2.0 * max(pref[-1][1], 1.5), 8))
    try:
        zone = unary_union(disks)
        s = 2.0 * cap
        zone = zone.buffer(s, 8).buffer(-s, 8)
        patch = zone.intersection(cur)
    except Exception:
        return None
    pieces = _piecesOf(patch)
    if not pieces:
        return None
    pt = Point(spur["tip"])
    for pc in pieces:
        if pc.contains(pt) or pc.distance(pt) < 0.2:
            return pc
    return max(pieces, key=lambda g: g.area)


def _seamContact(patch, other):
    """补丁边界落在邻笔 tol 邻域内的长度。切割缝两侧路径各自细分并
    0.1 舍入，精确 boundary∩boundary 会漏，按膨胀带口径量。"""
    if other is None or other.is_empty:
        return 0.0
    try:
        if patch.distance(other) > MORPH_SEAM_TOL:
            return 0.0
        return patch.boundary.intersection(other.buffer(MORPH_SEAM_TOL)).length
    except Exception:
        return 0.0


def _bigPieces(region, refArea):
    return [g for g in _piecesOf(region)
            if g.area >= max(25.0, refArea * 0.02)]


def _scanScore(region):
    """区域最大片的 (交叉+短叶枝) 合计分 | None。受让方传染守卫用。"""
    main = max(_piecesOf(region), key=lambda g: g.area, default=None)
    if main is None:
        return None
    scan = _morphScan(main)
    if not scan:
        return None
    return scan[0] + len(scan[1])


def _overlapRatio(a, b):
    """两区域重叠比（交集/较小者面积，verify OVERLAP 同口径）。"""
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    try:
        return a.intersection(b).area / max(1.0, min(a.area, b.area))
    except Exception:
        return 0.0


def _overlapGuardBad(regions, newRegions):
    """置换后 OVERLAP 越线检查：改动笔与**任意**其他笔（不限同组——
    蝮 的越线对 9-10 正是跨组双重归属对，同组窗口曾漏放）的重叠比
    不得新越 MORPH_OVL_CAP（置换前已越线的存量病不追责）。
    newRegions={笔:新区域}。返回 True=有越线（整包否决）。"""
    for x in newRegions:
        a = newRegions[x]
        if a is None or a.is_empty:
            continue
        for y in range(len(regions)):
            if y == x:
                continue
            b = newRegions.get(y, regions[y])
            if b is None or b.is_empty:
                continue
            ba, bb = a.bounds, b.bounds
            if ba[2] < bb[0] or bb[2] < ba[0] or \
                    ba[3] < bb[1] or bb[3] < ba[1]:
                continue
            after = _overlapRatio(a, b)
            if after <= MORPH_OVL_CAP:
                continue
            before = _overlapRatio(regions[x], regions[y])
            if after > before + 0.01:
                return True
    return False


def _repairOneStroke(strokes, regions, k, mates):
    """单笔毛刺勘察+试置换（不落盘）→ (新本笔区域, {受让笔:[补丁并,
    数,并后区域]}, 守卫上下文) | None。置换判据：补丁与邻笔的边界接触
    长度 > 补丁挂回本笔主体的颈宽（圆弧割线长，偏保守）——"挂在别人
    身上的肉"才转让。守卫：①补丁单块/合计面积上限（retain 面积化口
    径）；②逐补丁不破单连通；③置换后本笔中轴图指标净改善且交叉不升
    +最小收益门；④受让方传染守卫——重病笔（形态分>MORPH_RECV_SICK）
    不受让 + 受让侧恶化合计≤2×本笔改善量+2（永 受让差贴界实测）；
    ⑤OVERLAP 越线守卫。写出域终验在 _commitRepair（donor 舍入漂移
    复核，前值按 verify 口径）。"""
    own = regions[k]
    big0 = _bigPieces(own, own.area)
    if not big0:
        return None
    main0 = max(big0, key=lambda g: g.area)
    audit = _nonzeroLargest(strokes[k]["path"])
    if audit is None:
        return None
    # 域一致性门：审计域（nonzero）与手术域（奇偶）几何不一致的笔跳过
    # ——保留片搭接被奇偶误挖假孔时手术写出会把假孔实体化。
    try:
        if audit.symmetric_difference(main0).area > \
                max(1.0, own.area * 0.002) or \
                len(main0.interiors) != len(audit.interiors) or \
                main0.exterior.length > audit.exterior.length * 1.02 + 4 or \
                audit.exterior.length > main0.exterior.length * 1.02 + 4:
            return None
    except Exception:
        return None
    scan0 = _morphScan(main0)
    if not scan0:
        return None
    junc0, spurs0, arms0 = scan0
    if not spurs0 and not arms0:
        return None
    # verify 口径前值参照（IOU_FLAT=4.0）：曲线路径在 3.0/4.0 两种展平
    # 下的中轴拓扑可以天差地别（SC 筻 #9 退化缎带 164 vs 268，几何量
    # 全同不可检出）——写出材料化后 verify 只认 4.0 口径的前值；两口
    # 径拓扑分叉超阈=该笔中轴不可信，弃修。
    auditV = _nonzeroLargest(strokes[k]["path"], MORPH_VFLAT)
    scanV = _morphScan(auditV) if auditV is not None else None
    if not scanV or abs(scanV[0] - junc0) > 2 or \
            abs(len(scanV[1]) - len(spurs0)) > 3:
        return None
    # 执行域门：只修交叉节点型病笔（junc0≥1）。spur-only 笔（衬线锯齿
    # 底噪）的批量搬运在标定中净害——漱 spur 267→297、攮 morphBad
    # 2→4(码型迁移)、健康集 筻 经"置换→种子→自洽二遍"级联 retain
    # -0.027；而毛刺/枝桠必挂交叉点（伪臂按定义挂在交叉点上，junc0=0
    # 时 arms 恒空，标定 13 个 donor 行 11/11 实测），锯齿型不在射程。
    if junc0 < 1:
        return None
    cur = own
    gains = {}
    recvScore = {}      # 受让候选 verify 口径前值分（惰性，None=不可受让）
    for sp in spurs0 + arms0:
        patch = _burrPatch(cur, sp)
        if patch is None or patch.area < 9.0 or \
                patch.area > own.area * MORPH_PATCH_MAX:
            continue
        try:
            rest = cur.difference(patch)
            if not rest.is_valid:
                rest = rest.buffer(0)
            # 单连通阈基须用缩水后的面积——verify SPLIT 的显著片阈是
            # 2%×当前面积，donor 连捐后分母变小，按原面积算会漏掉
            # "捐完后才显著"的碎片（simhei 酹 #2 捐 8 补丁后 2 片、
            # simsun 嫌 #6 同型，SPLIT 回退实证）
            if len(_bigPieces(rest, rest.area)) != \
                    len(_bigPieces(cur, cur.area)):
                continue
            neck = patch.boundary.intersection(rest).length
        except Exception:
            continue
        bestJ, bestC = -1, max(neck, MORPH_CONTACT_MIN)
        for j in mates:
            if regions[j] is None or \
                    patch.area > regions[j].area * MORPH_RECV_MAX:
                continue
            if j not in recvScore:
                recvScore[j] = _recvBaseline(strokes, regions, j)
            if recvScore[j] is None or recvScore[j] > MORPH_RECV_SICK:
                continue
            c = _seamContact(patch, regions[j])
            if c > bestC:
                bestC, bestJ = c, j
        if bestJ < 0:
            continue
        cur = rest
        if bestJ in gains:
            gains[bestJ][0] = gains[bestJ][0].union(patch)
            gains[bestJ][1] += 1
        else:
            gains[bestJ] = [patch, 1]
    if not gains or own.area - cur.area > own.area * MORPH_SHARE_MAX:
        return None
    mainNew = max(_piecesOf(cur), key=lambda g: g.area, default=None)
    if mainNew is None:
        return None
    scan2 = _morphScan(mainNew)
    if not scan2:
        return None
    junc2, spurs2, _arms2 = scan2
    # 改善守卫：交叉节点不得上升，交叉+短叶枝合计必须严格下降（合计
    # 门允许切口处冒出个别新短叶枝，但只在交叉下降更多时放行——永
    # 横折钩实测 junc 7→0、spur 15→17，合计 22→17）；最小收益门拦
    # 噪声级搬运（赢家 永 5/simhei草 15，噪声 我 1/婕 3）。
    if junc2 > junc0 or junc2 + len(spurs2) >= junc0 + len(spurs0):
        return None
    donorGain = (junc0 + len(spurs0)) - (junc2 + len(spurs2))
    if donorGain < MORPH_MIN_GAIN:
        return None
    # 受让方传染守卫：受让侧恶化合计 ≤ 2×本笔改善+2（前值 verify 口径）。
    recvDelta = 0
    for j, entry in gains.items():
        m2 = regions[j].union(entry[0])
        if not m2.is_valid:
            m2 = m2.buffer(0)
        after = _scanScore(m2)
        if after is None:
            return None
        recvDelta += after - (recvScore.get(j) or 0)
        entry.append(m2)     # 平滑并后受让区域随包带出，commit 免重算
    if recvDelta > 2 * donorGain + 2:
        return None
    # OVERLAP 越线守卫：donor 缩身/受让方增肥都可能把同组笔对重叠比
    # 顶过 verify OVERLAP 门（蝮/鄱 sample958 实证，见 MORPH_OVL_CAP 注）
    newRegions = {k: cur}
    for j, entry in gains.items():
        newRegions[j] = entry[2]
    if _overlapGuardBad(regions, newRegions):
        return None
    guard = {"junc0": scanV[0], "donor0": scanV[0] + len(scanV[1]),
             "donorGain": donorGain,
             "recvBefore": {j: (recvScore.get(j) or 0) for j in gains}}
    return cur, gains, guard


def _recvBaseline(strokes, regions, j):
    """受让候选 j 的 verify 口径前值形态分 | None（不可受让）。域门：
    手术域（奇偶@3）与审计域几何一致 + 手术域/verify 口径（nonzero@4）
    形态分一致（发丝假孔/自叠材料化会让 verify 审计在受让笔上爆炸——
    磯 junc 35→114、筻 164→268 教训）。"""
    mJ = max(_piecesOf(regions[j]), key=lambda g: g.area, default=None)
    aJ = _nonzeroLargest(strokes[j]["path"])
    if mJ is None or aJ is None:
        return None
    try:
        if len(mJ.interiors) != len(aJ.interiors) or \
                mJ.exterior.length > aJ.exterior.length * 1.02 + 4 or \
                aJ.exterior.length > mJ.exterior.length * 1.02 + 4 or \
                aJ.symmetric_difference(mJ).area > \
                max(1.0, regions[j].area * 0.002):
            return None
    except Exception:
        return None
    sM = _scanScore(regions[j])
    aJ4 = _nonzeroLargest(strokes[j]["path"], MORPH_VFLAT)
    sV = _scanScore(aJ4) if aJ4 is not None else None
    if sM is None or sV is None or abs(sM - sV) > 4:
        return None
    return sV


def _axisDriftBad(strokes, x, newPath):
    """横/竖笔手术写出后的轴向漂移守卫（verify TYPE 门 32° 口径）：
    轮廓 PCA 主轴不得从门内恶化到门外（simsun 蚶 #2横 捐 5 补丁后轴
    偏 82° 回退实证）。donor 置换与受害者取回双方共用。"""
    if strokes[x].get("type") not in ("横", "竖"):
        return False
    d1 = shapeDescriptor([newPath])
    if not d1 or d1["elong"] < 1.8:
        return False
    canon = 0.0 if strokes[x]["type"] == "横" else 90.0
    a1 = math.degrees(d1["mainAngle"]) % 180.0
    dev1 = min(abs(a1 - canon), 180.0 - abs(a1 - canon))
    # 无前值基线（旧路径 elong<1.8，verify 对它免检）时按 0 处理：
    # 从"免检"长成"受检且门外"同属门外恶化——SC 顰 受让竖 术前矮胖
    # 免检、并肉后 elong 跨 1.8 且轴偏 57°，曾借 dev0=dev1 的默认值
    # 漏过本守卫翻 TYPE（sample958 回退实证）
    dev0 = 0.0
    d0 = shapeDescriptor([strokes[x]["path"]])
    if d0 and d0["elong"] >= 1.8:
        a0 = math.degrees(d0["mainAngle"]) % 180.0
        dev0 = min(abs(a0 - canon), 180.0 - abs(a0 - canon))
    return dev1 > 32.0 and dev1 > dev0 + 1.0


def _regionToPathFine(region):
    """区域→路径（0.01 精度，毛刺置换专用写手）。共享写手 _regionToPath
    的 0.1 舍入会给边界注入幅度≈0.05 的锯齿——恰与 _medialAdjacency 的
    -0.05 内腐蚀同量级，约半数微枝存活成假短叶枝（永 donor 写出域 spur
    17→29 实测）；0.01 舍入幅度 0.005 全部沉入腐蚀带。只在置换 commit
    使用，不动共享写手（改它会挪动全部收口路径，parity 硬门）。"""
    parts = []
    for g in _piecesOf(region):
        if g.area < 4:
            continue
        for ring in [g.exterior] + list(g.interiors):
            coords = list(ring.coords)
            if len(coords) < 4:
                continue
            d = "M %.2f %.2f " % coords[0][:2]
            d += " ".join("L %.2f %.2f" % c[:2] for c in coords[1:-1])
            parts.append(d + " Z")
    return " ".join(parts)


def _commitRepair(strokes, regions, k, trial):
    """置换落盘（整包 all-or-nothing）：受让侧试并+双侧路径重建+写出域
    守卫复核全部通过才写。写出域复核：守卫在全精度区域上评估，写出
    路径经舍入回读后形态分会漂移——按同一套判据（本笔 junc 不升+合计
    严格降+最小收益+受让传染界）对写出域再验一遍，不过整包回滚。受让
    笔不得新增显著碎片（补丁与邻笔只是 0.6 带内接触、实际隔缝时并集
    会成飞地——那是 SPLIT，整笔放弃）。返回 [[笔, 补丁数, 受让笔], ...]
    （失败返回 []，一切未动）。"""
    cur, gains, guard = trial
    merged, newPaths = {}, {}
    for j, (patch, _n, m2) in gains.items():
        p2 = _regionToPathFine(m2)
        if not p2 or len(_bigPieces(m2, m2.area)) > \
                len(_bigPieces(regions[j], regions[j].area)):
            return []
        merged[j], newPaths[j] = m2, p2
    pK = _regionToPathFine(cur)
    if not pK:
        return []
    written = _pathRegion(pK)
    if written is None or written.is_empty or written.area < 4.0:
        return []
    # 写出域单连通终验（verify SPLIT 口径：显著片阈 2%×终态面积）
    if len(_bigPieces(written, written.area)) != \
            len(_bigPieces(regions[k], regions[k].area)):
        return []
    # donor 轴向守卫：横/竖 donor 连捐后轮廓 PCA 主轴被拉歪会翻 verify
    # TYPE（simsun 蚶 #2横 捐 5 补丁后轴偏 82° 回退实证）——写出域
    # 轴向不得从门内恶化到门外（32°=verify TYPE 门；教条轴口径偏严，
    # 误拦方向安全）
    if _axisDriftBad(strokes, k, pK):
        return []
    # 写出域守卫复核（domain=verify 消费的真实路径）
    mainW = max(_piecesOf(written), key=lambda g: g.area, default=None)
    scanW = _morphScan(mainW) if mainW is not None else None
    if not scanW:
        return []
    juncW, spursW, _armsW = scanW
    # 写出域判据比全精度域各让一手：0.01 舍入仍磨掉 1-2 个短叶枝
    # （永 donor 全精度 gain 5、写出域 gain 4 实测）——MIN_GAIN 在
    # 全精度域把关（标定域），写出域只要求严格净改善（交叉不升+合计
    # 严格降）；传染界用全精度收益定标（写出域收益被舍入系统性压低）。
    if juncW > guard["junc0"] or \
            juncW + len(spursW) >= guard["donor0"]:
        return []
    strokes[k]["path"] = pK
    strokes[k]["clamped"] = True
    regions[k] = cur
    out = []
    for j, (patch, n, _m2) in gains.items():
        strokes[j]["path"] = newPaths[j]
        strokes[j]["clamped"] = True
        regions[j] = merged[j]
        out.append([k, n, j])
    return out


def _ringClearance(rings, p):
    """点到区域边界环组的最小距离（中轴点局部余隙）。"""
    pt = Point(p)
    return min(r.distance(pt) for r in rings)


def _terminalChains(main):
    """剪稳后为纯路径图的笔 → (终端链表, 边界环组) | None。取回执行域
    与 donor 置换互补的裁定：剪掉短叶枝后仍有交叉节点=病笔（donor 域，
    spur/arm 框架射程内），纯路径图的**主链末端**才是取回目标——威·撇
    的楔在剪稳图上就是链尾（junc=None），donor 框架按定义不可见。"""
    adj = _medialAdjacency(main)
    if not adj:
        return None
    adj = {u: dict(vs) for u, vs in adj.items()}
    rings = [main.exterior] + list(main.interiors)
    changed = True
    while changed:                      # 短叶枝剪除照 _morphScan 口径
        changed = False
        for leaf in [u for u, vs in adj.items() if len(vs) == 1]:
            if leaf not in adj or len(adj[leaf]) != 1:
                continue
            chain, chainLen, junc = _walkLeafChain(adj, leaf)
            if junc is None:
                continue
            if chainLen < max(2.0 * _ringClearance(rings, junc),
                              MORPH_SPUR_FLOOR):
                _dropLeafChain(adj, chain)
                changed = True
    if any(len(vs) >= 3 for vs in adj.values()):
        return None
    chains = []
    for leaf in [u for u, vs in adj.items() if len(vs) == 1]:
        chain, _len, junc = _walkLeafChain(adj, leaf)
        if junc is None and len(chain) >= 4:
            chains.append(chain)
    return (chains, rings) if chains else None


def _reclaimPrefix(chain, rings, vict):
    """终端链的受害者贴壁前缀 → (切点下标, 切点余隙, 贴壁长) | None。
    贴壁=中轴点到受害者的距离≈自身余隙（受害者边界就是本笔此段的一面
    墙，悬肉沿切割缝生长的几何本质）；前缀吞到链尾=整笔贴着受害者，
    不是悬肉是本体，弃。"""
    hugLen = 0.0
    n = 0
    prev = None
    for p in chain:
        c = _ringClearance(rings, p)
        if vict.distance(Point(p)) > c * MORPH_RECLAIM_HUG + 2.0:
            break
        if prev is not None:
            hugLen += math.hypot(p[0] - prev[0], p[1] - prev[1])
        prev = p
        n += 1
    if n < 3 or n >= len(chain) - 1:
        return None
    cSplit = _ringClearance(rings, chain[n])
    if hugLen < max(1.5 * cSplit, 25.0):
        return None                     # 贴壁比自身还短=丁字自然对接
    return n, cSplit, hugLen


def _bladeCut(own, chain, iSplit, cSplit):
    """沿链切点垂直截断 → (含链首侧补丁, 剩余) | None。直刀而非圆盘并
    集减除：中轴重建圆弧切口经 0.01 写手+重展平后每道弦折成一根假短
    叶枝（威·撇 写出域 spur 2→16 实测），弦切颈也比弧割线短（85→58），
    判据更锋利。0.7 宽刀条留在剩余侧，并集恒等保持。"""
    p = chain[iSplit]
    a = chain[max(iSplit - 2, 0)]
    b = chain[min(iSplit + 2, len(chain) - 1)]
    vx, vy = b[0] - a[0], b[1] - a[1]
    nrm = math.hypot(vx, vy)
    if nrm < 1e-6:
        return None
    half = 2.5 * cSplit + 6.0
    px, py = -vy / nrm * half, vx / nrm * half
    blade = LineString([(p[0] - px, p[1] - py),
                        (p[0] + px, p[1] + py)]).buffer(0.35, 8)
    try:
        cut = own.difference(blade)
        if not cut.is_valid:
            cut = cut.buffer(0)
    except Exception:
        return None
    pieces = [g for g in _piecesOf(cut) if g.area >= 25.0]
    if len(pieces) < 2:
        return None                     # 刀没切透（截面比刀长还宽）
    tip = Point(chain[0])
    patch = min(pieces, key=lambda g: g.distance(tip))
    if patch.distance(tip) > 2.0 or patch.area >= own.area * 0.5:
        return None
    rest = own.difference(patch)
    if not rest.is_valid:
        rest = rest.buffer(0)
    return patch, rest


def _reclaimTrial(strokes, regions, k, mates):
    """受害者 k 的取回勘察（不落盘）→ trial dict | None。判据与 donor
    置换同一张："挂在别人身上的肉"（补丁与受害者接触长度>切颈），方向
    相反：肉长在健康邻笔的中轴终端链上，受害者主导取回。守卫：缺口
    预算门（补丁≤1.5×缺口——只补被切走的，不抢邻笔本体）、双侧域一致
    +verify 口径前值（_recvBaseline 复用）、双侧单连通、双侧形态分不
    恶化（全精度）、OVERLAP 越线。写出域复核在 _commitReclaim。"""
    own = regions[k]
    retain0 = strokes[k].get("retainRatio") or 1.0
    budget = own.area * (1.0 / max(retain0, 0.05) - 1.0)
    if budget < MORPH_RECLAIM_MIN:
        return None
    preK = _recvBaseline(strokes, regions, k)
    if preK is None or preK > MORPH_RECV_SICK:
        return None
    scanK0 = _scanScore(own)
    if scanK0 is None:
        return None
    for j in mates:
        if regions[j].distance(own) > MORPH_SEAM_TOL:
            continue                    # 无缝接触=不可能有贴缝悬肉
        preJ = _recvBaseline(strokes, regions, j)
        if preJ is None or preJ > MORPH_RECV_SICK:
            continue
        mainJ = max(_piecesOf(regions[j]), key=lambda g: g.area)
        tc = _terminalChains(mainJ)
        if not tc:
            continue
        chains, rings = tc
        for chain in chains:
            pref = _reclaimPrefix(chain, rings, own)
            if pref is None:
                continue
            iSplit, cSplit, _hug = pref
            bc = _bladeCut(regions[j], chain, iSplit, cSplit)
            if bc is None:
                continue
            patch, rest = bc
            if patch.area < MORPH_RECLAIM_MIN or \
                    patch.area > budget * MORPH_RECLAIM_BUDGET or \
                    patch.area > regions[j].area * MORPH_PATCH_MAX or \
                    patch.area > own.area * MORPH_RECV_MAX:
                continue
            if len(_bigPieces(rest, rest.area)) != \
                    len(_bigPieces(regions[j], regions[j].area)):
                continue
            try:
                neck = patch.boundary.intersection(rest).length
            except Exception:
                continue
            contact = _seamContact(patch, own)
            if contact <= max(neck * MORPH_RECLAIM_MARGIN,
                              MORPH_CONTACT_MIN):
                continue
            uni = own.union(patch)
            if not uni.is_valid:
                uni = uni.buffer(0)
            if len(_piecesOf(uni)) > len(_piecesOf(own)):
                continue                # 带内接触实际隔缝=飞地，弃
            # 治愈双门：增肉按并集口径（补丁与本笔双重归属带不算肉）
            gain = uni.area - own.area
            if gain < budget * MORPH_RECLAIM_FILL or \
                    retain0 * uni.area / own.area < MORPH_RECLAIM_HEAL:
                continue
            sRest = _scanScore(rest)
            sUni = _scanScore(uni)
            sJ0 = _scanScore(regions[j])
            if sRest is None or sUni is None or sJ0 is None or \
                    sRest > sJ0 + 1 or sUni > scanK0 + 3:
                continue                # 威 实测：撇 2→2、横 2→3(起笔护尾)
            if _overlapGuardBad(regions, {k: uni, j: rest}):
                continue
            return {"j": j, "uni": uni, "rest": rest,
                    "preK": preK, "preJ": preJ}
    return None


def _commitReclaim(strokes, regions, k, trial):
    """取回落盘（整包 all-or-nothing）：双侧 0.01 写手重建+写出域复核
    （单连通/轴向/形态分≤verify 口径前值+4）全过才写。retainRatio 按
    面积比改写双侧——取回的存在意义就是修 retain（verify m.retain 直
    读该字段，威笔0 0.887→0.963 才可对账），donor 置换不改（补丁是
    毛边量级，改写反而放大 r2/r3 采纳噪声）。返回 [[捐方,1,受方]]|[]。"""
    j, uni, rest = trial["j"], trial["uni"], trial["rest"]
    pK = _regionToPathFine(uni)
    pJ = _regionToPathFine(rest)
    if not pK or not pJ:
        return []
    wK, wJ = _pathRegion(pK), _pathRegion(pJ)
    if wK is None or wK.is_empty or wJ is None or wJ.is_empty:
        return []
    if len(_bigPieces(wK, wK.area)) > \
            len(_bigPieces(regions[k], regions[k].area)) or \
            len(_bigPieces(wJ, wJ.area)) != \
            len(_bigPieces(regions[j], regions[j].area)):
        return []
    if _axisDriftBad(strokes, k, pK) or _axisDriftBad(strokes, j, pJ):
        return []
    sWK, sWJ = _scanScore(wK), _scanScore(wJ)
    if sWK is None or sWJ is None or \
            sWK > trial["preK"] + 4 or sWJ > trial["preJ"] + 4:
        return []
    for x, region, path in ((k, uni, pK), (j, rest, pJ)):
        r0 = strokes[x].get("retainRatio")
        if r0 and regions[x].area > 1.0:
            strokes[x]["retainRatio"] = round(
                min(1.0, r0 * region.area / regions[x].area), 4)
        strokes[x]["path"] = path
        strokes[x]["clamped"] = True
        regions[x] = region
    return [[j, 1, k]]


def morphRepairStrokes(strokes):
    """交界毛刺置换（形态修复器，MORPH_REPAIR 开关）：衬线体交叉组
    切割后交界带留下的毛边/枝桠（simsun 永 横折钩 retain0.82+2桥）是
    "挂在别人身上的肉"——短叶枝末端的轮廓局部凸起若与某同组邻笔的
    边界接触长度大于其挂回本笔主体的颈宽，则整块转让给该邻笔。转让
    是 difference/union 对偶（补丁只在两笔间换主），并集恒等天然保持。
    挂点=sealUnion 收口链末端，只做交界再划界；级联外溢由 finalize
    的采纳否决（_morphAdoptVeto）兜底。反向执行域=受害者取回（威·笔0
    型：肉长在健康邻笔的中轴终端链上，donor 框架不可见），同一张判据
    受害者主导，donor 置换优先、取回殿后。受让过肉的笔
    本轮禁止再当 donor——永 曾出现 #1→#4 转让后 #4 借新得的交叉点
    反向 #4→#1 倒手（ping-pong），两轮"局部改善"叠加成 verify 全字
    junc 7→14 净恶化；取回同理：捐过肉的笔禁再当受害者、received 笔
    禁当取回捐方（威 撇 retain 被改写后落进受害者门，不拦会倒手）。
    返回 [[笔, 补丁数, 受让笔], ...]。"""
    repairs = []
    regions = [None if s["failed"] else _pathRegion(s["path"])
               for s in strokes]
    groupIdx = {}
    for i, s in enumerate(strokes):
        if not s["failed"] and regions[i] is not None \
                and not regions[i].is_empty:
            groupIdx.setdefault(s.get("group"), []).append(i)
    received = set()
    donated = set()
    for k, s in enumerate(strokes):
        if k in received or s["failed"] or regions[k] is None or \
                regions[k].is_empty or regions[k].area < 100.0:
            continue
        mates = [j for j in groupIdx.get(s.get("group"), []) if j != k]
        if not mates:
            continue    # 无同组邻笔=无受让方，免付 Voronoi
        if s.get("retainRatio", 1.0) < MORPH_RETAIN_GATE or \
                (s.get("bridges") or 0) >= MORPH_BRIDGE_GATE:
            trial = _repairOneStroke(strokes, regions, k, mates)
            if trial is not None:
                done = _commitRepair(strokes, regions, k, trial)
                if done:
                    repairs.extend(done)
                    received.update(entry[2] for entry in done)
                    continue
        if s.get("retainRatio", 1.0) < MORPH_RECLAIM_RETAIN and \
                k not in donated:
            rec = _reclaimTrial(strokes, regions, k,
                                [j for j in mates if j not in received])
            if rec is not None:
                done = _commitReclaim(strokes, regions, k, rec)
                if done:
                    repairs.extend(done)
                    received.add(k)
                    donated.add(rec["j"])
    return repairs
