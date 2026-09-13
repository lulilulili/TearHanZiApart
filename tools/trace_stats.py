#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/trace_stats.py — 仲裁 Tracer 胜率表（架构评审#1 配套批跑工具）。

verify 的 jsonl 不存 trace，本工具自带批跑：逐字跑 verifyChar（其内部
的 runPipeline 顶层调用被透明拦截取回 result["trace"]——同一遍管线，
不重复跑），按仲裁级聚合：

    触发字数 / 触发笔次 / 采纳率 / 采纳字通过率 /
    触发字通过率 vs 未触发字通过率 / 采纳字失败码分布

产出 = 后续"低胜率级做矩阵归并"（评审#4）的选型数据依据。

用法：
    python -X utf8 tools/trace_stats.py                        # sample×两字体
    python -X utf8 tools/trace_stats.py --fonts simsun.ttc --limit 100
    python -X utf8 tools/trace_stats.py --chars-file 某字集.txt --jobs 4

字集默认 = verify_batch sample 同款（全库字典序 stride=10 抽样，958 字）；
字体缺字跳过（simsun 扩展区）。多进程照 reverify.py 的 Pool 先例。
输出 verifyOut/traceStats/<时间戳>-<tag>/：<字体>.trace.jsonl 明细 +
traceStats.md 胜率表 + chars.txt。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_FONTS = "HarmonyOS_Sans_SC.ttf,simsun.ttc"
SAMPLE_STRIDE = 10
LEVELS = ["G1", "S1b", "G2", "G3", "G4", "G4v", "G5",
          "G6", "G7", "G8", "G8.5", "G9", "G10"]

# ---------------------------------------------------------------- 批跑 worker

_LAST = {}     # 顶层 runPipeline 最近一次 result（含 trace）
_ORIG = {}     # 被包装前的 runPipeline 本体


def _capturingRunPipeline(*args, **kwargs):
    """runPipeline 透明包装：行为零改变，只截获**顶层**调用（无
    seedMedians）的 result 引用供 trace 读取。自洽二遍/轴向守卫的
    递归重跑带 seedMedians 关键字，不截获——外层返回值最后覆盖，
    与 result["trace"] 的首遍口径一致。"""
    result = _ORIG["runPipeline"](*args, **kwargs)
    if len(args) <= 4 and kwargs.get("seedMedians") is None:
        _LAST["result"] = result
    return result


def _initWorker(root, fontPath):
    """verify._initWorker 建 hub/font 后，包一层 runPipeline 拦截。"""
    import strokelab.verify as V
    import strokelab.pipeline as pl
    V._initWorker(root, fontPath)
    if "runPipeline" not in _ORIG:
        _ORIG["runPipeline"] = pl.runPipeline
        pl.runPipeline = _capturingRunPipeline


def _traceOne(ch):
    """单字：verifyChar 一遍拿判定 + 拦截到的 trace，拼成 jsonl 行。"""
    import strokelab.verify as V
    hub, font = V._CTX["hub"], V._CTX["font"]
    if not font.hasChar(ch):
        return json.dumps({"ch": ch, "skip": True}, ensure_ascii=False)
    _LAST.pop("result", None)
    try:
        rec = V.verifyChar(hub, font, ch)
    except Exception as e:
        rec = {"ch": ch, "fails": [{"code": "ERROR",
                                    "detail": repr(e)[:200]}], "m": {}}
    r = _LAST.get("result")
    rec["trace"] = (r.get("trace") or []) if isinstance(r, dict) else []
    return json.dumps(rec, ensure_ascii=False)


