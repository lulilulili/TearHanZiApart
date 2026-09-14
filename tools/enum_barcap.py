#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/enum_barcap.py — barcap 罚(矩阵归并 2b)全库两态前段枚举器。

2a 教训内置(docs/矩阵归并设计.md·实施记录):抽样结论必须全库复核——
sample958 按构造测不到全库全部受影响字。本工具复用 enum_g8.py 的前段
截跑模式,但比 G8 多跑到 **G8.5 执行器**(barcap 影响面=G1 锚定 + G5
门扩展 + G8.5 否决门分道,全部落在 ladderActStage 之前/之内),每字
同进程跑两态(PEN_BARCAP=False/True,包属性赋值即生效),对比前段终态:

    strokeGroup / groupStrokes / medians / initMedians /
    ladderRealign / ladderTouched   (json 序列化逐位比对)

**归纳链**(全库复核的正确性依据):barcap 罚仅名义遍施行(assign.py
seedMedians is None 门),种子遍(重试链/自洽二遍)两态代码路径逐位同
——故"首遍前段终态两态一致 ⇒ verifyChar 全程两态一致";终态不一致的
字才需要全管线两态 verifyChar diff(tools/verify_batch + diff_runs),
翻转清单里回退>0 即不许把开关默认转 True。

产出 verifyOut/barcapenum/<时间戳>-<tag>/:
    <字体>.jsonl        逐字 {ch, pairs(罚命中对数), hitK(命中笔数),
                        shift(G1 锚定漂移笔), endDiff(前段终态两态不同),
                        g5Off/g5On(G5 触发条目数两态)}
    <字体>.hit.txt      罚命中字集(penTags 含 barcap 条目)
    <字体>.shift.txt    G1 锚定漂移字集(匈牙利解两态不同)
    <字体>.enddiff.txt  前段终态漂移字集(需全管线两态 diff 的复核集)

用法:
    python -X utf8 tools/enum_barcap.py --jobs 6            # 全库×两字体
    python -X utf8 tools/enum_barcap.py --limit 200         # 冒烟
    python -X utf8 tools/enum_barcap.py --fonts simsun.ttc \
        --check 基线.trace.jsonl   # 保真度:off 态 G5 迹 vs 全管线基线迹
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_FONTS = "HarmonyOS_Sans_SC.ttf,simsun.ttc"


def _jsonable(o):
    """json 序列化兜底:numpy 标量/数组 → python 数。"""
    tl = getattr(o, "tolist", None)
    if tl is not None:
        return tl()
    return float(o)


def frontState(hub, font, ch, barcap):
    """管线前段(与 runPipeline 编排同序)跑到 G8.5 执行器为止,返回
    (终态快照串, G1锚定表, penTags, G5迹条目数)。缺数据返回 None。
    G8.5 之后各级(G9/G10/迭代/切割/收口)不读 penMatrix/penTags(全仓
    grep 实证),且 barcap 仅名义遍施行——前段终态两态一致即全管线一致。"""
    import strokelab.pipeline as pl
    from strokelab.pipeline import grouping, dbuild, assign, arbitrate
    from strokelab.pipeline.state import PipelineCtx, KaiRef, StrokePose
    kai = hub.kai(ch)
    if not kai:
        return None
    raw = font.glyphContours(ch)
    if not raw:
        return None
    pl.PEN_BARCAP = barcap
    try:
        ctx = PipelineCtx(dataHub=hub, fontEntry=font, ch=ch, raw=raw,
                          kaiRef=KaiRef(kai=kai), pose=StrokePose())
        geom, groups, pose = ctx.geom, ctx.groups, ctx.pose
        kaiRef, cost, diag = ctx.kaiRef, ctx.cost, ctx.diag
        grouping.parseAndMerge(geom, kaiRef, raw)
        diag.startTimer()
        dbuild.run(hub, font, geom, kaiRef, pose)
        grouping.buildTables(geom, groups)
        diag.semanticClaims, tr = assign.run(geom, kaiRef, groups, pose, cost)
        diag.trace += tr
        anchorMap = list(groups.strokeGroup)
        diag.trace += arbitrate.corridorFit(geom, groups, pose, cost)
        diag.trace += arbitrate.componentMate(kaiRef, groups, pose, cost)
        diag.trace += arbitrate.barOverload(geom, kaiRef, groups, pose, cost)
        diag.trace += arbitrate.barTheftSwap(geom, kaiRef, groups, pose, cost)
        diag.trace += arbitrate.axisMisplace(geom, kaiRef, groups)
        diag.slotSwaps, tr = arbitrate.slotSwap(kaiRef, groups, pose, cost)
        diag.trace += tr
        diag.trace += arbitrate.orderPreserve(kaiRef, groups, pose, cost)
        diag.ladderProbe = arbitrate.ladderProbeStage(kaiRef, groups, pose)
        arbitrate.ladderActStage(kaiRef, groups, pose, cost, diag)
    finally:
        pl.PEN_BARCAP = False
    snap = json.dumps(
        {"sg": groups.strokeGroup, "gs": groups.groupStrokes,
         "med": pose.medians, "im": pose.initMedians,
         "lr": diag.ladderRealign,
         "lt": sorted(diag.ladderTouched or [])},
        sort_keys=True, default=_jsonable)
    g5 = [t for t in diag.trace if t.get("level") == "G5"]
    return snap, anchorMap, cost.penTags, g5


