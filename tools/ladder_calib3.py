# -*- coding: utf-8 -*-
"""G8.5 梯队探针 v3 标定驱动（标定用，可删）。

用法:
  python -X utf8 tools/ladder_calib3.py family          # 家族25
  python -X utf8 tools/ladder_calib3.py healthy         # 健康点名23
  python -X utf8 tools/ladder_calib3.py sample200       # 通过抽样200
  python -X utf8 tools/ladder_calib3.py 搏鱄問          # 任意字符串
选项:
  -v  逐字打印完整 ladderProbe（含未触发部件不打印；触发者全 dump）
"""
import sys
import os
import json
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from strokelab.datahub import DataHub          # noqa: E402
from strokelab.fonthub import FontEntry        # noqa: E402
import strokelab.pipeline as pl                # noqa: E402
from strokelab.pipeline import runPipeline     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAM = "博啤埤搏濞痺睥碑礴稗縛萆蜱輻郫鎛陴颦髀鱄鼽鼾齄導首"
HEA = "事聿重量善畫甫聞門問間三王言目青且具真亘旦昌書"


def sample200():
    chars = []
    path = os.path.join(ROOT, "verifyOut", "acceptance-e4c9dfd",
                        "HarmonyOS_Sans_SC.jsonl")
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d.get("skip") or d.get("fails"):
            continue
        ch = d["ch"]
        if ch in FAM or ch in HEA:
            continue
        chars.append(ch)
    random.seed(7)
    return "".join(random.sample(chars, 200))


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "family"
    verbose = "-v" in sys.argv
    if which == "family":
        chars = FAM
    elif which == "healthy":
        chars = HEA
    elif which == "sample200":
        chars = sample200()
    else:
        chars = which

    hub = DataHub(ROOT)
    fe = FontEntry(os.path.join(ROOT, "Fonts", "HarmonyOS_Sans_SC.ttf"))
    fe.buildLibraryB(hub)
    fe.completeLibraryB(hub)
    pl.LADDER_PROBE = 2 if verbose else True

    fired = []
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
        lp = r.get("ladderProbe") or []
        hit = [e for e in lp if e.get("fired")]
        if hit:
            fired.append(ch)
        if hit or (verbose and lp):
            print("== %s %s (%.1fs)" % (
                ch, "FIRED" if hit else "clean", time.time() - t0))
            for ent in lp:
                print("   ", json.dumps(ent, ensure_ascii=False))
    print()
    print("触发 %d/%d: %s  (总耗时 %.0fs)" % (
        len(fired), len(chars), "".join(fired), time.time() - t00))


if __name__ == "__main__":
    main()
