# -*- coding: utf-8 -*-
"""strokelab.verify — 确定性批量校验：绝对不变量，无需 golden、无需目检。

原则：校验实现必须与生产代码路径独立（Re-Union 曾与 glyphRegion 共享同一
bug 而自校验恒 100%）。本模块的字形区域用缠绕数分层法独立构造，笔画类型
对 D′ 独立复判，不复用 boolean/pipeline 的区域与标注。

失败码：
  ERROR    管线异常或报错
  COUNT    笔画数/空路径/退化面积 与楷体不一致
  SPLIT    某笔断成多个孤立面片（公理：单笔必单连通）
  UNION    笔画并集与原字形不恒等（缠绕数区域对比，覆盖缺口/溢出>0.5%）
  TYPE     横/竖笔的轮廓 PCA 主轴偏离水平/垂直超 32°（张冠李戴/拆歪）
  ORDER    两笔质心相对方位与楷体矛盾（张冠李戴）
  OVERLAP  两笔大面积重叠但楷体对应笔不相交
  AREA     单笔面积占比与楷体对应笔占比偏离超限（饿死/拉爆）

用法：
  python -m strokelab.verify --root D:/07_chcharapart                 # 全字体×全字
  python -m strokelab.verify --root . --fonts simhei.ttf --limit 200  # 抽样
  python -m strokelab.verify --root . --chars 中永爱 --jobs 1         # 定点
  python -m strokelab.verify --root . --report-only                   # 只汇总
结果：<root>/verifyOut/<字体>.jsonl + summary.json + report.html
"""

import argparse
import json
import math
import os
import time

from .geometry import (parseContours, flattenSegs, signedArea, resamplePolyline,
                       dist, shapeDescriptor)
from .classify import classifyMedian, matchTier, semanticSegments

UNION_TOL = 0.5      # 覆盖缺口/溢出 阈值（% of 字形面积）
OVERLAP_RATIO = 0.6  # 两笔重叠占较小笔比例阈值
OVERLAP_KAI_D = 40.0  # 楷体两笔中轴线最近距离小于此值视为"确有相交"
AREA_RATIO = 4.0     # 面积占比偏离倍数阈值
AREA_MIN_SHARE = 0.02  # 占比小于此值的笔不参与 AREA 判定（点画噪声）
ORDER_KAI_GAP = 180.0  # 楷体质心间距大于此值才构成"明确方位"约束（相交笔对质心重合，小间距会误报）
ORDER_TOL = 60.0     # 目标字体反向超过此距离才算矛盾
SPLIT_MIN_AREA = 25.0  # 小于此面积的碎片不计连通性
IOU_FLAT = 4.0       # shapely 细分步长


# ---------------------------------------------------------------- 区域构造（独立实现）

def _polys(pathStr):
    """路径 → [(Polygon, signedArea)]。"""
    from shapely.geometry import Polygon
    out = []
    for c in parseContours(pathStr):
        pts = flattenSegs(c["segs"], IOU_FLAT)
        if len(pts) < 4:
            continue
        try:
            pg = Polygon(pts)
            if not pg.is_valid:
                pg = pg.buffer(0)
            if not pg.is_empty:
                out.append((pg, signedArea(pts)))
        except Exception:
            pass
    return out


def _coverLevels(polys):
    """levels[k] = 被至少 k+1 个多边形覆盖的区域（二进制进位法）。"""
    levels = []
    for pg in polys:
        carry = pg
        i = 0
        while carry is not None and not carry.is_empty:
            if i == len(levels):
                levels.append(carry)
                break
            inter = levels[i].intersection(carry)
            union = levels[i].union(carry)
            if not union.is_valid:
                union = union.buffer(0)
            levels[i] = union
            carry = inter if inter.is_valid else inter.buffer(0)
            i += 1
    return levels