def _initWorker(root, fontPath):
    import strokelab.verify as V
    V._initWorker(root, fontPath)


def _enumOne(ch):
    """单字两态前段枚举 → jsonl 行。"""
    import strokelab.verify as V
    hub, font = V._CTX["hub"], V._CTX["font"]
    if not font.hasChar(ch):
        return json.dumps({"ch": ch, "skip": True}, ensure_ascii=False)
    try:
        off = frontState(hub, font, ch, False)
        if off is None:
            return json.dumps({"ch": ch, "skip": True}, ensure_ascii=False)
        on = frontState(hub, font, ch, True)
    except Exception as e:
        return json.dumps({"ch": ch, "err": repr(e)[:200]},
                          ensure_ascii=False)
    snapOff, anchorOff, _tagsOff, g5Off = off
    snapOn, anchorOn, tagsOn, g5On = on
    pairs = hitK = 0
    for k, byG in tagsOn.items():
        n = sum(1 for pens in byG.values() if "barcap" in pens)
        if n:
            pairs += n
            hitK += 1
    shift = [k for k in range(len(anchorOff)) if anchorOff[k] != anchorOn[k]]
    g5OnCore = [t for t in g5On
                if not (t.get("evidence") or {}).get("ext")]
    row = {"ch": ch, "pairs": pairs, "hitK": hitK, "shift": shift,
           "endDiff": snapOff != snapOn,
           "g5Off": len(g5Off), "g5On": len(g5On),
           "g5OnCore": len(g5OnCore)}
    if _CHECK.get("base") is not None:
        b = _CHECK["base"].get(ch)
        if b is not None:
            bG5 = [t for t in (b.get("trace") or [])
                   if t.get("level") == "G5"]
            row["chkBad"] = json.dumps(g5Off, sort_keys=True) != \
                json.dumps(bG5, sort_keys=True)
    return json.dumps(row, ensure_ascii=False)


_CHECK = {"base": None}


