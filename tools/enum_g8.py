#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/enum_g8.py — G8 触发/采纳字集全库枚举器（矩阵归并 2a 专项）。

全库跑完整 verifyChar 约 6 小时（sample958 实测 SC 914s + simsun 1280s，
×10 外推），而 G8 决策只依赖管线前段（解析并组→D构建→组表→G0/G1 指派
→G2..G8 仲裁链），不需要 G8.5/锚定/采样迭代/切割/收口/校验。本工具只
跑前段（与 runPipeline 编排逐行同序，见 pipeline/__init__.py），截取
orderPreserve 决策迹——全库枚举成本降一个数量级。

态由 STROKELAB_G8_EXEC 环境变量控制（spawn worker 继承语义同管线，
默认执行态——2a 全库枚举后回退阀已扳回执行）：
    缺省/=1 执行态（互换执行）：adopted 条目 = "会互换"的采纳字集
    =0      撤除态（诊断实验）：demoted 条目 = 降级诊断字集
两态字集应完全一致（首个 adopted↔demoted 一一对应，此前状态逐位相同，
见 arbitrate.py G8 段注释；全库实测 SC19/simsun4 两态同集）。
--check <trace.jsonl> 用 trace_stats 全管线产物核对前段复刻保真度
（逐字 G8 条目全等；须与基线迹同 EXEC 态）。

用法:
    python -X utf8 tools/enum_g8.py --jobs 7                  # 全库×两字体
    STROKELAB_G8_EXEC=0 python -X utf8 tools/enum_g8.py ...   # 撤除态
    python -X utf8 tools/enum_g8.py \
        --fonts simsun.ttc --check 基线.trace.jsonl           # 保真度核对

输出 verifyOut/g8enum/<时间戳>-<tag>/：<字体>.g8.jsonl 明细 +
<字体>.trigger.txt / <字体>.adopted.txt 字集（demoted 记入 adopted 口径）。
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


def frontTrace(hub, font, ch):
    """管线前段（与 runPipeline 编排同序）跑到 G8 orderPreserve 为止，
    返回决策迹；缺楷体数据/缺字形返回 None。G8 之后各级（G8.5/G9/G10/
    迭代/切割）只追加迹条目、不回改已有 G8 条目，故前段迹的 G8 子集与
    全管线首遍迹的 G8 子集逐字全等（--check 实证）。"""
    from strokelab.pipeline import grouping, dbuild, assign, arbitrate
    from strokelab.pipeline.state import PipelineCtx, KaiRef, StrokePose
    kai = hub.kai(ch)
    if not kai:
        return None
    raw = font.glyphContours(ch)
    if not raw:
        return None
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
    diag.trace += arbitrate.corridorFit(geom, groups, pose, cost)
    diag.trace += arbitrate.componentMate(kaiRef, groups, pose, cost)
    diag.trace += arbitrate.barOverload(geom, kaiRef, groups, pose, cost)
    diag.trace += arbitrate.barTheftSwap(geom, kaiRef, groups, pose, cost)
    diag.trace += arbitrate.axisMisplace(geom, kaiRef, groups)
    diag.slotSwaps, tr = arbitrate.slotSwap(kaiRef, groups, pose, cost)
    diag.trace += tr
    diag.trace += arbitrate.orderPreserve(kaiRef, groups, pose, cost)
    return diag.trace


def _initWorker(root, fontPath):
    """worker 初始化：复用 verify._initWorker 建 hub/font（含 B 库预热）。"""
    import strokelab.verify as V
    V._initWorker(root, fontPath)


def _enumOne(ch):
    """单字前段枚举 → jsonl 行 {ch, g8:[G8 条目]}（缺字 skip / 异常 err）。"""
    import strokelab.verify as V
    hub, font = V._CTX["hub"], V._CTX["font"]
    if not font.hasChar(ch):
        return json.dumps({"ch": ch, "skip": True}, ensure_ascii=False)
    try:
        trace = frontTrace(hub, font, ch)
    except Exception as e:
        return json.dumps({"ch": ch, "err": repr(e)[:200]},
                          ensure_ascii=False)
    g8 = [t for t in (trace or []) if t.get("level") == "G8"]
    return json.dumps({"ch": ch, "g8": g8}, ensure_ascii=False)


