#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/audit_accuracy.py — 鸿蒙SC 全库逐字拆分准确性审计(用户裁定:
单纯过门限不够,每字判"准确/可疑/不准确"并归纳原因)。

判据 v1(硬失败 + 七类亚阈信号,阈值为首轮口径,分析期可调):
  硬失败: verify 8 类不变量任一 → 不准确(原因=失败码)
  STARVED    某笔 failed 或 retain==0            → 不准确
  CUT_LOSS   某笔 retain<0.60(重)/0.80(轻)      → 不准确/可疑
  SHAPE_BRK  某笔 shapeSim<30                    → 不准确
  MORPH_BAD  morphBad>0(形态拓扑超预算)          → 不准确
  AXIS_SUB   横/竖主轴偏 15°~32°(亚阈)           → 可疑
  OVERLAP_SUB 楷体不相交笔对重叠 20%~60%(亚阈)   → 可疑
  BRIDGE_HVY 某笔桥数≥3(重构补丁过多)            → 可疑
  TPL_FALLBK 楷体回退模板笔数≥2(D 形态先验缺失)  → 记录性
判定: 任一"不准确"信号→不准确;仅"可疑"信号→可疑;全无→准确。

跑法: 脱离进程,等 strokelab/ 工作区安静(修复器收官)后自动起跑;
逐字追加 verifyOut/accuracy/HarmonyOS_Sans_SC.jsonl(断点续跑);
完毕聚合 verifyOut/accuracy/taxonomy.json + report.md。
"""

import json
import math
import os
import subprocess
import sys
import time
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FONT = "HarmonyOS_Sans_SC.ttf"
OUT = os.path.join(ROOT, "verifyOut", "accuracy")
JSONL = os.path.join(OUT, "HarmonyOS_Sans_SC.jsonl")
WAIT_MAX_S = 4 * 3600

_CTX = {}


def _initWorker(root):
    from strokelab import DataHub, FontEntry
    hub = DataHub(root)
    font = FontEntry(os.path.join(root, "Fonts", FONT))
    font.buildLibraryB(hub)
    font.completeLibraryB(hub)
    _CTX["hub"] = hub
    _CTX["font"] = font


def _axisSubDev(strokes, kai):
    """横/竖笔主轴亚阈偏差(15~32°,>32 已被硬门抓)→ [(笔,偏差)]。"""
    from strokelab.geometry import shapeDescriptor, dist
    bad = []
    for s in strokes:
        if s["failed"] or s["type"] not in ("横", "竖"):
            continue
        d = shapeDescriptor([s["path"]])
        if not d or d["elong"] < 1.8:
            continue
        ang = math.degrees(d["mainAngle"]) % 180.0
        km = kai["medians"][s["index"]]
        if len(km) >= 2 and dist(tuple(km[0]), tuple(km[-1])) > 1e-6:
            ref = math.degrees(math.atan2(km[-1][1] - km[0][1],
                                          km[-1][0] - km[0][0])) % 180.0
        else:
            ref = 0.0 if s["type"] == "横" else 90.0
        dev = abs(ang - ref)
        dev = min(dev, 180.0 - dev)
        if 15.0 < dev <= 32.0:
            bad.append((s["index"], round(dev, 1)))
    return bad


def _overlapSub(strokes, kai):
    """楷体不相交笔对的亚阈重叠(20%~60%)→ [(i,j,占比)]。"""
    from strokelab import boolean as bl
    from strokelab.geometry import dist
    kaiM = kai["medians"]
    regs = {}
    for s in strokes:
        if not s["failed"] and s["path"]:
            try:
                regs[s["index"]] = bl._pathRegion(s["path"])
            except Exception:
                pass
    out = []
    idxs = sorted(regs)
    for a in range(len(idxs)):
        for b in range(a + 1, len(idxs)):
            i, j = idxs[a], idxs[b]
            # 楷体相交的笔对本就允许重叠,跳过(粗判:中轴最近距<40)
            mi, mj = kaiM[i], kaiM[j]
            near = min(dist(p, q) for p in mi[::3] for q in mj[::3])
            if near < 40.0:
                continue
            ri, rj = regs[i], regs[j]
            try:
                inter = ri.intersection(rj).area
            except Exception:
                continue
            small = min(ri.area, rj.area) or 1.0
            ratio = inter / small
            if 0.20 < ratio <= 0.60:
                out.append((i, j, round(ratio, 2)))
    return out


def auditOne(ch):
    from strokelab.pipeline import runPipeline
    from strokelab.verify import verifyChar
    hub, font = _CTX["hub"], _CTX["font"]
    if not font.hasChar(ch):
        return json.dumps({"ch": ch, "skip": True}, ensure_ascii=False)
    try:
        r = runPipeline(hub, font, ch)
    except Exception as e:
        return json.dumps({"ch": ch, "verdict": "不准确",
                           "reasons": ["ERROR:" + repr(e)[:80]]},
                          ensure_ascii=False)
    rec = {"ch": ch, "reasons": [], "susp": []}
    if "error" in r:
        rec["verdict"] = "不准确"
        rec["reasons"].append("ERROR:" + str(r["error"])[:80])
        return json.dumps(rec, ensure_ascii=False)
    v = verifyChar(hub, font, ch, result=r)
    for f in v.get("fails") or []:
        rec["reasons"].append("HARD_%s:%s" % (f["code"], f["detail"][:40]))
    kai = r["kai"]
    tplFallback = 0
    for s in r["strokes"]:
        k = s["index"]
        if s["failed"] or s["retainRatio"] == 0:
            rec["reasons"].append("STARVED:%d" % k)
            continue
        if s["retainRatio"] < 0.60:
            rec["reasons"].append("CUT_LOSS:%d=%.2f" % (k, s["retainRatio"]))
        elif s["retainRatio"] < 0.80:
            rec["susp"].append("CUT_LOSS~:%d=%.2f" % (k, s["retainRatio"]))
        if (s.get("shapeSim") or 0) < 30:
            rec["reasons"].append("SHAPE_BRK:%d=%s" % (k, s.get("shapeSim")))
        if (s.get("bridges") or 0) >= 3:
            rec["susp"].append("BRIDGE_HVY:%d=%d" % (k, s["bridges"]))
        if "楷体中轴线" in (s.get("template") or ""):
            tplFallback += 1
    if (v.get("m") or {}).get("morphBad", 0) > 0:
        rec["reasons"].append("MORPH_BAD:%d" % v["m"]["morphBad"])
    for k, dev in _axisSubDev(r["strokes"], kai):
        rec["susp"].append("AXIS_SUB:%d=%.0f°" % (k, dev))
    for i, j, ratio in _overlapSub(r["strokes"], kai):
        rec["susp"].append("OVERLAP_SUB:%d-%d=%.0f%%" % (i, j, ratio * 100))
    if tplFallback >= 2:
        rec["susp"].append("TPL_FALLBK:%d" % tplFallback)
    rec["m"] = {k2: v["m"].get(k2) for k2 in
                ("retain", "morphJunc", "morphSpur", "compOut", "orderX")}
    rec["verdict"] = ("不准确" if rec["reasons"]
                      else ("可疑" if rec["susp"] else "准确"))
    return json.dumps(rec, ensure_ascii=False)


def waitForQuiet():
    t0 = time.time()
    stable = 0
    while time.time() - t0 < WAIT_MAX_S:
        try:
            dirty = bool(subprocess.run(
                ["git", "status", "--porcelain", "strokelab"], cwd=ROOT,
                capture_output=True, text=True, encoding="utf-8"
            ).stdout.strip())
        except Exception:
            dirty = False
        if not dirty:
            stable += 1
            print("[等待] 安静确认 %d/2" % stable, flush=True)
            if stable >= 2:
                return
        else:
            stable = 0
            print("[等待] strokelab 工作区仍有未提交改动", flush=True)
        time.sleep(300)
    print("[等待] 超时,带当前状态起跑", flush=True)


def aggregate():
    from collections import Counter
    verd = Counter()
    reasonC = Counter()
    suspC = Counter()
    examples = {}
    for line in open(JSONL, encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("skip"):
            continue
        verd[rec["verdict"]] += 1
        for kind, cnt in (("reasons", reasonC), ("susp", suspC)):
            for tag in rec.get(kind) or []:
                key = tag.split(":")[0]
                cnt[key] += 1
                examples.setdefault(key, []).append(rec["ch"])
    out = {"verdicts": dict(verd), "reasonCounts": dict(reasonC),
           "suspCounts": dict(suspC),
           "examples": {k: "".join(v[:30]) for k, v in examples.items()}}
    with open(os.path.join(OUT, "taxonomy.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("聚合完成:", dict(verd), flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    waitForQuiet()
    from strokelab import DataHub
    hub = DataHub(ROOT)
    chars = sorted(hub.graphicsIndex.keys())
    done = set()
    if os.path.exists(JSONL):
        for line in open(JSONL, encoding="utf-8"):
            try:
                done.add(json.loads(line)["ch"])
            except Exception:
                pass
    todo = [c for c in chars if c not in done]
    print("总 %d 字, 已完成 %d, 待跑 %d" % (len(chars), len(done), len(todo)),
          flush=True)
    t0 = time.time()
    with Pool(7, initializer=_initWorker, initargs=(ROOT,)) as pool, \
            open(JSONL, "a", encoding="utf-8") as f:
        for i, line in enumerate(pool.imap_unordered(auditOne, todo,
                                                     chunksize=8)):
            f.write(line + "\n")
            if (i + 1) % 200 == 0:
                f.flush()
                el = time.time() - t0
                print("%d/%d (%.0f 分钟, 预计还需 %.0f 分钟)" % (
                    i + 1, len(todo), el / 60,
                    el / (i + 1) * (len(todo) - i - 1) / 60), flush=True)
    aggregate()


if __name__ == "__main__":
    main()
