#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/hnp_fix_svg.py — 横捺撇修复目检归档:单字拆解逐笔填色 SVG。

用法:
  python -X utf8 tools/hnp_fix_svg.py before 好      # → verifyOut/hnp_fix/<字体>_好_before.svg
  python -X utf8 tools/hnp_fix_svg.py after  好 蜉 …  # 修复后再跑,同名 _after
  可加 --fonts simhei.ttf,simsun.ttc(默认 simhei/simsun/HarmonyOS/Noto)

两态各归档一次,肉眼对照 好·子部 ㇇ 的撇段是否拿回(修复前撇段整段
判给竖钩/横线化)。逐笔调色板与 bench 目检页同款。
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strokelab.datahub import DataHub          # noqa: E402
from strokelab.fonthub import FontEntry        # noqa: E402
from strokelab.pipeline import runPipeline     # noqa: E402

OUT_DIR = os.path.join(ROOT, "verifyOut", "hnp_fix")
PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0",
           "#f032e6", "#bcf60c", "#9a6324", "#008080", "#e6beff", "#800000"]
DEFAULT_FONTS = ["simhei.ttf", "simsun.ttc", "HarmonyOS_Sans_SC.ttf",
                 "NotoSansSC-VariableFont_wght.ttf"]


def panel(result):
    parts = ['<g transform="scale(1,-1) translate(0,-900)">']
    for s in result["strokes"]:
        if not s.get("path"):
            continue
        parts.append('<path d="%s" fill="%s" fill-opacity="0.8" '
                     'fill-rule="evenodd" stroke="#333" stroke-width="1"/>'
                     % (s["path"], PALETTE[s["index"] % len(PALETTE)]))
        med = s.get("median") or []
        if len(med) >= 2:
            d = "M" + " L".join("%.0f %.0f" % (p[0], p[1]) for p in med)
            parts.append('<path d="%s" fill="none" stroke="#000" '
                         'stroke-width="6" stroke-opacity="0.5"/>' % d)
    parts.append("</g>")
    return "".join(parts)


def legend(result):
    items = []
    for s in result["strokes"]:
        items.append('<tspan fill="%s">%d%s%s </tspan>' % (
            PALETTE[s["index"] % len(PALETTE)], s["index"] + 1, s["type"],
            "×" if s.get("failed") else ""))
    return ('<text x="10" y="-110" font-size="46">%s</text>'
            % "".join(items))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", choices=("before", "after"))
    ap.add_argument("chars", nargs="+")
    ap.add_argument("--fonts", default=",".join(DEFAULT_FONTS))
    a = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    hub = DataHub(ROOT)
    for fn in a.fonts.split(","):
        fe = FontEntry(os.path.join(ROOT, "Fonts", fn))
        fe.buildLibraryB(hub)
        fe.completeLibraryB(hub)
        for ch in a.chars:
            r = runPipeline(hub, fe, ch)
            if "error" in r:
                print(fn, ch, "ERROR", r["error"])
                continue
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" '
                   'viewBox="-60 -180 1200 1280" width="480">%s%s</svg>'
                   % (panel(r), legend(r)))
            stem = os.path.splitext(fn)[0]
            path = os.path.join(OUT_DIR, "%s_%s_%s.svg" % (stem, ch, a.tag))
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
            print("归档", path)


if __name__ == "__main__":
    main()
