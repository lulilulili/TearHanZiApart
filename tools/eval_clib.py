#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/eval_clib.py — C库（偏旁部件模板层）收益评估（架构评审第6项）。

评估性原型的裁定工具：C库本体在 strokelab/fonthub.py（建库）与
strokelab/pipeline/dbuild.py（注入），开关 CLIB_ENABLE 默认 False。
本脚本只测量、不改算法；数字说话，结论可以是"收益不足不推荐"。

用法：
    python -X utf8 tools/eval_clib.py build              # 建C库并打印五旁条目
    python -X utf8 tools/eval_clib.py eval               # a/b/c 开关对比（全五旁）
    python -X utf8 tools/eval_clib.py eval --radicals 氵扌   # 分片跑（单旁数分钟）
    python -X utf8 tools/eval_clib.py report             # 合并分片 → report.json
    python -X utf8 tools/eval_clib.py bench              # 开着C库跑 bench --check

评估口径（用户裁定）：
  a 一致性收益：五旁各取前 20 个合格成员字（(笔画数,字典序) 序），该旁
    槽位逐笔切割路径做跨字形状相似度（geometry.shapeDescriptor/
    shapeSimilarity 同款，笔位内两两平均），报告开/关C库的组内均值；
  b 质量门：同字集 verifyChar 通过率 开/关对比（不得下降），翻转字点名；
  c 算力：同字集 D构建段计时（timings "解析对齐/D构建" 求和）与整字
    墙钟 开/关对比。
  d coverage preset 的开C对比走 tools/verify_batch.py 子进程
    （环境变量 STROKELAB_CLIB=1），不在本脚本内。

