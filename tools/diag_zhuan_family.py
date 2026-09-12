# -*- coding: utf-8 -*-
"""诊断"专族"失败字 搏/鱄/導/痺 在 HarmonyOS_Sans_SC 上的现症。
输出 JSON 到 stdout（逐字），供上层分析。"""
import json, sys

from strokelab.datahub import DataHub
from strokelab.fonthub import FontEntry
from strokelab.verify import verifyChar
from strokelab.pipeline import runPipeline


def strokeCenter(median):
    if not median:
        return None
    xs = [p[0] for p in median]
    ys = [p[1] for p in median]
    return [round(sum(xs) / len(xs), 1), round(sum(ys) / len(ys), 1)]


def main():
    chars = sys.argv[1] if len(sys.argv) > 1 else "搏鱄導痺"
    hub = DataHub(".")
    font = FontEntry("Fonts/HarmonyOS_Sans_SC.ttf")
    font.buildLibraryB(hub)
    font.completeLibraryB(hub)
    for ch in chars:
        out = {"ch": ch}
        try:
            rec = verifyChar(hub, font, ch)
            out["fails"] = rec.get("fails")
            out["m"] = rec.get("m")
        except Exception as e:
            out["verifyError"] = repr(e)[:300]
        try:
            r = runPipeline(hub, font, ch)
            if "error" in r:
                out["pipelineError"] = str(r["error"])
            else:
                ss = []
                for s in r["strokes"]:
                    med = s.get("median") or []
                    ss.append({
                        "index": s["index"],
                        "type": s.get("type"),
                        "retainRatio": round(s.get("retainRatio", 0), 4),
                        "shapeSim": round(s.get("shapeSim", 0), 1),
                        "template": s.get("template"),
                        "group": s.get("group"),
                        "failed": s.get("failed"),
                        "loops": s.get("loops"),
                        "medianStart": [round(v, 1) for v in med[0]] if med else None,
                        "medianEnd": [round(v, 1) for v in med[-1]] if med else None,
                        "center": strokeCenter(med),
                        "width": round(s.get("width", 0), 1) if s.get("width") else s.get("width"),
                    })
                out["strokes"] = ss
                for key in ("groupRemap", "slotSwaps", "semanticClaims"):
                    if key in r:
                        out[key] = r[key]
                # 楷体每笔类型与中轴中心（比较梯级横顺序用）
                kai = r.get("kai") or {}
                kaiMed = kai.get("medians") or []
                out["kai"] = [{
                    "index": i,
                    "type": (kai.get("strokeTypes") or ["?"] * len(kaiMed))[i]
                            if kai.get("strokeTypes") else None,
                    "start": [round(v, 1) for v in m[0]],
                    "end": [round(v, 1) for v in m[-1]],
                    "center": strokeCenter(m),
                } for i, m in enumerate(kaiMed)]
        except Exception as e:
            out["pipelineException"] = repr(e)[:300]
        print(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