def windingRegion(pathStr):
    """按 nonzero 缠绕数构造填充区域：filled ⇔ 同外环向圈数 > 反向圈数
    = ∪_k ( posLv[k] − negLv[k] )。与 boolean.glyphRegion 完全独立。"""
    pp = _polys(pathStr)
    if not pp:
        return None
    # 面积最大的轮廓是外环，其绕向为正类
    outerSign = 1.0 if max(pp, key=lambda t: abs(t[1]))[1] >= 0 else -1.0
    pos = [pg for pg, a in pp if a * outerSign >= 0]
    neg = [pg for pg, a in pp if a * outerSign < 0]
    posLv = _coverLevels(pos)
    negLv = _coverLevels(neg)
    filled = None
    for k in range(len(posLv)):
        piece = posLv[k]
        if k < len(negLv):
            piece = piece.difference(negLv[k])
        if not piece.is_valid:
            piece = piece.buffer(0)
        filled = piece if filled is None else filled.union(piece)
    if filled is not None and not filled.is_valid:
        filled = filled.buffer(0)
    return filled


def evenOddRegion(pathStr):
    """笔画区域：多环奇偶合成（环形笔画=外环⊕孔环）。"""
    region = None
    for pg, _ in _polys(pathStr):
        if region is None:
            region = pg
        else:
            try:
                region = region.symmetric_difference(pg)
            except Exception:
                region = region.buffer(0).symmetric_difference(pg.buffer(0))
    if region is not None and not region.is_valid:
        region = region.buffer(0)
    return region


def _pieces(region):
    from shapely.geometry import Polygon, MultiPolygon
    if region is None or region.is_empty:
        return []
    if isinstance(region, Polygon):
        return [region]
    if isinstance(region, MultiPolygon):
        return list(region.geoms)
    return [g for g in getattr(region, "geoms", []) if isinstance(g, Polygon)]


# ---------------------------------------------------------------- 辅助

def _centroid(median):
    n = len(median) or 1
    return (sum(p[0] for p in median) / n, sum(p[1] for p in median) / n)


def _centroid_region(reg):
    if reg is None or reg.is_empty:
        return None
    c = reg.centroid
    return (c.x, c.y)


def _medianMinDist(mA, mB):
    a = resamplePolyline([tuple(p) for p in mA], 30)
    b = resamplePolyline([tuple(p) for p in mB], 30)
    return min(dist(p, q) for p in a for q in b)


# ---------------------------------------------------------------- 单字校验

