# -*- coding: utf-8 -*-
"""strokelab.fonthub — 目标字体解析（fontTools）、B库建库、A↔B 骨架映射。"""

import math
import os
import json
import hashlib

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen

from .geometry import (lineSeg, cubicSeg, dist, parseContours, contourToPath,
                       flattenSegs, bboxOfPoints, analyzeContours,
                       nearestOnPolyline, segLength, bezPoint,
                       shapeDescriptor, shapeSimilarity, refineMedianFit,
                       straightenSections, midpointRectify, outlineCenterline,
                       resamplePolyline)
from .classify import (PROBE_TABLE, TYPE_ORDER, CJK_STROKE_NAMES,
                       CJK_STROKE_ABBR, typeOfStroke, matchTier, findLibEntry,
                       parseProbes, PROBE_POSITIONS, similarTypes,
                       KAI_TARGET_MAP)


def _resolveProbeStroke(dataHub, ch, t, pos):
    """位置词 → 笔序：在该字楷体笔画中先按类型过滤（本类型或相似组），
    再取中轴线质心最靠近指定方位（归一化九宫格）的那笔。"""
    g = dataHub.geom(ch)
    if not g or not g.get("medians"):
        return None
    medians = g["medians"]
    pts = [p for m in medians for p in m]
    x0 = min(p[0] for p in pts)
    x1 = max(p[0] for p in pts)
    y0 = min(p[1] for p in pts)
    y1 = max(p[1] for p in pts)
    w = max(1.0, x1 - x0)
    h = max(1.0, y1 - y0)
    tx, ty = PROBE_POSITIONS[pos]
    sim = set(similarTypes(t))

    def normCenter(m):
        cx = sum(p[0] for p in m) / len(m)
        cy = sum(p[1] for p in m) / len(m)
        return ((cx - x0) / w, (cy - y0) / h)

    cand = []
    for k in range(len(medians)):
        tk = typeOfStroke(ch, k, medians)
        if matchTier(tk, t) or tk in sim:
            cand.append(k)
    if not cand:
        cand = list(range(len(medians)))
    return min(cand, key=lambda k: (normCenter(medians[k])[0] - tx) ** 2 +
               (normCenter(medians[k])[1] - ty) ** 2)

_ALGO_SIG = None


def _algoSignature():
    """算法签名：核心源码内容哈希——算法一变缓存自动失效。
    pipeline 已包化（strokelab/pipeline/*.py），逐文件按名排序纳入。"""
    global _ALGO_SIG
    if _ALGO_SIG is None:
        h = hashlib.md5()
        pkg = os.path.dirname(os.path.abspath(__file__))
        srcFiles = []
        pipeDir = os.path.join(pkg, "pipeline")
        if os.path.isdir(pipeDir):
            srcFiles += [os.path.join(pipeDir, n)
                         for n in sorted(os.listdir(pipeDir))
                         if n.endswith(".py")]
        else:
            srcFiles.append(os.path.join(pkg, "pipeline.py"))
        srcFiles += [os.path.join(pkg, n)
                     for n in ("geometry.py", "classify.py",
                               "boolean.py", "fonthub.py")]
        for path in srcFiles:
            try:
                with open(path, "rb") as f:
                    h.update(f.read())
            except OSError:
                pass
        _ALGO_SIG = h.hexdigest()[:12]
    return _ALGO_SIG


def _splitQuadImplied(points):
    result = []
    n = len(points)
    for i in range(n - 2):
        ctrl = points[i]
        nxt = points[i + 1]
        implied = ((ctrl[0] + nxt[0]) / 2.0, (ctrl[1] + nxt[1]) / 2.0)
        result.append((ctrl, implied))
    if n >= 2:
        result.append((points[-2], points[-1]))
    else:
        result.append((points[0], points[0]))
    return result


def _quadToCubic(p0, ctrl, p1):
    c1 = (p0[0] + 2 / 3 * (ctrl[0] - p0[0]), p0[1] + 2 / 3 * (ctrl[1] - p0[1]))
    c2 = (p1[0] + 2 / 3 * (ctrl[0] - p1[0]), p1[1] + 2 / 3 * (ctrl[1] - p1[1]))
    return c1, c2


# ---------------------------------------------------------------- C 库（偏旁部件模板层）
# 架构评审第6项评估原型：对高频偏旁用"载体字"（含该偏旁的最简成员字）
# 的**实拆结果**做部件级骨架模板。载体字拆解经过全部仲裁/精调/切割/
# 收口守卫，其偏旁槽位的逐笔中轴是该字体此偏旁的实证形态——比 B 库
# 单笔模板多携带槽位内的相对布局（氵三笔的错落、扌钩的收位），注入
# 时整槽一起定位，理论上可省去逐笔 B 候选扫描并提升同旁跨字一致性。
# 是否真有收益由 tools/eval_clib.py 用数字裁定，默认不启用。
CLIB_RADICALS = ["氵", "扌", "亻", "口", "木"]


