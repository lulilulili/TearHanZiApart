#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/calib_typecheck.py — 映射取形同型校验(几何对照)标定。

背景:映射表码位错形已两例(捺←0x31D2撇形/横捺撇族←0x31D6横钩形),
取形链此前无形态自洽校验。机制=候选骨架 vs A库同类型参照中轴的归一化
偏差(0.5·均距+0.5·端点距,fonthub._normalizedMedianDev),
MAP_TYPECHECK_DEV 阈值拒收。

本脚本做上岗前标定:
 1. log 模式建全部字体 B 库,收集映射(map*)条目 dev 分布——同时测两种
    归一化(unit=各自bbox拉成单位方阵 / aspect=按最长边等比),供选型;
 2. 重构两例既往错形候选(旧映射),验证其 dev 与健康分布可分;
 3. 输出建议阈值与误拒率。

用法:
  python -X utf8 tools/calib_typecheck.py                # 全字体
  python -X utf8 tools/calib_typecheck.py --fonts simhei.ttf,simsun.ttc
  python -X utf8 tools/calib_typecheck.py --enforce      # 用当前阈值重跑,数拒收

输出: verifyOut/typecheckCalib.json + 控制台摘要。
"""

import argparse
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strokelab.datahub import DataHub                       # noqa: E402
import strokelab.fonthub as fh                              # noqa: E402
from strokelab.fonthub import FontEntry                     # noqa: E402
from strokelab.geometry import (resamplePolyline, dist,     # noqa: E402
                                bboxOfPoints, contourToPath,
                                shapeDescriptor)
from strokelab.classify import CJK_STROKE_NAMES     # noqa: E402


def normDev(skel, ref, mode):
    """两种归一化的均距(弧长等分 24 点,复用 fonthub._samplePolylineN):
    unit=各自 bbox 拉单位方阵;aspect=按最长边等比(平笔不放大厚度噪声)。"""
    if not skel or not ref or len(skel) < 2 or len(ref) < 2:
        return None

    def norm(poly):
        bb = bboxOfPoints(poly)
        w = max(1e-6, bb.x1 - bb.x0)
        h = max(1e-6, bb.y1 - bb.y0)
        if mode == "aspect":
            s = max(w, h)
            return [((p[0] - bb.x0) / s, (p[1] - bb.y0) / s) for p in poly]
        return [((p[0] - bb.x0) / w, (p[1] - bb.y0) / h) for p in poly]

    a = fh._samplePolylineN(norm(skel), 24)
    b = fh._samplePolylineN(norm(ref), 24)
    if a is None or b is None:
        return None
    return sum(dist(a[i], b[i]) for i in range(24)) / 24.0


def refOf(hub, t):
    """精确键 A 参照(与 fonthub._typeConsistencyDev 同口径):方言型
    无参照不检——骨架宽容回退/楷体样例都被标定否决(见 fonthub 注释)。"""
    aList = (hub.libraryA or {}).get(t)
    return aList[0]["median"] if aList else None


def entryDevs(font, hub, e):
    """→ (生产判据 dev=0.5均距+0.5端点距, unit均距, aspect均距)。"""
    ref = refOf(hub, e["type"])
    if ref is None:
        return None, None, None
    try:
        skel = font.ensureSkeleton(e, hub)
    except Exception:
        return None, None, None
    return (fh._normalizedMedianDev(skel, ref),
            normDev(skel, ref, "unit"), normDev(skel, ref, "aspect"))


def badCases(hub):
    """既往两例错形的旧映射候选(码位直取重构,simsun 有码位区)。"""
    ss = FontEntry(os.path.join(ROOT, "Fonts", "simsun.ttc"))
    ss.buildLibraryB(hub)
    out = []

    def cpEntry(t, cp):
        contours = ss.glyphContours(chr(cp))
        if not contours:
            return None
        e = {"type": t,
             "contours": [contourToPath(c["segs"]) for c in contours],
             "source": "旧映射复原 U+%04X(%s)" % (cp, CJK_STROKE_NAMES[cp]),
             "kind": "mapUnicode", "tier": 0}
        e["desc"] = shapeDescriptor(e["contours"])
        return e

    for t, cp in (("捺", 0x31D2), ("横捺撇", 0x31D6), ("横撇", 0x31D6)):
        e = cpEntry(t, cp)
        if e is None:
            out.append({"case": "%s←U+%04X" % (t, cp), "dev": "码位缺字形"})
            continue
        d, u, asp = entryDevs(ss, hub, e)
        out.append({"case": "simsun %s←U+%04X(%s)" % (t, cp,
                                                      CJK_STROKE_NAMES[cp]),
                    "dev": d, "meanUnit": u, "meanAspect": asp,
                    "rejected": (d is not None and fh.MAP_TYPECHECK_DEV
                                 is not None and d > fh.MAP_TYPECHECK_DEV)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonts", default="all")
    ap.add_argument("--enforce", action="store_true",
                    help="用 fonthub 当前阈值建库,统计实际拒收/warnKept")
    a = ap.parse_args()
    hub = DataHub(ROOT)
    fontsDir = os.path.join(ROOT, "Fonts")
    names = sorted(f for f in os.listdir(fontsDir)
                   if f.lower().endswith((".ttf", ".otf", ".ttc"))) \
        if a.fonts == "all" else a.fonts.split(",")
    if not a.enforce:
        fh.MAP_TYPECHECK_DEV = None    # 标定期强制 log 模式
    rows = []
    enforceStats = defaultdict(int)
    for fn in names:
        try:
            font = FontEntry(os.path.join(fontsDir, fn))
            font.buildLibraryB(hub)
        except Exception as e:
            print(fn, "建库失败:", repr(e)[:120])
            continue
        nMap = 0
        for e in font.libraryBAll or []:
            kind = e.get("kind", "")
            if not kind.startswith("map") or kind in ("mapFuse", "mapCompose"):
                continue
            nMap += 1
            tc = e.get("typeCheck", "")
            enforceStats[tc.split(":")[0]] += 1
            d, u, asp = entryDevs(font, hub, e)
            rows.append({"font": fn, "type": e["type"], "kind": kind,
                         "source": e.get("source", ""), "dev": d,
                         "devUnit": u, "devAspect": asp, "typeCheck": tc})
        print("%-40s map条目%3d" % (fn, nMap), flush=True)
    devP = sorted(r["dev"] for r in rows if r["dev"] is not None)
    devU = sorted(r["devUnit"] for r in rows if r["devUnit"] is not None)
    devA = sorted(r["devAspect"] for r in rows if r["devAspect"] is not None)

    def pct(arr, q):
        return round(arr[min(len(arr) - 1, int(q * len(arr)))], 3) if arr else None

    thr = fh.MAP_TYPECHECK_DEV
    nOver = sum(1 for d in devP if thr is not None and d > thr)
    summary = {
        "entries": len(rows), "checkable": len(devP),
        "threshold": thr,
        "prod": {"p50": pct(devP, .5), "p90": pct(devP, .9),
                 "p95": pct(devP, .95), "p99": pct(devP, .99),
                 "max": pct(devP, 1.0), "overThr": nOver,
                 "overThrPct": round(nOver / len(devP) * 100, 2) if devP else 0},
        "unitMean": {"p95": pct(devU, .95), "max": pct(devU, 1.0)},
        "aspectMean": {"p95": pct(devA, .95), "max": pct(devA, 1.0)},
        "enforceStats": dict(enforceStats),
    }
    bad = badCases(hub)
    report = {"summary": summary, "badCases": bad,
              "top": sorted([r for r in rows if r["dev"] is not None],
                            key=lambda r: -r["dev"])[:40],
              "rows": rows}
    od = os.path.join(ROOT, "verifyOut")
    with open(os.path.join(od, "typecheckCalib.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    for c in bad:
        print("既往病例:", json.dumps(c, ensure_ascii=False))
    print("生产判据 dev 最大的 12 条候选:")
    for r in report["top"][:12]:
        print("  %.3f %-5s %-12s %s %s" % (r["dev"], r["type"],
                                           r["font"][:12], r["source"],
                                           r["typeCheck"]))


if __name__ == "__main__":
    main()
