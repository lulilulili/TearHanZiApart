# -*- coding: utf-8 -*-
"""第二遍：每笔实际墨区（bounds+面积占比） vs 楷体占比，定位吞并/饿死。"""
import json, sys
from strokelab.datahub import DataHub
from strokelab.fonthub import FontEntry
from strokelab.pipeline import runPipeline
from strokelab.verify import evenOddRegion


def main():
    chars = sys.argv[1] if len(sys.argv) > 1 else "搏鱄導痺"
    hub = DataHub(".")
    font = FontEntry("Fonts/HarmonyOS_Sans_SC.ttf")
    font.buildLibraryB(hub)
    font.completeLibraryB(hub)
    for ch in chars:
        r = runPipeline(hub, font, ch)
        if "error" in r:
            print(json.dumps({"ch": ch, "error": r["error"]}, ensure_ascii=False))
            continue
        kai = r["kai"]
        kaiRegs = [evenOddRegion(p) for p in kai["strokes"]]
        kaiAreas = [(g.area if g is not None else 0.0) for g in kaiRegs]
        kaiTot = sum(kaiAreas) or 1.0
        regs = []
        for s in r["strokes"]:
            g = None if s["failed"] else evenOddRegion(s["path"])
            regs.append(g)
        tgtAreas = [(g.area if g is not None else 0.0) for g in regs]
        tgtTot = sum(tgtAreas) or 1.0
        rows = []
        for i, s in enumerate(r["strokes"]):
            g = regs[i]
            kg = kaiRegs[i]
            rows.append({
                "i": i, "type": kai["strokeTypes"][i],
                "kaiShare": round(kaiAreas[i] / kaiTot * 100, 1),
                "tgtShare": round(tgtAreas[i] / tgtTot * 100, 1),
                "tgtBounds": [round(v) for v in g.bounds] if g is not None and not g.is_empty else None,
                "kaiBounds": [round(v) for v in kg.bounds] if kg is not None and not kg.is_empty else None,
                "tgtCentroid": [round(g.centroid.x), round(g.centroid.y)] if g is not None and not g.is_empty else None,
                "kaiCentroid": [round(kg.centroid.x), round(kg.centroid.y)] if kg is not None and not kg.is_empty else None,
            })
        print(json.dumps({"ch": ch, "rows": rows}, ensure_ascii=False))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
