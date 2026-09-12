# -*- coding: utf-8 -*-
"""G8.5 标定辅助：dump 单字的笔画/组指派表（标定用，可删）。"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from strokelab.datahub import DataHub          # noqa: E402
from strokelab.fonthub import FontEntry        # noqa: E402
import strokelab.pipeline as pl                # noqa: E402
from strokelab.pipeline import runPipeline     # noqa: E402
from strokelab.geometry import bboxOfPoints    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    chars = sys.argv[1]
    hub = DataHub(ROOT)
    fe = FontEntry(os.path.join(ROOT, "Fonts", "HarmonyOS_Sans_SC.ttf"))
    fe.buildLibraryB(hub)
    fe.completeLibraryB(hub)
    pl.LADDER_PROBE = 2
    for ch in chars:
        r = runPipeline(hub, fe, ch)
        if "error" in r:
            print("== %s error %s" % (ch, r["error"]))
            continue
        kai = r["kai"]
        matches = kai.get("matches") or []
        print("== %s  fails=%s" % (ch, [f["code"] for f in r.get("fails", [])]
                                   if r.get("fails") else "?"))
        g2s = {}
        for s in r["strokes"]:
            g2s.setdefault(s["group"], []).append(s["index"])
        for s in r["strokes"]:
            k = s["index"]
            m = kai["medians"][k]
            cx = sum(p[0] for p in m) / len(m)
            cy = sum(p[1] for p in m) / len(m)
            med = s.get("median") or []
            if med:
                dc = (sum(p[0] for p in med) / len(med),
                      sum(p[1] for p in med) / len(med))
            else:
                dc = (0, 0)
            print("  笔%-2d %-4s slot=%-8s 组%-2d(同组%s) 楷c=(%.0f,%.0f) "
                  "墨c=(%.0f,%.0f)" % (
                      k, kai["strokeTypes"][k],
                      str(matches[k] if k < len(matches) else None),
                      s["group"], g2s.get(s["group"]), cx, cy, dc[0], dc[1]))
        for ent in r.get("ladderProbe") or []:
            print("   LP:", json.dumps(ent, ensure_ascii=False))


if __name__ == "__main__":
    main()