def verifyChar(hub, font, ch):
    from shapely.ops import unary_union
    t0 = time.time()
    rec = {"ch": ch, "fails": [], "m": {}}

    def fail(code, detail):
        rec["fails"].append({"code": code, "detail": detail})

    from .pipeline import runPipeline
    r = runPipeline(hub, font, ch)
    if "error" in r:
        fail("ERROR", str(r["error"]))
        rec["ms"] = int((time.time() - t0) * 1000)
        return rec

    kai = r["kai"]
    strokes = r["strokes"]

    # ---- COUNT：条数 + 空路径 + 退化面积
    if len(strokes) != len(kai["medians"]):
        fail("COUNT", "笔数 %d≠楷体 %d" % (len(strokes), len(kai["medians"])))
    regions = []
    badIdx = []
    for s in strokes:
        reg = None if s["failed"] else evenOddRegion(s["path"])
        if reg is None or reg.is_empty or reg.area < 4:
            badIdx.append(s["index"])
            reg = None
        regions.append(reg)
    if badIdx:
        fail("COUNT", "空/退化笔画 %s" % badIdx)

    # ---- 字形区域（SPLIT 放行判定与 UNION 共用）
    glyphPath = " ".join(c["path"] for c in r["contours"])
    glyph = windingRegion(glyphPath)
    glyphPieces = _pieces(glyph) if glyph is not None else []

    # ---- SPLIT：单笔单连通（复合笔形放行"设计性分离"：字体把竖折等
    # 画成不相交的件时，正确拆解的该笔本就多片——片数≤语义单元数、
    # 且各片落在字形墨的**不同连通分量**才放行，同分量内断裂仍算病）
    splitIdx = []
    for s, reg in zip(strokes, regions):
        if reg is None:
            continue
        big = [g for g in _pieces(reg)
               if g.area >= max(SPLIT_MIN_AREA, reg.area * 0.02)]
        if len(big) > 1:
            units = semanticSegments(kai["strokeTypes"][s["index"]]) \
                if s["index"] < len(kai["strokeTypes"]) else []
            allowed = False
            if len(units) >= 2 and len(big) <= len(units) and len(glyphPieces) > 1:
                comps = []
                for pc in big:
                    ci = -1
                    for gi, gp in enumerate(glyphPieces):
                        try:
                            if gp.intersection(pc).area > pc.area * 0.5:
                                ci = gi
                                break
                        except Exception:
                            pass
                    comps.append(ci)
                if -1 not in comps and len(set(comps)) == len(comps):
                    allowed = True
            if not allowed:
                splitIdx.append("%d(%d片)" % (s["index"], len(big)))
    if splitIdx:
        fail("SPLIT", " ".join(splitIdx))

    # ---- UNION：缠绕数区域 vs 笔画并集
    if glyph is None or glyph.area < 1:
        fail("ERROR", "字形区域构造失败")
    else:
        valid = [g for g in regions if g is not None]
        if valid:
            union = unary_union(valid)
            if not union.is_valid:
                union = union.buffer(0)
            shortPct = glyph.difference(union).area / glyph.area * 100
            excessPct = union.difference(glyph).area / glyph.area * 100
            rec["m"]["short"] = round(shortPct, 2)
            rec["m"]["excess"] = round(excessPct, 2)
            if shortPct > UNION_TOL or excessPct > UNION_TOL:
                fail("UNION", "缺口%.2f%% 溢出%.2f%%" % (shortPct, excessPct))
        else:
            fail("UNION", "无有效笔画")

    # ---- TYPE：轮廓 PCA 主轴校验（仅横/竖，稳健硬门）。
    # D′ 中轴线 classifyMedian 全量重分类对精调后的短中轴线误报率过高
    # （口的竖曾被判捺折），降级为软指标 m.reclass 供统计分析。
    # 参照轴 = 楷体该笔自身的弦向而非教条水平/垂直——楷体把丬的第二
    # 笔画成 35° 陡提但标签叫"横"（分类器方言），目标字体照画 39° 被
    # 教条轴误报（丬/冫族 76+ 字）。校验本义是"切出来的像楷体这一笔"。
    typeBad = []
    reclassBad = 0
    for s in strokes:
        if s["failed"]:
            continue
        if s.get("median"):
            cls = classifyMedian(s["median"])
            if matchTier(cls, s["type"]) == 0:
                reclassBad += 1
        if s["type"] in ("横", "竖"):
            d = shapeDescriptor([s["path"]])
            if d and d["elong"] >= 1.8:
                ang = math.degrees(d["mainAngle"]) % 180.0
                km = kai["medians"][s["index"]] \
                    if s["index"] < len(kai["medians"]) else None
                if km and len(km) >= 2 and \
                        dist(tuple(km[0]), tuple(km[-1])) > 1e-6:
                    kAng = math.degrees(math.atan2(
                        km[-1][1] - km[0][1], km[-1][0] - km[0][0])) % 180.0
                else:
                    kAng = 0.0 if s["type"] == "横" else 90.0
                devK = abs(ang - kAng)
                devK = min(devK, 180.0 - devK)
                # 双参照取小：楷体弦向治丬族方言（楷体自画35°陡提），
                # 教条轴治镜像斜向（糹底左点楷体右下斜/鸿蒙左下斜为合法
                # 镜像，对弦向偏43°对教条轴仅18°）。真错家双参照皆超仍抓
                canon = 0.0 if s["type"] == "横" else 90.0
                devC = abs(ang - canon)
                devC = min(devC, 180.0 - devC)
                dev = min(devK, devC)
                if dev > 32.0:
                    typeBad.append("%d:%s轴偏%.0f°" % (s["index"], s["type"], dev))
    rec["m"]["reclass"] = reclassBad
    if typeBad:
        fail("TYPE", " ".join(typeBad))

    # ---- ORDER：两笔质心相对方位 vs 楷体。硬门只判**同型/相似型**笔对
    # ——ORDER 的本义是"张冠李戴"（身份互换），只有同型笔才可能换家；
    # 跨型对的方位翻转（她：鸿蒙把女的提画得比也的短竖高、楷体相反）
    # 是字体比例设计差异，全量 605/685 违规皆此类，降为软指标 orderX。
    cKai = [_centroid(m) for m in kai["medians"]]
    cTgt = [(_centroid(s["median"]) if s.get("median") else None) for s in strokes]
    orderBad = []
    orderSoft = 0
    n = min(len(cKai), len(cTgt))
    for i in range(n):
        for j in range(i + 1, n):
            if cTgt[i] is None or cTgt[j] is None:
                continue
            ti = kai["strokeTypes"][i] if i < len(kai["strokeTypes"]) else ""
            tj = kai["strokeTypes"][j] if j < len(kai["strokeTypes"]) else ""
            sameKind = (ti == tj) or bool(matchTier(ti, tj))
            for axis in (0, 1):
                dK = cKai[j][axis] - cKai[i][axis]
                dT = cTgt[j][axis] - cTgt[i][axis]
                if abs(dK) >= ORDER_KAI_GAP and dK * dT < 0 and abs(dT) > ORDER_TOL:
                    if sameKind:
                        orderBad.append("%d-%d%s" % (i, j, "xy"[axis]))
                    else:
                        orderSoft += 1
    rec["m"]["orderX"] = orderSoft
    if orderBad:
        fail("ORDER", " ".join(orderBad[:8]))

    # ---- COMP 软指标（种子字体系，先观察后升门）：
    # compQuota = 一级槽位字内笔数 vs 种子字条目笔数失配数（过省形
    # 别名表后仍不一致才计）；compOut = 槽位离群笔数（笔的切割质心到
    # 本槽成员质心中位的距离 > 1.8×到他槽中心——换家残余探测器）。
    try:
        from .datahub import COMPONENT_ALIASES, COMPONENT_CHAR_EXCEPTIONS
        matches0 = kai.get("matches") or []
        tree = kai.get("structure") or {}

        def _compChar(idx0):
            ch2 = (tree.get("children") or [])
            if idx0 < len(ch2):
                return ch2[idx0].get("char")
            return None

        slotStrokes = {}
        for k in range(len(strokes)):
            p = matches0[k] if k < len(matches0) else None
            if p:
                slotStrokes.setdefault(p[0], []).append(k)
        quotaBad = 0
        for s0, ks in slotStrokes.items():
            cc = _compChar(s0)
            if not cc or cc == "？":
                continue
            g2 = hub.geom(cc) if hasattr(hub, "geom") else None
            if not g2:
                continue
            seedN = len(g2["medians"])
            inN = len(ks)
            if inN == seedN:
                continue
            rule = COMPONENT_ALIASES.get(cc)
            if rule and rule.get("inChar") == inN:
                continue
            exc = COMPONENT_CHAR_EXCEPTIONS.get(rec["ch"], [])
            if any(e.get("comp") == cc and e.get("inChar") == inN for e in exc):
                continue
            quotaBad += 1
        rec["m"]["compQuota"] = quotaBad
        cSt = [(_centroid_region(regions[k]) if k < len(regions) else None)
               for k in range(len(strokes))]
        centers = {}
        for s0, ks in slotStrokes.items():
            pts2 = [cSt[k] for k in ks if cSt[k] is not None]
            if len(pts2) >= 2:
                xs = sorted(p[0] for p in pts2)
                ys = sorted(p[1] for p in pts2)
                centers[s0] = (xs[len(xs) // 2], ys[len(ys) // 2])
        outN = 0
        for s0, ks in slotStrokes.items():
            if s0 not in centers:
                continue
            for k in ks:
                if cSt[k] is None:
                    continue
                dOwn = dist(cSt[k], centers[s0])
                for s1, c1 in centers.items():
                    if s1 == s0:
                        continue
                    if dist(cSt[k], c1) * 1.8 < dOwn:
                        outN += 1
                        break
        rec["m"]["compOut"] = outN
    except Exception:
        pass

    # ---- OVERLAP：大重叠但楷体不相交
    overlapBad = []
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            a, b = regions[i], regions[j]
            if a is None or b is None:
                continue
            ba, bb = a.bounds, b.bounds
            if ba[2] < bb[0] or bb[2] < ba[0] or ba[3] < bb[1] or bb[3] < ba[1]:
                continue
            inter = a.intersection(b).area
            ratio = inter / max(1.0, min(a.area, b.area))
            if ratio > OVERLAP_RATIO and i < len(cKai) and j < len(cKai):
                if _medianMinDist(kai["medians"][i], kai["medians"][j]) > OVERLAP_KAI_D:
                    overlapBad.append("%d-%d:%d%%" % (i, j, round(ratio * 100)))
    if overlapBad:
        fail("OVERLAP", " ".join(overlapBad[:8]))

    # ---- AREA：面积占比 vs 楷体占比
    kaiRegions = [evenOddRegion(p) for p in kai["strokes"]]
    kaiAreas = [(g.area if g is not None else 0.0) for g in kaiRegions]
    kaiTotal = sum(kaiAreas) or 1.0
    tgtAreas = [(g.area if g is not None else 0.0) for g in regions]
    tgtTotal = sum(tgtAreas) or 1.0
    areaBad = []
    for i in range(min(len(kaiAreas), len(tgtAreas))):
        sk = kaiAreas[i] / kaiTotal
        st = tgtAreas[i] / tgtTotal
        if max(sk, st) < AREA_MIN_SHARE:
            continue
        if st > sk * AREA_RATIO or st < sk / AREA_RATIO:
            areaBad.append("%d:%.0f%%→%.0f%%" % (i, sk * 100, st * 100))
    if areaBad:
        fail("AREA", " ".join(areaBad[:8]))

    # ---- 质量指标（不判失败，供趋势分析）
    okS = [s for s in strokes if not s["failed"]]
    rec["m"]["retain"] = round(sum(s["retainRatio"] for s in okS) / (len(okS) or 1), 3)
    rec["m"]["clamped"] = sum(1 for s in strokes if s.get("clamped"))
    rec["m"]["kaiTpl"] = sum(1 for s in strokes if "楷" in str(s.get("template", "")))
    rec["ms"] = int((time.time() - t0) * 1000)
    return rec


# ---------------------------------------------------------------- 多进程批量

_CTX = {}


def _initWorker(root, fontPath):
    from .datahub import DataHub
    from .fonthub import FontEntry
    hub = DataHub(root)
    font = FontEntry(fontPath)
    font.buildLibraryB(hub)
    font.completeLibraryB(hub)
    _CTX["hub"] = hub
    _CTX["font"] = font


def _verifyOne(ch):
    hub, font = _CTX["hub"], _CTX["font"]
    if not font.hasChar(ch):
        return json.dumps({"ch": ch, "skip": True}, ensure_ascii=False)
    try:
        return json.dumps(verifyChar(hub, font, ch), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ch": ch, "fails": [{"code": "ERROR",
                          "detail": repr(e)[:200]}], "m": {}},
                          ensure_ascii=False)