def _initWorkerCheck(root, fontPath, checkPath):
    _initWorker(root, fontPath)
    if checkPath:
        base = {}
        with open(checkPath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    base[r["ch"]] = r
        _CHECK["base"] = base


def runFontEnum(fontFile, chars, jobs, outPath, checkPath):
    """一个字体批跑两态前段枚举,jsonl 落盘,返回记录列表。"""
    from multiprocessing import Pool
    fontPath = os.path.join(ROOT, "Fonts", fontFile)
    recs = []
    t0 = time.time()
    with open(outPath, "w", encoding="utf-8") as f:
        pool = None
        if jobs <= 1:
            _initWorkerCheck(ROOT, fontPath, checkPath)
            iterator = map(_enumOne, chars)
        else:
            _initWorker(ROOT, fontPath)  # 预热建库(verify_batch 先例)
            pool = Pool(jobs, initializer=_initWorkerCheck,
                        initargs=(ROOT, fontPath, checkPath))
            iterator = pool.imap_unordered(_enumOne, chars, chunksize=16)
        try:
            for i, line in enumerate(iterator):
                f.write(line + "\n")
                recs.append(json.loads(line))
                if (i + 1) % 500 == 0:
                    f.flush()
                    print("  %s: %d/%d (%.0fs)" % (
                        fontFile, i + 1, len(chars), time.time() - t0),
                        flush=True)
        finally:
            if pool is not None:
                pool.close()
                pool.join()
    print("  %s: 完成 %d 字, 耗时 %.0fs" % (
        fontFile, len(recs), time.time() - t0), flush=True)
    return recs


def summarize(recs):
    """记录表 → (hit, shift, endDiff, errs, chkBad) 字集 + G5 触发计数
    (g5FiredOnCore=剔除扩展门提议后的原判据面口径,降幅按它评)。"""
    hit, shift, endDiff, errs, chkBad = [], [], [], [], []
    g5FiredOff = g5FiredOn = g5FiredOnCore = 0
    for r in recs:
        if r.get("skip"):
            continue
        if r.get("err"):
            errs.append(r["ch"])
            continue
        if r.get("pairs"):
            hit.append(r["ch"])
        if r.get("shift"):
            shift.append(r["ch"])
        if r.get("endDiff"):
            endDiff.append(r["ch"])
        if r.get("chkBad"):
            chkBad.append(r["ch"])
        g5FiredOff += 1 if r.get("g5Off") else 0
        g5FiredOn += 1 if r.get("g5On") else 0
        g5FiredOnCore += 1 if r.get("g5OnCore") else 0
    return ("".join(sorted(hit)), "".join(sorted(shift)),
            "".join(sorted(endDiff)), "".join(sorted(errs)),
            "".join(sorted(chkBad)), g5FiredOff, g5FiredOn, g5FiredOnCore)


def buildChars(charsFile, limit):
    if charsFile:
        raw = open(charsFile, encoding="utf-8").read()
        chars = "".join(ch for ch in raw if not ch.isspace())
        tag = "file"
    else:
        from strokelab.datahub import DataHub
        hub = DataHub(ROOT)
        chars = "".join(sorted(hub.graphicsIndex.keys()))
        tag = "full"
    if limit:
        chars = chars[:limit]
    return chars, tag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonts", default=DEFAULT_FONTS)
    ap.add_argument("--chars-file", help="自定义字集文件(默认全库)")
    ap.add_argument("--limit", type=int, default=0, help="截取前 N 字(冒烟)")
    ap.add_argument("--jobs", type=int,
                    default=max(1, min(8, (os.cpu_count() or 4) - 2)))
    ap.add_argument("--out", help="输出目录;默认 verifyOut/barcapenum/时间戳")
    ap.add_argument("--check", help="trace_stats 迹 jsonl:off 态 G5 迹保真度"
                                    "核对(须单字体、基线为 barcap 关闭态)")
    a = ap.parse_args()

    fonts = [f.strip() for f in a.fonts.split(",") if f.strip()]
    if a.check and len(fonts) != 1:
        raise SystemExit("--check 须配合单字体(--fonts 只给一个)")
    chars, tag = buildChars(a.chars_file, a.limit)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outDir = a.out or os.path.join(ROOT, "verifyOut", "barcapenum",
                                   "%s-%s" % (stamp, tag))
    os.makedirs(outDir, exist_ok=True)
    print("barcap 两态枚举: %d 字(%s) × %s, jobs=%d → %s" % (
        len(chars), tag, ",".join(fonts), a.jobs, outDir), flush=True)

    bad = False
    for fontFile in fonts:
        stem = os.path.splitext(fontFile)[0]
        recs = runFontEnum(fontFile, chars, a.jobs,
                           os.path.join(outDir, stem + ".jsonl"), a.check)
        hit, shift, endDiff, errs, chkBad, g5o, g5n, g5c = summarize(recs)
        for name, val in (("hit", hit), ("shift", shift),
                          ("enddiff", endDiff)):
            with open(os.path.join(outDir, "%s.%s.txt" % (stem, name)),
                      "w", encoding="utf-8") as f:
                f.write(val)
        print("  %s: 罚命中 %d 字 | G1漂移 %d 字 | 前段终态漂移 %d 字 | "
              "G5触发 off %d → on %d (原判据面 %d, %.1f%%↓) | err %d[%s]" % (
                  fontFile, len(hit), len(shift), len(endDiff), g5o, g5n,
                  g5c, (100.0 * (g5o - g5c) / g5o) if g5o else 0.0,
                  len(errs), errs), flush=True)
        if a.check:
            print("  保真度核对(off态 G5 迹): %d 字不一致 %s" % (
                len(chkBad), chkBad), flush=True)
            if chkBad:
                bad = True
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