def _clibRegistry(root):
    """data/radicals.json → {偏旁: 同源位形组}；文件缺失/损坏 → 空表
    （调用方按 [自身] 兜底）。variants 用于注入端等价匹配（亻↔人），
    建库端载体槽位只认本形（模板笔数=偏旁本形笔数，见 _clibCarrierOf）。"""
    reg = {}
    try:
        with open(os.path.join(root, "data", "radicals.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        for e in d.get("radicals", []):
            reg[e["radical"]] = list(e.get("variants") or [e["radical"]])
    except Exception:
        pass
    return reg


def _clibTopSlot(kai, radical):
    """载体字里偏旁**本形**所在的一级槽位 → (槽位号, [楷体笔序])；
    找不到 → None。槽位判定 = structure 一级 children 的 char 恰为本形
    （嵌套匿名 IDS 子树无 char 键，天然跳过）；成员笔 = matches 首元素
    等于该槽位号的笔（deepenMatches 细化保留首元素的一级槽位语义）。"""
    children = (kai.get("structure") or {}).get("children") or []
    slotIdx = None
    for i, node in enumerate(children):
        if node.get("char") == radical:
            slotIdx = i
            break
    if slotIdx is None:
        return None
    matches = kai.get("matches") or []
    strokeIdxs = [k for k, p in enumerate(matches) if p and p[0] == slotIdx]
    return (slotIdx, strokeIdxs) if strokeIdxs else None


def _clibCarrierOf(dataHub, fontEntry, radical, expectStrokes):
    """载体字选择：该偏旁成员字（dictionary radical 字段口径，复用
    datahub.familyChars 懒建索引，与 tools/build_radicals.py 一致）中，
    含本形一级槽位、槽位笔数恰为偏旁本形笔数、字体有字形者，取
    (笔画数, 字典序) 最小。偏旁字符本身跳过（模板要的是"偏旁在字内
    受挤压后的形态"，孤立偏旁字形不含这一信息）；槽外必须还有别的笔
    ——才3笔全落扌槽（⿻分解把整字标成扌+丿镶嵌），它是偏旁本形的
    变体字而非复合字，第三笔还是撇不是复合位的提，同理排除。
    → (载体字, 槽位号, [楷体笔序]) | None"""
    members = dataHub.familyChars(radical, "radical")
    pairs = []
    for ch in members:
        if ch == radical:
            continue
        g = dataHub.geom(ch)
        if g and len(g["medians"]) > expectStrokes:
            pairs.append((len(g["medians"]), ch))
    for _n, ch in sorted(pairs):
        if not fontEntry.hasChar(ch):
            continue
        kai = dataHub.kai(ch)
        if not kai:
            continue
        slot = _clibTopSlot(kai, radical)
        if slot and len(slot[1]) == expectStrokes:
            return ch, slot[0], slot[1]
    return None


def _clibKaiBBox(kai, strokeIdxs):
    """楷体包围盒：strokeIdxs 指定笔集（None=全字）的轮廓展平点并集。
    展平粒度 25 与 dbuild 全局对齐同款——建库端槽位 bbox 与注入端
    kaiStrokeBBoxes 并集必须同口径，否则映射比例带系统偏差。"""
    if strokeIdxs is None:
        strokeIdxs = range(len(kai["strokes"]))
    pts = []
    for k in strokeIdxs:
        for c in parseContours(kai["strokes"][k]):
            pts.extend(flattenSegs(c["segs"], 25))
    return bboxOfPoints(pts)


def _clibToKaiSpace(median, kb, tb):
    """载体字形空间 → 楷体坐标系（dbuild 全局仿射的逆映射）。
    C 条目 median 必须存楷体坐标：注入端"载体楷体槽位bbox→目标楷体
    槽位bbox"的映射要求两端同在楷体坐标系；若存字形坐标，映射比例
    会混入载体字自己的整字缩放（瘦字/扁字），跨字不可比。
    tb 展平粒度 10 与 grouping.analyzeContours 的 poly 同款。"""
    sx = tb.w / max(1e-6, kb.w)
    sy = tb.h / max(1e-6, kb.h)
    return [[kb.x0 + (p[0] - tb.x0) / max(1e-6, sx),
             kb.y0 + (p[1] - tb.y0) / max(1e-6, sy)] for p in median]


def _clibGlyphBBox(fontEntry, ch):
    """载体目标字形整字包围盒——与 dbuild 的 tb 同口径（grouping 的
    poly=flattenSegs(segs,10)），保证逆仿射恰为管线全局仿射之逆。"""
    pts = []
    for c in fontEntry.glyphContours(ch):
        pts.extend(flattenSegs(c["segs"], 10))
    return bboxOfPoints(pts)


def _clibBuildEntry(dataHub, fontEntry, radical, variants):
    """单旁 C 条目：载体字全程拆解 → 槽位逐笔档案。载体拆解失败或
    槽位笔画 failed → usable=False（评估原型不找替补载体：换载体=
    换模板形态，评估口径会漂移；不可用就如实记录）。"""
    from .pipeline import runPipeline  # 延迟导入避免循环
    entry = {"radical": radical, "variants": list(variants),
             "usable": False, "reason": ""}
    gRad = dataHub.geom(radical)
    if not gRad:
        entry["reason"] = "偏旁本形无楷体数据"
        return entry
    sel = _clibCarrierOf(dataHub, fontEntry, radical, len(gRad["medians"]))
    if not sel:
        entry["reason"] = "无合格载体字"
        return entry
    ch, slotIdx, strokeIdxs = sel
    entry["carrier"] = ch
    entry["slot"] = slotIdx
    try:
        r = runPipeline(dataHub, fontEntry, ch)
    except Exception as e:
        entry["reason"] = "载体拆解异常: " + repr(e)[:120]
        return entry
    if not r or "error" in r:
        entry["reason"] = "载体拆解失败: " + str((r or {}).get("error", ""))[:120]
        return entry
    slotStrokes = [r["strokes"][k] for k in strokeIdxs]
    if any(s["failed"] for s in slotStrokes):
        entry["reason"] = "载体槽位笔画 failed"
        return entry
    kai = dataHub.kai(ch)
    kb = _clibKaiBBox(kai, None)
    tb = _clibGlyphBBox(fontEntry, ch)
    slotBB = _clibKaiBBox(kai, strokeIdxs)
    entry["kaiSlotBBox"] = [slotBB.x0, slotBB.y0, slotBB.x1, slotBB.y1]
    entry["strokes"] = [{
        "carrierIndex": s["index"],
        "type": s["type"],
        "path": s["path"],                  # 切割路径（载体字形空间，评估/可视化用）
        "median": s["median"],              # 精调中轴（载体字形空间）
        "medianKai": _clibToKaiSpace(s["median"], kb, tb),  # 注入用（楷体坐标系）
        "width": s["width"],
    } for s in slotStrokes]
    # 槽位实测内容 bbox（楷体坐标系）：注入映射的**源** bbox。不能用
    # 楷体名义槽位 bbox 当源——medianKai 是字体实际布局的整字回拉，
    # 字体普遍把小部件抬高/收紧（卟的口在鸿蒙里比楷体名义位高 100+
    # 单位），名义源 bbox 会把这份"载体字体 vs 楷体"的布局偏移二次
    # 记账，映射结果整体错位（载体字对自己注入都过不了守卫：卟三笔
    # dev 0.33/0.43/1.42）。与 B 模板同理：源=模板自身实测 bbox
    # （outlineBBox），宿=楷体结构给的目标 bbox。
    cPts = [p for s in entry["strokes"] for p in s["medianKai"]]
    cBB = bboxOfPoints(cPts)
    entry["slotContentBBox"] = [cBB.x0, cBB.y0, cBB.x1, cBB.y1]
    entry["usable"] = True
    return entry


def _atomicWriteJson(path, obj):
    """JSON 原子落盘：同目录临时文件写全 → os.replace 原子替换（同卷
    NTFS/POSIX 均原子）。此前直接 open(最终路径,"w") 重写，进程中断/
    并发写会留半截 JSON——_loadLibCache 虽容错返回 None，整库缓存却
    白丢（重建 40s+）；server 曾受"只改 server.py"约束在调用侧用伪
    hub 重定向到 _tmp/ 目录补原子性，本轮下沉到写盘本源、撤销该
    workaround。临时名带 pid：两进程同时写同一缓存互不踩踏、各自
    替换，任一最终结果都是完整 JSON。替换失败（Windows 上目标被
    无 FILE_SHARE_DELETE 句柄持有等）时清掉临时文件，不留 .tmp
    残留；异常向上抛，由调用方按"缓存写失败不打断请求"纪律吞掉。"""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class FontEntry:
    """一个目标字体：字形轮廓提取 + B库（标准笔画库）。"""

    def __init__(self, path):
        self.path = path
        self.key = os.path.basename(path)
        self.font = TTFont(path, fontNumber=0, lazy=True)
        self.cmap = self.font.getBestCmap()
        self.upm = self.font["head"].unitsPerEm
        self.scale = 1024.0 / self.upm
        self._hmtx = self.font["hmtx"]
        self._glyphSet = self.font.getGlyphSet()
        self._glyphCache = {}
        self.libraryB = None
        self.libraryBAll = None
        self.libraryC = None    # C库：偏旁部件模板层（评估原型，按需建）

    def hasChar(self, ch):
        return ord(ch) in self.cmap

    # ------------------------------------------------------------ 轮廓提取
    def glyphContours(self, ch):
        """→ [{"segs": [...]}]，已归一化到 makemeahanzi 空间（按字宽居中）。"""
        if ch in self._glyphCache:
            return self._glyphCache[ch]
        if not self.hasChar(ch):
            self._glyphCache[ch] = None
            return None
        gname = self.cmap[ord(ch)]
        pen = RecordingPen()
        self._glyphSet[gname].draw(pen)
        aw = self._hmtx[gname][0] * self.scale
        dx = (1024.0 - aw) / 2.0
        s = self.scale

        def tx(pt):
            return (pt[0] * s + dx, pt[1] * s)

        contours = []
        cur = None
        pos = None
        start = None
        for op, args in pen.value:
            if op == "moveTo":
                if cur and cur["segs"]:
                    contours.append(cur)
                pos = tx(args[0])
                start = pos
                cur = {"segs": []}
            elif op == "lineTo":
                p = tx(args[0])
                if dist(pos, p) > 0.01:
                    cur["segs"].append(lineSeg(pos, p))
                pos = p
            elif op == "curveTo":
                pts = [tx(a) for a in args]
                for i in range(0, len(pts), 3):
                    cur["segs"].append(cubicSeg(pos, pts[i], pts[i + 1], pts[i + 2]))
                    pos = pts[i + 2]
            elif op == "qCurveTo":
                if args[-1] is None:
                    pts = [tx(a) for a in args[:-1]]
                    first, last_ = pts[0], pts[-1]
                    impliedStart = ((first[0] + last_[0]) / 2, (first[1] + last_[1]) / 2)
                    if cur is None or not cur["segs"] and pos is None:
                        cur = cur or {"segs": []}
                    if pos is None:
                        pos = impliedStart
                        start = impliedStart
                    pts = pts + [impliedStart]
                else:
                    pts = [tx(a) for a in args]
                if len(pts) == 1:
                    cur["segs"].append(lineSeg(pos, pts[0]))
                    pos = pts[0]
                else:
                    quads = [(pts[0], pts[1])] if len(pts) == 2 \
                        else _splitQuadImplied(pts)
                    for ctrl, on in quads:
                        c1, c2 = _quadToCubic(pos, ctrl, on)
                        cur["segs"].append(cubicSeg(pos, c1, c2, on))
                        pos = on
            elif op == "closePath":
                if pos is not None and start is not None and dist(pos, start) > 0.6:
                    cur["segs"].append(lineSeg(pos, start))
                pos = start
                if cur and cur["segs"]:
                    contours.append(cur)
                cur = None
        if cur and cur["segs"]:
            contours.append(cur)
        result = contours if contours else None
        self._glyphCache[ch] = result
        return result

    # ------------------------------------------------------------ B库磁盘缓存
    # 键 = 字体文件(大小+mtime) × 算法签名(核心源码哈希)；建库(base)与
    # 自举补全(full)分两阶段存取，保证有无缓存行为一致（bench 只 build、
    # server 会 complete，二者各取所需）
    def _libCachePath(self, dataHub):
        return os.path.join(dataHub.root, ".blibCache",
                            os.path.splitext(self.key)[0] + ".json")

    def _fontSig(self):
        try:
            st = os.stat(self.path)
            return "%d-%d" % (st.st_size, int(st.st_mtime))
        except OSError:
            return "?"

    def _setLibFromAll(self, allEntries):
        self.libraryBAll = allEntries
        self.libraryB = {}
        for e in allEntries:
            self.libraryB.setdefault(e["type"], e)

    def _saveLibCache(self, dataHub):
        try:
            p = self._libCachePath(dataHub)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            base = [e for e in self.libraryBAll if e.get("kind") != "bootstrap"]
            full = self.libraryBAll if getattr(self, "_libCompleted", False) else None
            _atomicWriteJson(p, {"algo": _algoSignature(),
                                 "font": self._fontSig(),
                                 "baseAll": base, "fullAll": full,
                                 "fullAdded": getattr(self, "_libAdded", None)})
        except Exception:
            pass

    def _loadLibCache(self, dataHub):
        try:
            d = json.load(open(self._libCachePath(dataHub), encoding="utf-8"))
            if d.get("algo") == _algoSignature() and \
               d.get("font") == self._fontSig():
                return d
        except Exception:
            pass
        return None

    # ------------------------------------------------------------ B 库
    def buildLibraryB(self, dataHub):
        cached = self._loadLibCache(dataHub)
        if cached:
            self._libCache = cached
            self._setLibFromAll(cached["baseAll"])
            return
        self._libCache = None
        lib = {}
        all_ = []
        # 来源 1：U+31C0..31E5 笔画区
        for cp in range(0x31C0, 0x31E6):
            ch = chr(cp)
            if not self.hasChar(ch):
                continue
            contours = self.glyphContours(ch)
            if not contours:
                continue
            t = CJK_STROKE_NAMES[cp]
            e = {"type": t,
                 "contours": [contourToPath(c["segs"]) for c in contours],
                 "source": "U+%04X %s %s" % (cp, ch, CJK_STROKE_ABBR.get(cp, "")),
                 "kind": "unicode", "tier": 0}
            e["desc"] = shapeDescriptor(e["contours"])
            all_.append(e)
            lib.setdefault(t, e)

        # 来源 2：规则表（32类 × 独立/少相交代表字）孤立连通组拆取
        analyzed = {}

        def analyzeChar(ch):
            g = dataHub.geom(ch)
            if not g or not self.hasChar(ch):
                return None
            contours = self.glyphContours(ch)
            if not contours:
                return None
            cs = [{"segs": c["segs"]} for c in contours]
            analyzeContours(cs)
            nGroups = max((c["group"] + 1 for c in cs), default=0)
            groupSamples = [[] for _ in range(nGroups)]
            for c in cs:
                groupSamples[c["group"]].extend(flattenSegs(c["segs"], 18))
            groupToStroke = [-1] * nGroups
            groupTop = [(-1, 0.0)] * nGroups
            strokeHits = [set() for _ in g["medians"]]
            for gi, samples in enumerate(groupSamples):
                if not samples:
                    continue
                votes = {}
                for p in samples:
                    best, bestD = -1, 1e18
                    for k, m in enumerate(g["medians"]):
                        d = nearestOnPolyline(p, m)["d"]
                        if d < bestD:
                            bestD, best = d, k
                    votes[best] = votes.get(best, 0) + 1
                top, topCount = max(votes.items(), key=lambda kv: kv[1])
                groupTop[gi] = (top, topCount / len(samples))
                if topCount >= len(samples) * 0.95:
                    groupToStroke[gi] = top
                    strokeHits[top].add(gi)
            return {"cs": cs, "nGroups": nGroups, "groupToStroke": groupToStroke,
                    "strokeHits": strokeHits, "groupTop": groupTop, "g": g}

        for t in TYPE_ORDER:
            if t in lib:
                continue  # 来源1（Unicode 笔画区）已明确该类型，短路其他途径
            candidates = []
            for ch, idx, pos in parseProbes(PROBE_TABLE[t]):
                if idx is None and pos is not None:
                    idx = _resolveProbeStroke(dataHub, ch, t, pos)
                if ch not in analyzed:
                    analyzed[ch] = analyzeChar(ch)
                an = analyzed[ch]
                if not an:
                    continue
                relaxedGroups = None
                if idx is not None:
                    # 明确笔序：投票纯度 95%→60%——楷体中轴线在笔画交界附近
                    # 的串票会错杀真孤立笔（黑体八的撇纯度 77%）；仍要求
                    # 该笔序恰好对应唯一连通组
                    relaxedGroups = [gj for gj in range(an["nGroups"])
                                     if an["groupTop"][gj][0] == idx
                                     and an["groupTop"][gj][1] >= 0.6]
                for gi in range(an["nGroups"]):
                    k = an["groupToStroke"][gi]
                    if idx is not None:
                        if len(relaxedGroups) != 1 or gi != relaxedGroups[0]:
                            continue
                        k = idx
                        tier = 1
                    elif k < 0 or len(an["strokeHits"][k]) != 1:
                        continue
                    else:
                        tier = matchTier(typeOfStroke(ch, k, an["g"]["medians"]), t)
                        if not tier:
                            continue
                    cand = {"type": t, "tier": tier,
                            "contours": [contourToPath(c["segs"])
                                         for c in an["cs"] if c["group"] == gi],
                            "source": "从「%s」第%d笔拆出" % (ch, k + 1),
                            "kind": "extracted"}
                    cand["desc"] = shapeDescriptor(cand["contours"])
                    ref = (lib.get(t) or {}).get("desc") or \
                        shapeDescriptor([an["g"]["strokes"][k]])
                    cand["shapeSim"] = shapeSimilarity(cand["desc"], ref)
                    if cand["shapeSim"] < 20:
                        continue
                    candidates.append(cand)
            candidates.sort(key=lambda e: e["tier"])  # 稳定排序：同tier保持规则表顺序（用户"按序尝试"语义）
            for e in candidates[:4]:
                all_.append(e)
                lib.setdefault(t, e)

        self.libraryB = lib
        self.libraryBAll = all_

        # 来源 0（最高优先）：用户裁定的楷体类型→目标字体取材映射
        # （KAI_TARGET_MAP，覆盖审计后全部 70 类楷体笔形）。映射产出的
        # 条目插到该类型候选队首并接管 libraryB[t]；原有来源1/2 条目保留
        # 为并行候选——逐笔匹配时由模板-结构一致性偏差闸最终挑选，
        # 映射标注偶有笔误也不会造成硬伤。
        def _probeEntry(t, ch, pos):
            """探针字提取：位置词→笔序→孤立连通组；单笔字整字直取。"""
            if not self.hasChar(ch) or not dataHub.hasKai(ch):
                return None
            g = dataHub.geom(ch)
            if ch not in analyzed:
                analyzed[ch] = analyzeChar(ch)
            an = analyzed[ch]
            if not an:
                return None
            if pos is None:
                if len(g["medians"]) != 1:
                    return None
                cs = an["cs"]
                paths = [contourToPath(c["segs"]) for c in cs]
            else:
                idx = _resolveProbeStroke(dataHub, ch, t, pos)
                if idx is None:
                    return None
                relaxed = [gj for gj in range(an["nGroups"])
                           if an["groupTop"][gj][0] == idx
                           and an["groupTop"][gj][1] >= 0.6]
                if len(relaxed) != 1:
                    return None
                paths = [contourToPath(c["segs"]) for c in an["cs"]
                         if c["group"] == relaxed[0]]
            if not paths:
                return None
            e = {"type": t, "contours": paths,
                 "source": "映射：从「%s%s」提取" % (ch, "·" + pos if pos else ""),
                 "kind": "mapExtract", "tier": 0}
            e["desc"] = shapeDescriptor(e["contours"])
            return e

        def _cpEntry(t, cp):
            ch = chr(cp)
            if not self.hasChar(ch):
                return None
            contours = self.glyphContours(ch)
            if not contours:
                return None
            e = {"type": t,
                 "contours": [contourToPath(c["segs"]) for c in contours],
                 "source": "映射：U+%04X %s %s" % (cp, ch,
                                                   CJK_STROKE_ABBR.get(cp, "")),
                 "kind": "mapUnicode", "tier": 0}
            e["desc"] = shapeDescriptor(e["contours"])
            return e

        def _shiftPaths(paths, dx, dy):
            out = []
            for d in paths:
                for c in parseContours(d):
                    segs = [(s[0],) + tuple((p[0] + dx, p[1] + dy)
                                            for p in s[1:]) for s in c["segs"]]
                    out.append(contourToPath(segs))
            return out

        mapAll = []
        for t, spec in KAI_TARGET_MAP.items():
            entries = []
            for cp, ch, pos in spec.get("take", []):
                # 取字集语义=按优先级递减：码位有字形直取；探针字提取只在
                # 码位缺失时启用（并行候选曾让勺·中的点/买·上的横钩顶掉
                # 码位字形，詫潺餾等 12 字齐跌——同型异源比拼 dev 分不出
                # 优劣，噪声提取偶胜反而切坏）
                e = _cpEntry(t, cp) if cp else None
                if e:
                    entries.append(e)
                elif ch:
                    e2 = _probeEntry(t, ch, pos)
                    if e2:
                        entries.append(e2)
            fuseCh = spec.get("fuse")
            if fuseCh and self.hasChar(fuseCh):
                contours = self.glyphContours(fuseCh)
                if contours:
                    e = {"type": t,
                         "contours": [contourToPath(c["segs"]) for c in contours],
                         "source": "映射：融合字形「%s」" % fuseCh,
                         "kind": "mapFuse", "tier": 0}
                    e["desc"] = shapeDescriptor(e["contours"])
                    entries.append(e)
            members = spec.get("compose")
            if members:
                # 组合笔画：成员骨架首尾相接，第二段整体平移到第一段终点
                parts = []
                for cp, ch, pos in members:
                    e = (_cpEntry(t, cp) if cp else None) or \
                        (_probeEntry(t, ch, pos) if ch else None)
                    if e:
                        parts.append(e)
                if len(parts) == len(members) and len(parts) >= 2:
                    try:
                        base = dict(parts[0])
                        skel = list(self.ensureSkeleton(parts[0], dataHub))
                        paths = list(parts[0]["contours"])
                        for nxt in parts[1:]:
                            s2 = self.ensureSkeleton(nxt, dataHub)
                            dx = skel[-1][0] - s2[0][0]
                            dy = skel[-1][1] - s2[0][1]
                            skel += [(p[0] + dx, p[1] + dy) for p in s2[1:]]
                            paths += _shiftPaths(nxt["contours"], dx, dy)
                        pts = []
                        for d in paths:
                            for c in parseContours(d):
                                pts.extend(flattenSegs(c["segs"], 12))
                        bb = bboxOfPoints(pts)
                        base.update({
                            "contours": paths, "skeleton": skel,
                            "outlineBBox": [bb.x0, bb.y0, bb.x1, bb.y1],
                            "source": "映射：组合 " + "+".join(
                                p["source"].replace("映射：", "") for p in parts),
                            "kind": "mapCompose", "tier": 0,
                            "desc": shapeDescriptor(paths)})
                        entries.append(base)
                    except Exception:
                        pass
            if entries:
                mapAll.extend(entries)
                self.libraryB[t] = entries[0]
        if mapAll:
            self.libraryBAll = mapAll + self.libraryBAll

        self._saveLibCache(dataHub)

    # ------------------------------------------------------------ 自举回灌
    def completeLibraryB(self, dataHub, maxCharsPerType=3,
                         simGate=40, retainGate=0.55, coverGate=99.0):
        """补全 B库：孤立提取在融合/连笔字体上先天贫血（可能连撇都提不出），
        对缺失类型用当前管线拆规则表代表字，质量闸（原轮廓保留率、形状匹配、
        并集覆盖率）通过的 D′ 笔画作为库条目回灌。管线有布尔收口保证并集
        恒等，回灌是安全的。"""
        cached = getattr(self, "_libCache", None)
        if cached and cached.get("fullAll") is not None:
            self._setLibFromAll(cached["fullAll"])
            self._libCompleted = True
            return dict(cached.get("fullAdded") or {})
        from .pipeline import runPipeline  # 延迟导入避免循环
        added = {}
        resultCache = {}
        for t in TYPE_ORDER:
            # 精确键判断：findLibEntry 的骨架宽容回退会让缺失类型被近亲
            # 顶包而跳过自举（黑体竖提缺失时被区的竖折冒名，拆「以」明明
            # 能拿到 retain 1.00 的真身）
            if t in self.libraryB:
                continue
            tried = 0
            for ch, idx, pos in parseProbes(PROBE_TABLE[t]):
                if idx is None and pos is not None:
                    idx = _resolveProbeStroke(dataHub, ch, t, pos)
                if tried >= maxCharsPerType:
                    break
                if not self.hasChar(ch) or not dataHub.hasKai(ch):
                    continue
                tried += 1
                if ch not in resultCache:
                    try:
                        resultCache[ch] = runPipeline(dataHub, self, ch)
                    except Exception:
                        resultCache[ch] = None
                r = resultCache[ch]
                if not r or "error" in r:
                    continue
                if r["unionCheck"]["cover"] < coverGate:
                    continue
                best = None
                for s in r["strokes"]:
                    if s["failed"]:
                        continue
                    # 规则表明确笔序：只认该笔；否则按类型匹配
                    if idx is not None:
                        if s["index"] != idx:
                            continue
                    elif not matchTier(s["type"], t):
                        continue
                    # 保留率≥90% ≈ 整条孤立轮廓，就是该字体笔画的真身，
                    # 不再要求与楷体形似（风格化字体恰恰在这里最不像楷体）
                    trusted = s["retainRatio"] >= 0.9
                    gated = s["retainRatio"] >= retainGate and s["shapeSim"] >= simGate
                    if not (trusted or gated):
                        continue
                    score = s["retainRatio"] * 100 + s["shapeSim"]
                    if best is None or score > best[0]:
                        best = (score, s)
                if best:
                    best = best[1]
                    entry = {"type": t, "contours": [best["path"]],
                             "source": "自举：拆「%s」第%d笔" % (ch, best["index"] + 1),
                             "kind": "bootstrap", "tier": 2,
                             "shapeSim": best["shapeSim"]}
                    entry["desc"] = shapeDescriptor(entry["contours"])
                    self.libraryBAll.append(entry)
                    self.libraryB.setdefault(t, entry)
                    added[t] = entry["source"]
                    break
        self._libCompleted = True
        self._libAdded = added
        # 预计算全部骨架（Voronoi 中轴较贵，懒算曾让首字拆解多花 2.5s）
        # 后一并落盘，重启进程直接命中
        self.ensureAllSkeletons(dataHub)
        self._skelDirty = False
        self._saveLibCache(dataHub)
        return added

    def ensureAllSkeletons(self, dataHub):
        for e in (self.libraryBAll or []):
            try:
                self.ensureSkeleton(e, dataHub)
            except Exception:
                pass

    def saveSkeletonsIfDirty(self, dataHub):
        """拆解过程中借用/兜底可能懒算出新骨架——机会性回写缓存。"""
        if getattr(self, "_skelDirty", False):
            self._skelDirty = False
            self._saveLibCache(dataHub)

    # ------------------------------------------------------------ C 库磁盘缓存
    # 键 = 字体指纹 × 算法签名，与 B 库同一纪律：载体字拆解结果是算法
    # 的函数，算法一变缓存自动失效。独立文件（<font>.clib.json）而非并
    # 入 B 缓存——C 库按需建（默认关），不拖累 B 库的加载路径。
    def _clibCachePath(self, dataHub):
        return os.path.join(dataHub.root, ".blibCache",
                            os.path.splitext(self.key)[0] + ".clib.json")

    def _loadClibCache(self, dataHub):
        try:
            with open(self._clibCachePath(dataHub), encoding="utf-8") as f:
                d = json.load(f)
            if d.get("algo") == _algoSignature() and \
               d.get("font") == self._fontSig():
                return d["entries"]
        except Exception:
            pass
        return None

    def _saveClibCache(self, dataHub, entries):
        try:
            p = self._clibCachePath(dataHub)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            _atomicWriteJson(p, {"algo": _algoSignature(),
                                 "font": self._fontSig(),
                                 "entries": entries})
        except Exception:
            pass

    # ------------------------------------------------------------ C 库建库
    def buildLibraryC(self, dataHub):
        """C 库（偏旁部件模板层）建库：每字体一次 + 落盘缓存。

        条目 = CLIB_RADICALS 五个高频旁各一：载体字（(笔画数,字典序)
        最小的合格成员字）经 runPipeline 全程拆解，按 matches 提取偏旁
        槽位逐笔的 楷体笔序/类型/切割路径/median/笔宽，连同槽位楷体
        bbox 存档（median 另存楷体坐标系版本，注入映射用）。

        重入护栏：建库要跑载体字拆解，期间若 CLIB_ENABLE 开着，dbuild
        会经 ensureLibraryC 再进来——护栏令其拿到 None、按无 C 库走；
        否则递归自举，且载体模板会依赖建库顺序（非确定）。护栏同时
        保证有无开关建出的库字节一致（缓存可复用）。"""
        if getattr(self, "_clibBuilding", False):
            return None
        self._clibBuilding = True
        try:
            entries = self._loadClibCache(dataHub)
            if entries is None:
                # 载体拆解走生产同款库路径（建库+自举补全），与 bench/
                # verify/server 环境一致，模板形态不随调用方漂移
                if self.libraryB is None:
                    self.buildLibraryB(dataHub)
                if not getattr(self, "_libCompleted", False):
                    self.completeLibraryB(dataHub)
                reg = _clibRegistry(dataHub.root)
                entries = [_clibBuildEntry(dataHub, self, r,
                                           reg.get(r) or [r])
                           for r in CLIB_RADICALS]
                self._saveClibCache(dataHub, entries)
            self._clibAll = entries
            self.libraryC = {e["radical"]: e for e in entries
                             if e.get("usable")}
        finally:
            self._clibBuilding = False
        return entries

    def ensureLibraryC(self, dataHub):
        """惰性建库入口（dbuild 注入端用）：CLIB_ENABLE 开着跑 bench/
        verify 时 FontEntry 由各处自行构造，不能指望调用方显式建库；
        照 B 库"首用即建+缓存命中"语义。建库进行中返回 None（见
        buildLibraryC 护栏注释）。"""
        if self.libraryC is not None:
            return self.libraryC
        self.buildLibraryC(dataHub)
        return self.libraryC


    # ------------------------------------------------------------ A↔B 骨架映射
    def ensureSkeleton(self, entry, dataHub):
        """B 笔画骨架 = 从 B 轮廓自身提取的中线（Voronoi 中轴直径路径），
        楷体同类型中轴线只决定书写方向（起点在哪头）——臂长比例、弯直
        等形状信息完全来自目标字体（用户架构要求：A↔B 对应建立后楷体
        形状不参与，bbox 映射楷体中线在臂比悬殊时会斜穿墨块）。中轴
        提取退化时才回退老路：楷体中线 bbox 映射 + 精调。"""
        if entry.get("skeleton"):
            return entry["skeleton"]
        self._skelDirty = True  # 新算骨架 → 缓存待回写
        contours = []
        for d in entry["contours"]:
            contours.extend(parseContours(d))
        pts = []
        for c in contours:
            pts.extend(flattenSegs(c["segs"], 12))
        bb = bboxOfPoints(pts)
        entry["outlineBBox"] = [bb.x0, bb.y0, bb.x1, bb.y1]
        aList = findLibEntry(dataHub.libraryA, entry["type"]) if dataHub.libraryA else None
        kaiStart = None
        if aList:
            aPathPts = []
            for c in parseContours(aList[0]["path"]):
                aPathPts.extend(flattenSegs(c["segs"], 20))
            ab = bboxOfPoints(aPathPts)
            km = aList[0]["median"]
            kaiStart = (bb.x0 + (km[0][0] - ab.x0) * bb.w / max(1.0, ab.w),
                        bb.y0 + (km[0][1] - ab.y0) * bb.h / max(1.0, ab.h))
        loops = [flattenSegs(c["segs"], 6) for c in contours]
        med = outlineCenterline(loops)
        if med and len(med) >= 2:
            if kaiStart is not None:
                d0 = dist(med[0], kaiStart)
                d1 = dist(med[-1], kaiStart)
                if d1 < d0:
                    med = med[::-1]
            med = resamplePolyline([tuple(p) for p in med], 15)
        else:
            # 回退：楷体中线 bbox 映射 + 精调（中轴提取退化，如极小轮廓）
            if aList:
                med = [(bb.x0 + (p[0] - ab.x0) * bb.w / max(1.0, ab.w),
                        bb.y0 + (p[1] - ab.y0) * bb.h / max(1.0, ab.h))
                       for p in aList[0]["median"]]
            else:
                med = [(bb.x0, bb.y1), (bb.x1, bb.y0)]
            samples = []
            for c in contours:
                for seg in c["segs"]:
                    n = max(3, min(20, int(math.ceil(segLength(seg) / 12))))
                    for i in range(n):
                        samples.append(bezPoint(seg, i / n))
            for _ in range(3):
                if len(samples) < 6:
                    break
                ds = sorted(nearestOnPolyline(p, med)["d"] for p in samples)
                w = max(10.0, 2 * ds[len(ds) // 2])
                med = refineMedianFit(med, [tuple(p) for p in med], samples, w)
        # 法向中点矫正（Voronoi 路径有采样锯齿）+ 分段吸直：直笔纯直线、
        # 折笔干净直段+拐角（同类型骨架拓扑一致），真曲段保持
        med = midpointRectify(med, loops)
        med = straightenSections(med)
        entry["skeleton"] = med
        return med