def outDirOf(root):
    d = os.path.join(root, "verifyOut")
    os.makedirs(d, exist_ok=True)
    return d


def runFont(root, fontFile, chars, jobs, resume):
    import multiprocessing as mp
    outPath = os.path.join(outDirOf(root), os.path.splitext(fontFile)[0] + ".jsonl")
    done = set()
    if resume and os.path.exists(outPath):
        with open(outPath, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["ch"])
                except Exception:
                    pass
    todo = [c for c in chars if c not in done]
    if not todo:
        print("  %s: 已全部完成(%d)" % (fontFile, len(done)))
        return
    fontPath = os.path.join(root, "Fonts", fontFile)
    t0 = time.time()
    nDone = 0
    mode = "a" if (resume and done) else "w"
    with open(outPath, mode, encoding="utf-8") as out:
        if jobs <= 1:
            _initWorker(root, fontPath)
            for ch in todo:
                out.write(_verifyOne(ch) + "\n")
                nDone += 1
                if nDone % 200 == 0:
                    out.flush()
                    print("  %s: %d/%d (%.0fs)" % (
                        fontFile, nDone, len(todo), time.time() - t0), flush=True)
        else:
            with mp.Pool(jobs, initializer=_initWorker,
                         initargs=(root, fontPath)) as pool:
                for line in pool.imap_unordered(_verifyOne, todo, chunksize=16):
                    out.write(line + "\n")
                    nDone += 1
                    if nDone % 500 == 0:
                        out.flush()
                        print("  %s: %d/%d (%.0fs)" % (
                            fontFile, nDone, len(todo), time.time() - t0), flush=True)
    print("  %s: 完成 %d 字, 耗时 %.0fs" % (fontFile, nDone, time.time() - t0))