def runFontEnum(fontFile, chars, jobs, outPath):
    """一个字体批跑前段枚举，jsonl 落盘，返回记录列表。"""
    from multiprocessing import Pool
    fontPath = os.path.join(ROOT, "Fonts", fontFile)
    recs = []
    t0 = time.time()
    with open(outPath, "w", encoding="utf-8") as f:
        pool = None
        if jobs <= 1:
            _initWorker(ROOT, fontPath)
            iterator = map(_enumOne, chars)
        else:
            # 主进程先预热建库（verify_batch 先例：Windows spawn 下防多
            # worker 同时发现 B 库缺失而重复建库）
            _initWorker(ROOT, fontPath)
            pool = Pool(jobs, initializer=_initWorker,
                        initargs=(ROOT, fontPath))
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


def classifyRecs(recs):
    """记录表 → (trigger, adopted, demoted, errs) 字集（字典序字符串）。
    adopted=执行态采纳；demoted=撤除态降级；两态口径下 adopted∪demoted
    即"若执行会互换"的采纳字集（跑哪个态哪个非空）。"""
    trigger, adopted, demoted, errs = [], [], [], []
    for r in recs:
        if r.get("skip"):
            continue
        if r.get("err"):
            errs.append(r["ch"])
            continue
        g8 = r.get("g8") or []
        if not g8:
            continue
        trigger.append(r["ch"])
        if any(t.get("adopted") for t in g8):
            adopted.append(r["ch"])
        if any(t.get("action") == "demoted" for t in g8):
            demoted.append(r["ch"])
    return ("".join(sorted(trigger)), "".join(sorted(adopted)),
            "".join(sorted(demoted)), "".join(sorted(errs)))


def checkAgainstBaseline(recs, baselinePath):
    """保真度核对：前段枚举的逐字 G8 条目 vs trace_stats 全管线迹的 G8
    子集全等（json 序列化逐条比对，顺序也须一致——同一遍历序）。
    返回不一致字清单。"""
    base = {}
    with open(baselinePath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            base[r["ch"]] = r
    bad = []
    for r in recs:
        ch = r.get("ch")
        b = base.get(ch)
        if b is None or r.get("skip") or b.get("skip"):
            continue
        bG8 = [t for t in (b.get("trace") or []) if t.get("level") == "G8"]
        mine = r.get("g8") or []
        if json.dumps(mine, sort_keys=True) != json.dumps(bG8,
                                                          sort_keys=True):
            bad.append(ch)
    return bad


def buildChars(charsFile, limit):
    """默认全库（graphicsIndex 全字典序，无抽样）；--chars-file 覆盖。"""
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
    ap.add_argument("--chars-file", help="自定义字集文件（默认全库）")
    ap.add_argument("--limit", type=int, default=0, help="截取前 N 字（冒烟）")
    ap.add_argument("--jobs", type=int,
                    default=max(1, min(8, (os.cpu_count() or 4) - 2)))
    ap.add_argument("--out", help="输出目录；默认 verifyOut/g8enum/时间戳")
    ap.add_argument("--check", help="trace_stats 迹 jsonl：核对前段复刻"
                                    "保真度（须单字体 + 同 EXEC 态）")
    a = ap.parse_args()

    fonts = [f.strip() for f in a.fonts.split(",") if f.strip()]
    if a.check and len(fonts) != 1:
        raise SystemExit("--check 须配合单字体（--fonts 只给一个）")
    chars, tag = buildChars(a.chars_file, a.limit)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outDir = a.out or os.path.join(ROOT, "verifyOut", "g8enum",
                                   "%s-%s" % (stamp, tag))
    os.makedirs(outDir, exist_ok=True)
    execState = os.environ.get("STROKELAB_G8_EXEC", "1")
    print("G8 枚举: %d 字(%s) × %s, jobs=%d, G8_EXEC=%s → %s" % (
        len(chars), tag, ",".join(fonts), a.jobs, execState, outDir),
        flush=True)

    for fontFile in fonts:
        stem = os.path.splitext(fontFile)[0]
        recs = runFontEnum(fontFile, chars,
                           a.jobs, os.path.join(outDir, stem + ".g8.jsonl"))
        trigger, adopted, demoted, errs = classifyRecs(recs)
        for name, val in (("trigger", trigger), ("adopted", adopted),
                          ("demoted", demoted)):
            with open(os.path.join(outDir, "%s.%s.txt" % (stem, name)),
                      "w", encoding="utf-8") as f:
                f.write(val)
        print("  %s: 触发 %d 字, adopted %d 字[%s], demoted %d 字[%s]"
              ", err %d[%s]" % (fontFile, len(trigger), len(adopted),
                                adopted, len(demoted), demoted,
                                len(errs), errs), flush=True)
        if a.check:
            bad = checkAgainstBaseline(recs, a.check)
            print("  保真度核对 vs %s: %d 字不一致 %s" % (
                a.check, len(bad), "".join(bad)), flush=True)
            if bad:
                sys.exit(1)


if __name__ == "__main__":
    main()
