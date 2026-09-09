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
                       shapeDescriptor, shapeSimilarity, refineMedianFit)
from .classify import (PROBE_TABLE, TYPE_ORDER, CJK_STROKE_NAMES,
                       CJK_STROKE_ABBR, typeOfStroke, matchTier, findLibEntry,
                       parseProbes, PROBE_POSITIONS, similarTypes)


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
    """算法签名：核心源码内容哈希——算法一变缓存自动失效。"""
    global _ALGO_SIG
    if _ALGO_SIG is None:
        h = hashlib.md5()
        pkg = os.path.dirname(os.path.abspath(__file__))
        for name in ("pipeline.py", "geometry.py", "classify.py",
                     "boolean.py", "fonthub.py"):
            try:
                with open(os.path.join(pkg, name), "rb") as f:
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
            json.dump({"algo": _algoSignature(), "font": self._fontSig(),
                       "baseAll": base, "fullAll": full,
                       "fullAdded": getattr(self, "_libAdded", None)},
                      open(p, "w", encoding="utf-8"), ensure_ascii=False)
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
                if topCount >= len(samples) * 0.95:
                    groupToStroke[gi] = top
                    strokeHits[top].add(gi)
            return {"cs": cs, "nGroups": nGroups, "groupToStroke": groupToStroke,
                    "strokeHits": strokeHits, "g": g}

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
                for gi in range(an["nGroups"]):
                    k = an["groupToStroke"][gi]
                    if k < 0 or len(an["strokeHits"][k]) != 1:
                        continue
                    if idx is not None:
                        # 规则表明确笔序：只认该笔，跳过类型自动匹配
                        if k != idx:
                            continue
                        tier = 1
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
            candidates.sort(key=lambda e: (e["tier"], -e.get("shapeSim", 0)))
            for e in candidates[:4]:
                all_.append(e)
                lib.setdefault(t, e)

        self.libraryB = lib
        self.libraryBAll = all_
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
            if findLibEntry(self.libraryB, t) is not None:
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
        self._saveLibCache(dataHub)
        return added

    # ------------------------------------------------------------ A↔B 骨架映射
    def ensureSkeleton(self, entry, dataHub):
        """把 A库（楷体）同类型中轴线 bbox 映射进 B 笔画轮廓并单笔精调拟合，
        得到目标字体自己的笔画形态骨架（A↔B 映射的核心产物）。"""
        if entry.get("skeleton"):
            return entry["skeleton"]
        contours = []
        for d in entry["contours"]:
            contours.extend(parseContours(d))
        pts = []
        for c in contours:
            pts.extend(flattenSegs(c["segs"], 12))
        bb = bboxOfPoints(pts)
        entry["outlineBBox"] = [bb.x0, bb.y0, bb.x1, bb.y1]
        aList = findLibEntry(dataHub.libraryA, entry["type"]) if dataHub.libraryA else None
        if aList:
            aPathPts = []
            for c in parseContours(aList[0]["path"]):
                aPathPts.extend(flattenSegs(c["segs"], 20))
            ab = bboxOfPoints(aPathPts)
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
        entry["skeleton"] = med
        return med