报告：verifyOut/clib_eval/report.json + stdout 表格。
"""

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_FONT = "HarmonyOS_Sans_SC.ttf"
MEMBERS_PER_RADICAL = 20


def buildContext(fontName):
    """DataHub + FontEntry（B库建齐 + C库建好），评估共用入口。"""
    from strokelab import DataHub, FontEntry
    hub = DataHub(ROOT)
    font = FontEntry(os.path.join(ROOT, "Fonts", fontName))
    font.buildLibraryB(hub)
    font.completeLibraryB(hub)
    font.buildLibraryC(hub)
    return hub, font


def cmdBuild(fontName):
    hub, font = buildContext(fontName)
    for e in font._clibAll:
        if e.get("usable"):
            print("%s 载体=%s 槽位=%d 笔数=%d 类型=%s" % (
                e["radical"], e["carrier"], e["slot"], len(e["strokes"]),
                "/".join(s["type"] for s in e["strokes"])))
        else:
            print("%s 不可用: %s" % (e["radical"], e.get("reason", "")))
    return 0


def eligibleMembers(hub, font, entry, limit):
    """该旁前 limit 个合格成员字 → [(字, [槽位笔序])]。
    合格 = 有楷体数据、字体有字形、含本形一级槽位且槽位笔数与 C 条目
    一致（与注入端匹配条件同口径，保证逐笔位跨字可比）、槽外还有别
    的笔（与载体选择同口径，排除 才 这类偏旁变体字）。"""
    from strokelab.fonthub import _clibTopSlot
    radical = entry["radical"]
    need = len(entry["strokes"])
    pairs = []
    for ch in hub.familyChars(radical, "radical"):
        if ch == radical:
            continue
        g = hub.geom(ch)
        if g and len(g["medians"]) > need:
            pairs.append((len(g["medians"]), ch))
    out = []
    for _n, ch in sorted(pairs):
        if len(out) >= limit:
            break
        if not font.hasChar(ch):
            continue
        kai = hub.kai(ch)
        if not kai:
            continue
        slot = _clibTopSlot(kai, radical)
        if slot and len(slot[1]) == need:
            out.append((ch, slot[1]))
    return out


def collectRuns(hub, font, members, enable):
    """字集单遍采集：→ {字: {paths, hits, slotN, vPass, vCodes,
    allPaths}}。paths=槽位逐笔切割路径（failed 笔为 None）；allPaths=
    全字逐笔路径连写指纹（开/关终态几何是否真变了的判据）。
    verifyChar 自己会再跑一遍管线（正确性口径与批量验收一致）。"""
    import strokelab.pipeline as pl
    from strokelab import runPipeline
    from strokelab.verify import verifyChar
    prev = pl.CLIB_ENABLE
    pl.CLIB_ENABLE = enable
    out = {}
    try:
        for ch, strokeIdxs in members:
            r = runPipeline(hub, font, ch)
            rec = {"paths": None, "hits": 0, "slotN": len(strokeIdxs),
                   "vPass": False, "vCodes": ["ERROR"], "allPaths": ""}
            if "error" not in r:
                rec["hits"] = r.get("clibHits", 0)
                rec["paths"] = [None if r["strokes"][k]["failed"]
                                else r["strokes"][k]["path"]
                                for k in strokeIdxs]
                rec["allPaths"] = "|".join(s["path"] for s in r["strokes"])
            try:
                v = verifyChar(hub, font, ch)
                rec["vPass"] = not v["fails"]
                rec["vCodes"] = sorted({f["code"] for f in v["fails"]})
            except Exception as e:
                rec["vCodes"] = ["ERROR:" + repr(e)[:60]]
            out[ch] = rec
    finally:
        pl.CLIB_ENABLE = prev
    return out


def timedRun(hub, font, ch, enable):
    """单字计时跑 → (D构建段ms, 整字墙钟ms)。"""
    import strokelab.pipeline as pl
    from strokelab import runPipeline
    prev = pl.CLIB_ENABLE
    pl.CLIB_ENABLE = enable
    try:
        t0 = time.perf_counter()
        r = runPipeline(hub, font, ch)
        wall = (time.perf_counter() - t0) * 1000.0
    finally:
        pl.CLIB_ENABLE = prev
    db = 0.0
    if "error" not in r:
        db = sum(t[1] for t in r.get("timings", [])
                 if t[0] == "解析对齐/D构建")
    return db, wall


def timingPass(hub, font, members):
    """算力对比专用第三遍：正确性两遍已把 glyph/kai 解析缓存焐热，
    此处逐字 关/开 紧邻交替计时，排除跑序冷热偏置。
    → {"dbuildMsOff":均值, "dbuildMsOn":, "wallMsOff":, "wallMsOn":}"""
    acc = {"dbuildMsOff": 0.0, "dbuildMsOn": 0.0,
           "wallMsOff": 0.0, "wallMsOn": 0.0}
    for ch, _idx in members:
        db, wall = timedRun(hub, font, ch, False)
        acc["dbuildMsOff"] += db
        acc["wallMsOff"] += wall
        db, wall = timedRun(hub, font, ch, True)
        acc["dbuildMsOn"] += db
        acc["wallMsOn"] += wall
    n = len(members) or 1
    return {k: v / n for k, v in acc.items()}


def slotConsistency(members, runs):
    """组内一致性：逐笔位跨字两两 shapeSimilarity 的平均（0..100）。
    failed/缺路径的笔位样本剔除；样本<2 的笔位不计。"""
    from strokelab.geometry import shapeDescriptor, shapeSimilarity
    if not members:
        return None
    slotN = len(members[0][1])
    posMeans = []
    for j in range(slotN):
        descs = []
        for ch, _idx in members:
            rec = runs.get(ch)
            paths = rec and rec["paths"]
            if paths and paths[j]:
                d = shapeDescriptor([paths[j]])
                if d:
                    descs.append(d)
        if len(descs) < 2:
            continue
        sims = [shapeSimilarity(descs[a], descs[b])
                for a in range(len(descs))
                for b in range(a + 1, len(descs))]
        posMeans.append(sum(sims) / len(sims))
    if not posMeans:
        return None
    return sum(posMeans) / len(posMeans)


def totalRow(rows):
    """分片行 → 合计（cmdReport 用）。"""
    total = {
        "passOff": sum(r["passOff"] for r in rows),
        "passOn": sum(r["passOn"] for r in rows),
        "n": sum(r["n"] for r in rows),
        "hitStrokes": sum(r["hitStrokes"] for r in rows),
        "slotStrokes": sum(r["slotStrokes"] for r in rows),
        "changedChars": sum(r["changedChars"] for r in rows),
        "dbuildMsOff": sum(r["dbuildMsOff"] * r["n"] for r in rows),
        "dbuildMsOn": sum(r["dbuildMsOn"] * r["n"] for r in rows),
        "wallMsOff": sum(r["wallMsOff"] * r["n"] for r in rows),
        "wallMsOn": sum(r["wallMsOn"] * r["n"] for r in rows),
    }
    simOff = [r["simOff"] for r in rows if r["simOff"] is not None]
    simOn = [r["simOn"] for r in rows if r["simOn"] is not None]
    total["simOff"] = sum(simOff) / len(simOff) if simOff else None
    total["simOn"] = sum(simOn) / len(simOn) if simOn else None
    return total


def evalRadical(hub, font, entry):
    """单旁全量对比 → 报告行。正确性开/关各一遍（结果与缓存冷热无关），
    计时另走第三遍暖缓存交替采样（timingPass）。changed* = 开C后全字
    终态几何真变了的字数/该旁槽位笔数——C 命中若被后续精调洗回原形，
    收益按 0 计，这个数就是"命中→留痕"的转化率分母。"""
    members = eligibleMembers(hub, font, entry, MEMBERS_PER_RADICAL)
    on = collectRuns(hub, font, members, True)
    off = collectRuns(hub, font, members, False)
    fixed = sorted(ch for ch in off
                   if not off[ch]["vPass"] and on[ch]["vPass"])
    broken = sorted(ch for ch in off
                    if off[ch]["vPass"] and not on[ch]["vPass"])
    changed = sorted(ch for ch in off
                     if off[ch]["allPaths"] != on[ch]["allPaths"])
    row = {
        "radical": entry["radical"],
        "carrier": entry.get("carrier"),
        "chars": "".join(ch for ch, _ in members),
        "n": len(members),
        "slotStrokes": sum(rec["slotN"] for rec in on.values()),
        "hitStrokes": sum(rec["hits"] for rec in on.values()),
        "changedChars": len(changed),
        "changed": "".join(changed),
        "simOff": slotConsistency(members, off),
        "simOn": slotConsistency(members, on),
        "passOff": sum(1 for rec in off.values() if rec["vPass"]),
        "passOn": sum(1 for rec in on.values() if rec["vPass"]),
        "fixed": fixed,
        "broken": broken,
        "brokenCodes": {ch: on[ch]["vCodes"] for ch in broken},
    }
    row.update(timingPass(hub, font, members))
    return row


def fmtSim(v):
    return "%.2f" % v if v is not None else "n/a"


def printRow(row):
    print("%s@%s n=%d 命中笔 %d/%d 几何变化字 %d(%s) | 一致性 关%s→开%s"
          " | 通过 关%d→开%d | D构建ms 关%.1f→开%.1f"
          " | 墙钟ms 关%.0f→开%.0f" % (
              row["radical"], row["carrier"], row["n"],
              row["hitStrokes"], row["slotStrokes"],
              row["changedChars"], row["changed"],
              fmtSim(row["simOff"]), fmtSim(row["simOn"]),
              row["passOff"], row["passOn"],
              row["dbuildMsOff"], row["dbuildMsOn"],
              row["wallMsOff"], row["wallMsOn"]))
    if row["fixed"]:
        print("   修复:", "".join(row["fixed"]))
    if row["broken"]:
        print("   回退:", "".join(row["broken"]), row["brokenCodes"])


def cmdEval(fontName, radicals):
    """radicals=偏旁子集（空=全部五旁）。逐旁写 row_<旁>.json 分片——
    单旁约 20字×2遍×2跑（runPipeline+verifyChar）耗时数分钟，分片可
    分批跑/断点续；跑完用 report 子命令合并出总表。"""
    hub, font = buildContext(fontName)
    outDir = os.path.join(ROOT, "verifyOut", "clib_eval")
    os.makedirs(outDir, exist_ok=True)
    for entry in font._clibAll:
        if radicals and entry["radical"] not in radicals:
            continue
        if not entry.get("usable"):
            print("%s 不可用，跳过: %s" % (entry["radical"],
                                           entry.get("reason", "")))
            continue
        row = evalRadical(hub, font, entry)
        printRow(row)
        p = os.path.join(outDir, "row_%s.json" % entry["radical"])
        with open(p, "w", encoding="utf-8") as f:
            json.dump(row, f, ensure_ascii=False, indent=1)
    return 0


def cmdReport(fontName):
    """合并 row_*.json 分片 → report.json + stdout 总表。"""
    outDir = os.path.join(ROOT, "verifyOut", "clib_eval")
    rows = []
    for fn in sorted(os.listdir(outDir)):
        if fn.startswith("row_") and fn.endswith(".json"):
            with open(os.path.join(outDir, fn), encoding="utf-8") as f:
                rows.append(json.load(f))
    for row in rows:
        printRow(row)
    total = totalRow(rows)
    print("—— 合计 n=%d 命中笔 %d/%d 几何变化字 %d"
          " | 一致性 关%s→开%s | 通过 关%d→开%d"
          " | D构建ms总 关%.0f→开%.0f | 墙钟ms总 关%.0f→开%.0f" % (
              total["n"], total["hitStrokes"], total["slotStrokes"],
              total["changedChars"],
              fmtSim(total["simOff"]), fmtSim(total["simOn"]),
              total["passOff"], total["passOn"],
              total["dbuildMsOff"], total["dbuildMsOn"],
              total["wallMsOff"], total["wallMsOn"]))
    outPath = os.path.join(outDir, "report.json")
    with open(outPath, "w", encoding="utf-8") as f:
        json.dump({"font": fontName, "rows": rows, "total": total},
                  f, ensure_ascii=False, indent=1)
    print("报告:", outPath)
    return 0


def cmdBench():
    """开着 C库 跑 bench --check（质量门 b 的跨字体部分）。环境变量 +
    包属性双开：bench 在本进程内跑，包属性即时生效；环境变量兜底
    可能的子进程语义。"""
    os.environ["STROKELAB_CLIB"] = "1"
    import strokelab.pipeline as pl
    pl.CLIB_ENABLE = True
    from strokelab.bench import cmdCheck
    return int(bool(cmdCheck(ROOT)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=("build", "eval", "report", "bench"))
    ap.add_argument("--font", default=DEFAULT_FONT)
    ap.add_argument("--radicals", default="",
                    help="eval 限定偏旁子集（连写，如 氵扌）；空=全部")
    a = ap.parse_args()
    if a.cmd == "build":
        return cmdBuild(a.font)
    if a.cmd == "eval":
        return cmdEval(a.font, a.radicals)
    if a.cmd == "report":
        return cmdReport(a.font)
    return cmdBench()


if __name__ == "__main__":
    raise SystemExit(main())