# ---------------------------------------------------------------- 汇总报告

FAIL_CODES = ["ERROR", "COUNT", "SPLIT", "UNION", "TYPE", "ORDER", "OVERLAP", "AREA"]


def summarize(root):
    od = outDirOf(root)
    summary = {}
    for fn in sorted(os.listdir(od)):
        if not fn.endswith(".jsonl"):
            continue
        fontName = fn[:-6]
        total = skipped = passed = 0
        byCode = {c: [] for c in FAIL_CODES}
        retainSum = retainN = clampedN = kaiTplN = 0
        failChars = {}
        with open(os.path.join(od, fn), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                total += 1
                if rec.get("skip"):
                    skipped += 1
                    continue
                m = rec.get("m", {})
                if "retain" in m:
                    retainSum += m["retain"]
                    retainN += 1
                clampedN += m.get("clamped", 0)
                kaiTplN += m.get("kaiTpl", 0)
                fails = rec.get("fails", [])
                if not fails:
                    passed += 1
                    continue
                codes = sorted({fl["code"] for fl in fails})
                failChars[rec["ch"]] = codes
                for fl in fails:
                    if fl["code"] in byCode:
                        byCode[fl["code"]].append(rec["ch"])
        tested = total - skipped
        summary[fontName] = {
            "total": total, "skipped": skipped, "tested": tested,
            "passed": passed,
            "passRate": round(passed / tested * 100, 2) if tested else 0,
            "byCode": {c: {"count": len(set(chs)), "chars": "".join(sorted(set(chs)))}
                       for c, chs in byCode.items() if chs},
            "failChars": failChars,
            "avgRetain": round(retainSum / retainN, 3) if retainN else 0,
            "clampedStrokes": clampedN, "kaiTplStrokes": kaiTplN,
        }
    with open(os.path.join(od, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    writeReport(root, summary)
    return summary


def writeReport(root, summary):
    od = outDirOf(root)
    head = ("<!DOCTYPE html><meta charset='utf-8'><title>strokelab verify</title>"
            "<style>body{font-family:sans-serif;margin:16px}table{border-collapse:"
            "collapse}td,th{border:1px solid #bbb;padding:3px 8px;font-size:13px}"
            "details{margin:4px 0}summary{cursor:pointer}.chars{font-size:15px;"
            "line-height:1.7;word-break:break-all;max-width:1100px}</style>"
            "<h2>strokelab 批量校验报告</h2>")
    rows = ["<tr><th>字体</th><th>已测</th><th>通过</th><th>通过率</th>" +
            "".join("<th>%s</th>" % c for c in FAIL_CODES) +
            "<th>均保留</th><th>裁剪笔</th><th>楷体模板笔</th></tr>"]
    for fontName, s in summary.items():
        cells = "".join(
            "<td>%s</td>" % (s["byCode"].get(c, {}).get("count", "") or "·")
            for c in FAIL_CODES)
        rows.append(
            "<tr><td>%s</td><td>%d</td><td>%d</td><td><b>%.2f%%</b></td>%s"
            "<td>%.3f</td><td>%d</td><td>%d</td></tr>" % (
                fontName, s["tested"], s["passed"], s["passRate"], cells,
                s["avgRetain"], s["clampedStrokes"], s["kaiTplStrokes"]))
    body = ["<table>%s</table>" % "".join(rows)]
    for fontName, s in summary.items():
        body.append("<h3>%s</h3>" % fontName)
        for c in FAIL_CODES:
            info = s["byCode"].get(c)
            if not info:
                continue
            body.append(
                "<details><summary>%s × %d</summary>"
                "<div class='chars'>%s</div></details>"
                % (c, info["count"], info["chars"]))
    out = os.path.join(od, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(head + "".join(body))
    print("报告:", out)


# ---------------------------------------------------------------- 楷体分类一致性审计

def _parseIds(s):
    """IDS 分解串 → 树。⿰⿱等二元、⿲⿳三元，其余为叶。"""
    from .classify import IDS_OPS2, IDS_OPS3
    toks = list(s or "")
    pos = [0]

    def node():
        if pos[0] >= len(toks):
            return None
        c = toks[pos[0]]
        pos[0] += 1
        kids = []
        arity = 3 if c in IDS_OPS3 else (2 if c in IDS_OPS2 else 0)
        for _ in range(arity):
            k = node()
            if k is not None:
                kids.append(k)
        return {"c": c, "kids": kids}

    return node()


def _subtreeStr(n):
    if n is None:
        return "？"
    return n["c"] + "".join(_subtreeStr(k) for k in n["kids"])


def auditKai(root):
    """部件一致性审计：MakeMeAHanzi 无逐笔笔画名真值，但 matches 把每笔
    挂到 decomposition 部件——同一部件同一笔位在全库所有字里应分类一致。
    按 (部件, 组内笔位) 聚桶多数投票，少数派=分类器误判（魂3判竖折而
    全库厶/云的同位笔多数判撇折）。每个字自身也作为桶键参与（标准字
    与部件互相印证）。输出 verifyOut/kaiAudit.md + kaiAudit.json。"""
    from collections import Counter, defaultdict
    from .datahub import DataHub
    hub = DataHub(root)
    buckets = defaultdict(list)   # (comp, ordinal) -> [(type, ch, strokeIdx)]
    nCh = nStrokeTotal = 0
    for ch in sorted(hub.graphicsIndex.keys()):
        kai = hub.kai(ch)
        if not kai:
            continue
        nCh += 1
        types = kai["strokeTypes"]
        nStrokeTotal += len(types)
        # 自身桶：字 ch 的第 i 笔
        for i, t in enumerate(types):
            buckets[(ch, i)].append((t, ch, i))
        # 部件桶：matches 路径解析到分解树节点
        entry = hub.dictEntry(ch) or {}
        matches = entry.get("matches") or []
        tree = _parseIds(entry.get("decomposition", ""))
        if tree is None or tree["c"] == "？":
            continue
        ordinalOf = Counter()
        for i, m in enumerate(matches):
            if i >= len(types) or not isinstance(m, list):
                continue
            n = tree
            ok = True
            for step in m:
                if n is None or step >= len(n["kids"]):
                    ok = False
                    break
                n = n["kids"][step]
            if not ok or n is None:
                continue
            comp = _subtreeStr(n)
            if "？" in comp or len(comp) == 0:
                continue
            o = ordinalOf[comp]
            ordinalOf[comp] += 1
            if comp != ch:
                buckets[(comp, o)].append((types[i], ch, i))

    suspects = []
    audited = agreed = 0
    typeNoise = Counter()
    typeSeen = Counter()
    for (comp, o), arr in buckets.items():
        chSet = {c for _, c, _ in arr}
        if len(arr) < 3 or len(chSet) < 2:
            continue
        votes = Counter(t for t, _, _ in arr)
        major, majN = votes.most_common(1)[0]
        if majN < len(arr) * 0.6:
            continue  # 无明确多数（部件在不同字里真有形变），不裁决
        for t, c, i in arr:
            audited += 1
            typeSeen[t] += 1
            if t == major:
                agreed += 1
            else:
                typeNoise[t] += 1
                suspects.append({"ch": c, "stroke": i + 1, "got": t,
                                 "expect": major, "comp": comp,
                                 "votes": "%d/%d" % (majN, len(arr))})
    suspects.sort(key=lambda s: (s["got"], s["ch"]))
    od = outDirOf(root)
    with open(os.path.join(od, "kaiAudit.json"), "w", encoding="utf-8") as f:
        json.dump(suspects, f, ensure_ascii=False, indent=1)
    lines = ["# 楷体分类一致性审计（部件多数投票）", "",
             "全库 %d 字 %d 笔；可裁决样本 %d，其中一致 %d（%.2f%%），"
             "疑似误判 %d。" % (nCh, nStrokeTotal, audited, agreed,
                               agreed / (audited or 1) * 100, len(suspects)),
             "", "## 按类型的疑似误判占比（该类型的'虚假率'）", "",
             "| 分类器输出 | 可裁决笔数 | 少数派(疑误) | 虚假率 |", "|---|---|---|---|"]
    for t, bad in typeNoise.most_common():
        seen = typeSeen[t]
        lines.append("| %s | %d | %d | %.0f%% |" % (t, seen, bad, bad / seen * 100))
    lines += ["", "## 疑似误判清单（字·笔序 分类→多数派 @部件 票数）", ""]
    for s in suspects:
        lines.append("- %s%d %s→%s @%s %s" % (
            s["ch"], s["stroke"], s["got"], s["expect"], s["comp"], s["votes"]))
    with open(os.path.join(od, "kaiAudit.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("审计: %d 字 %d 笔; 可裁决 %d, 一致率 %.2f%%, 疑似误判 %d"
          % (nCh, nStrokeTotal, audited, agreed / (audited or 1) * 100,
             len(suspects)))
    print("报告:", os.path.join(od, "kaiAudit.md"))
    return suspects


# ---------------------------------------------------------------- CLI

def listFonts(root):
    d = os.path.join(root, "Fonts")
    return sorted(f for f in os.listdir(d)
                  if f.lower().endswith((".ttf", ".otf")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--fonts", default="all", help="all 或逗号分隔文件名")
    ap.add_argument("--chars", default="all", help="all 或字符串")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--limit", type=int, default=0, help="每字体最多测多少字")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--audit-kai", action="store_true",
                    help="楷体分类一致性审计（部件多数投票）")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.audit_kai:
        auditKai(root)
        return
    if not a.report_only:
        from .datahub import DataHub
        hub = DataHub(root)
        chars = sorted(hub.graphicsIndex.keys()) if a.chars == "all" else list(a.chars)
        if a.limit:
            chars = chars[:a.limit]
        fonts = listFonts(root) if a.fonts == "all" else a.fonts.split(",")
        print("校验 %d 字体 × %d 字, jobs=%d" % (len(fonts), len(chars), a.jobs))
        for f in fonts:
            runFont(root, f, chars, a.jobs, not a.no_resume)
    s = summarize(root)
    worst = min(s.values(), key=lambda v: v["passRate"], default=None)
    for fontName, v in s.items():
        print("%-36s 测%d 过%d (%.2f%%)" % (fontName, v["tested"],
                                            v["passed"], v["passRate"]))


if __name__ == "__main__":
    main()
