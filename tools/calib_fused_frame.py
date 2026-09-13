# -*- coding: utf-8 -*-
"""G8.5-F 融合框探针/执行器标定驱动（标定用，可删）。

用法:
  python -X utf8 tools/calib_fused_frame.py sick            # 病字集(simsun)
  python -X utf8 tools/calib_fused_frame.py sickSC          # 病字集 SC 对照
  python -X utf8 tools/calib_fused_frame.py healthy         # 融框健康点名(两字体)
  python -X utf8 tools/calib_fused_frame.py sample200       # SC 通过抽样200
  python -X utf8 tools/calib_fused_frame.py crossscan       # simsun cross 失败字融框自查
  python -X utf8 tools/calib_fused_frame.py fix 鬼白 [字体]  # 执行器前后 verify 对照
  python -X utf8 tools/calib_fused_frame.py 任意字串 [字体]
选项:
  -v   dump 全候选(含 preMiss)；-d 探测调试(全候选跑 Voronoi 看行真值)
"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from strokelab.datahub import DataHub            # noqa: E402
from strokelab.fonthub import FontEntry          # noqa: E402
import strokelab.pipeline as pl                  # noqa: E402
from strokelab.pipeline import runPipeline, arbitrate  # noqa: E402
from strokelab.verify import verifyChar          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SICK = "鬼白皃自目倪晚魁魂醒"
# 融框高频健康字（任务指定）
HEA = "昌書重量畫良即食"
CROSS_SIMSUN = os.path.join(ROOT, "verifyOut", "runs",
                            "20260913-194719-cross", "simsun.jsonl")


def loadFont(name):
    fe = FontEntry(os.path.join(ROOT, "Fonts", name))
    hub = DataHub(ROOT)
    fe.buildLibraryB(hub)
    fe.completeLibraryB(hub)
    return hub, fe


def sample200():
    chars = []
    path = os.path.join(ROOT, "verifyOut", "acceptance-e4c9dfd",
                        "HarmonyOS_Sans_SC.jsonl")
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d.get("skip") or d.get("fails"):
            continue
        ch = d["ch"]
        if ch in SICK or ch in HEA:
            continue
        chars.append(ch)
    random.seed(7)
    return "".join(random.sample(chars, 200))


def crossFails():
    chars = []
    for line in open(CROSS_SIMSUN, encoding="utf-8"):
        d = json.loads(line)
        if not d.get("skip") and d.get("fails"):
            chars.append(d["ch"])
    return "".join(chars)


def probeSet(chars, fontName, verbose=False, debug=False):
    """探针跑一遍：统计预筛命中/触发，打印 mode=F 条目。"""
    hub, fe = loadFont(fontName)
    pl.LADDER_PROBE = True
    arbitrate.LADDER_F = 3 if debug else 2
    preHit, fired = [], []
    t00 = time.time()
    for ch in chars:
        t0 = time.time()
        try:
            r = runPipeline(hub, fe, ch)
        except Exception as e:
            print("== %s ERROR %s" % (ch, e))
            continue
        if "error" in r:
            print("== %s skip: %s" % (ch, r["error"]))
            continue
        ents = [e for e in (r.get("ladderProbe") or [])
                if e.get("mode") == "F"]
        pre = [e for e in ents if e.get("pre")]
        hit = [e for e in ents if e.get("fired")]
        if pre:
            preHit.append(ch)
        if hit:
            fired.append(ch)
        if pre or hit or verbose:
            print("== %s %s (%.1fs)" % (
                ch, "FIRED" if hit else ("preHit" if pre else "clean"),
                time.time() - t0))
            for e in ents:
                if e.get("pre") or e.get("fired") or verbose or debug:
                    print("   ", json.dumps(e, ensure_ascii=False))
    n = len(chars)
    print()
    print("预筛命中 %d/%d (%.1f%%): %s" % (
        len(preHit), n, 100.0 * len(preHit) / max(1, n), "".join(preHit)))
    print("触发 %d/%d (%.1f%%): %s  (总耗时 %.0fs)" % (
        len(fired), n, 100.0 * len(fired) / max(1, n), "".join(fired),
        time.time() - t00))


def fixPanel(chars, fontName):
    """执行器前后 verify 对照面板（LADDER_F=0 vs 1）。"""
    hub, fe = loadFont(fontName)
    pl.LADDER_PROBE = True
    rows = []
    for ch in chars:
        arbitrate.LADDER_F = False
        try:
            r0 = verifyChar(hub, fe, ch)
        except Exception as e:
            print(ch, "ERR off:", e)
            continue
        arbitrate.LADDER_F = True
        try:
            r1 = verifyChar(hub, fe, ch)
        except Exception as e:
            print(ch, "ERR on:", e)
            continue
        c0 = "+".join(sorted({f["code"] for f in r0["fails"]})) or "PASS"
        c1 = "+".join(sorted({f["code"] for f in r1["fails"]})) or "PASS"
        arbitrate.LADDER_F = 2
        rr = runPipeline(hub, fe, ch)
        ents = [e for e in (rr.get("ladderProbe") or [])
                if e.get("mode") == "F" and e.get("fired")]
        act = "; ".join("组%d resets%s wander%s" % (
            e["group"], e["resets"], e["wander"]) for e in ents)
        flag = "=" if c0 == c1 else ("FIX" if c1 == "PASS" else
                                     ("BREAK" if c0 == "PASS" else "MIG"))
        rows.append((ch, c0, c1, flag, act))
        print("%s  %-18s -> %-18s %-5s %s" % (ch, c0, c1, flag, act))
    print()
    nFix = sum(1 for r in rows if r[3] == "FIX")
    nBrk = sum(1 for r in rows if r[3] == "BREAK")
    nMig = sum(1 for r in rows if r[3] == "MIG")
    print("FIX %d / BREAK %d / MIG %d / 共 %d" % (
        nFix, nBrk, nMig, len(rows)))


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "sick"
    verbose = "-v" in sys.argv
    debug = "-d" in sys.argv
    args = [a for a in sys.argv[2:] if not a.startswith("-")]
    if which == "sick":
        probeSet(SICK, "simsun.ttc", verbose, debug)
    elif which == "sickSC":
        probeSet(SICK, "HarmonyOS_Sans_SC.ttf", verbose, debug)
    elif which == "healthy":
        for fn in ("simsun.ttc", "HarmonyOS_Sans_SC.ttf"):
            print("---- 健康点名 @", fn)
            probeSet(HEA, fn, verbose, debug)
    elif which == "sample200":
        probeSet(sample200(), "HarmonyOS_Sans_SC.ttf", verbose, debug)
    elif which == "crossscan":
        probeSet(crossFails(), "simsun.ttc", verbose, debug)
    elif which == "fix":
        fixPanel(args[0], args[1] if len(args) > 1 else "simsun.ttc")
    else:
        probeSet(which, args[0] if args else "simsun.ttc", verbose, debug)


if __name__ == "__main__":
    main()
