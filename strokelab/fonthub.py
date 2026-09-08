# -*- coding: utf-8 -*-
"""strokelab.fonthub — 目标字体解析（fontTools）、B库建库、A↔B 骨架映射。"""

import math
import os

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen

from .geometry import (lineSeg, cubicSeg, dist, parseContours, contourToPath,
                       flattenSegs, bboxOfPoints, analyzeContours,
                       nearestOnPolyline, segLength, bezPoint,
                       shapeDescriptor, shapeSimilarity, refineMedianFit)
from .classify import (PROBE_TABLE, TYPE_ORDER, CJK_STROKE_NAMES,
                       CJK_STROKE_ABBR, typeOfStroke, matchTier, findLibEntry)


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

    # ------------------------------------------------------------ B 库
    def buildLibraryB(self, dataHub):
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
            candidates = []
            for ch in PROBE_TABLE[t]:
                if ch not in analyzed:
                    analyzed[ch] = analyzeChar(ch)
                an = analyzed[ch]
                if not an:
                    continue
                for gi in range(an["nGroups"]):
                    k = an["groupToStroke"][gi]
                    if k < 0 or len(an["strokeHits"][k]) != 1:
                        continue
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