def runFontBatch(fontFile, chars, jobs, outPath):
    """一个字体批跑全字集，jsonl 落盘，返回记录列表。"""
    from multiprocessing import Pool
    fontPath = os.path.join(ROOT, "Fonts", fontFile)
    recs = []
    t0 = time.time()
    with open(outPath, "w", encoding="utf-8") as f:
        pool = None
        if jobs <= 1:
            _initWorker(ROOT, fontPath)
            iterator = map(_traceOne, chars)
        else:
            # 主进程先预热一次建库（verify_batch 先例：Windows spawn 下
            # 防多个 worker 同时发现 B 库缺失而重复建库）
            _initWorker(ROOT, fontPath)
            pool = Pool(jobs, initializer=_initWorker,
                        initargs=(ROOT, fontPath))
            iterator = pool.imap_unordered(_traceOne, chars, chunksize=4)
        try:
            for i, line in enumerate(iterator):
                f.write(line + "\n")
                recs.append(json.loads(line))
                if (i + 1) % 100 == 0:
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


# ---------------------------------------------------------------- 聚合

def _entryStrokes(entry):
    """一条决策迹涉及的笔数（笔次口径）。"""
    ss = entry.get("strokes")
    if ss:
        return len(ss)
    return 1


def _passRate(recList):
    if not recList:
        return None
    return sum(1 for r in recList if not r.get("fails")) / len(recList)


def _fmtRate(v):
    return "%.1f%%" % (v * 100) if v is not None else "—"


def aggregateFont(recs):
    """→ [(级, 行统计 dict)]；行含触发/采纳/通过率/失败码分布。"""
    tested = [r for r in recs if not r.get("skip")]
    rows = []
    for lv in LEVELS:
        fired = [r for r in tested
                 if any(t.get("level") == lv for t in r.get("trace") or [])]
        entries = [t for r in fired for t in r["trace"]
                   if t.get("level") == lv]
        adoptedE = [t for t in entries if t.get("adopted")]
        adoptedChars = [r for r in fired
                        if any(t.get("level") == lv and t.get("adopted")
                               for t in r["trace"])]
        firedSet = {r["ch"] for r in fired}
        others = [r for r in tested if r["ch"] not in firedSet]
        codeDist = Counter()
        failAdopted = []
        for r in adoptedChars:
            if r.get("fails"):
                failAdopted.append(r["ch"])
                for code in sorted({fl["code"] for fl in r["fails"]}):
                    codeDist[code] += 1
        rows.append((lv, {
            "firedChars": len(fired),
            "firedStrokes": sum(_entryStrokes(t) for t in entries),
            "entries": len(entries),
            "adoptedStrokes": sum(_entryStrokes(t) for t in adoptedE),
            "adoptRate": (len(adoptedE) / len(entries)) if entries else None,
            "adoptedChars": len(adoptedChars),
            "passAdopted": _passRate(adoptedChars),
            "passFired": _passRate(fired),
            "passOthers": _passRate(others),
            "codeDist": codeDist,
            "failAdopted": "".join(sorted(failAdopted)),
        }))
    return tested, rows


def renderFontSection(fontFile, tested, rows):
    """一个字体的 markdown 胜率表段落。"""
    lines = []
    overall = _passRate(tested)
    traced = [r for r in tested if r.get("trace")]
    lines.append("## %s（测 %d 字，总通过率 %s；有迹字 %d）" % (
        fontFile, len(tested), _fmtRate(overall), len(traced)))
    lines.append("")
    lines.append("| 级 | 触发字数 | 触发笔次 | 采纳笔次 | 采纳率 | "
                 "采纳字通过率 | 触发字通过率 | 未触发字通过率 | "
                 "采纳失败字失败码 | 采纳失败字 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for lv, s in rows:
        if not s["entries"]:
            lines.append("| %s | 0 | 0 | 0 | — | — | — | %s | — | — |" % (
                lv, _fmtRate(s["passOthers"])))
            continue
        dist = " ".join("%s×%d" % (c, n)
                        for c, n in s["codeDist"].most_common()) or "—"
        failChars = s["failAdopted"] or "—"
        if len(failChars) > 25:
            failChars = failChars[:25] + "…(%d)" % len(s["failAdopted"])
        lines.append(
            "| %s | %d | %d | %d | %s | %s | %s | %s | %s | %s |" % (
                lv, s["firedChars"], s["firedStrokes"], s["adoptedStrokes"],
                _fmtRate(s["adoptRate"]), _fmtRate(s["passAdopted"]),
                _fmtRate(s["passFired"]), _fmtRate(s["passOthers"]),
                dist, failChars))
    lines.append("")
    return lines


# ---------------------------------------------------------------- 字集与主流程

def buildChars(charsFile, limit):
    """默认 sample 同款字集（全库字典序 stride=10）；--chars-file 覆盖。"""
    if charsFile:
        raw = open(charsFile, encoding="utf-8").read()
        chars = "".join(ch for ch in raw if not ch.isspace())
        tag = "file"
    else:
        from strokelab.datahub import DataHub
        hub = DataHub(ROOT)
        chars = "".join(sorted(hub.graphicsIndex.keys()))[::SAMPLE_STRIDE]
        tag = "sample"
    if limit:
        chars = chars[:limit]
    return chars, tag


def gitRev():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonts", default=DEFAULT_FONTS,
                    help="逗号分隔字体文件名（默认 %s）" % DEFAULT_FONTS)
    ap.add_argument("--chars-file", help="自定义字集文件（默认 sample 958）")
    ap.add_argument("--limit", type=int, default=0, help="截取前 N 字（冒烟）")
    ap.add_argument("--jobs", type=int,
                    default=max(1, min(8, (os.cpu_count() or 4) - 2)))
    ap.add_argument("--out", help="输出目录；默认 verifyOut/traceStats/时间戳")
    a = ap.parse_args()

    chars, tag = buildChars(a.chars_file, a.limit)
    fonts = [f.strip() for f in a.fonts.split(",") if f.strip()]
    for f in fonts:
        if not os.path.exists(os.path.join(ROOT, "Fonts", f)):
            raise SystemExit("字体不存在: " + f)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outDir = a.out or os.path.join(ROOT, "verifyOut", "traceStats",
                                   "%s-%s" % (stamp, tag))
    os.makedirs(outDir, exist_ok=True)
    with open(os.path.join(outDir, "chars.txt"), "w", encoding="utf-8") as f:
        f.write(chars)
    print("字集 %d 字(%s) × %s, jobs=%d → %s" % (
        len(chars), tag, ",".join(fonts), a.jobs, outDir), flush=True)

    md = ["# 仲裁 Tracer 胜率表", "",
          "生成 %s · rev %s · 字集 %s(%d 字) · jobs %d · G8_EXEC=%s" % (
              datetime.now().strftime("%Y-%m-%d %H:%M"), gitRev(),
              tag, len(chars), a.jobs,
              os.environ.get("STROKELAB_G8_EXEC", "1")), "",
          "口径：触发=该级病征门控成立并形成改判提议（含被拒绝的，"
          "adopted=False）；笔次=提议涉及笔数；采纳率=采纳条目/触发条目；"
          "通过率=verifyChar 零失败码。trace 为首遍（名义指派）口径。", "",
          "G8 口径注（矩阵归并 2a）：撤除态（STROKELAB_G8_EXEC=0，诊断"
          "实验用，默认为执行态）为**单遍未换状态**口径——原 adopted 条目"
          "改记 action=demoted/adopted=False（采纳笔次恒 0），与执行态"
          "双 pass（swapped 驱动二遍）不逐位对应：首个 adopted↔demoted "
          "一一对应，其后条目因组状态分叉可增减。跨态对比胜率表时 G8 行"
          "按 demoted=原采纳读。", ""]
    for fontFile in fonts:
        outPath = os.path.join(
            outDir, os.path.splitext(fontFile)[0] + ".trace.jsonl")
        recs = runFontBatch(fontFile, chars, a.jobs, outPath)
        tested, rows = aggregateFont(recs)
        md.extend(renderFontSection(fontFile, tested, rows))
    mdPath = os.path.join(outDir, "traceStats.md")
    with open(mdPath, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("\n".join(md))
    print("胜率表:", mdPath)


if __name__ == "__main__":
    main()
