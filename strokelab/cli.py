# -*- coding: utf-8 -*-
"""strokelab.cli — 命令行：单字/批量拆解，输出 JSON 或 SVG。

用法：
    python -m strokelab.cli --root . --font Fonts/HarmonyOS_Sans_SC.ttf \
        --chars 永汉国 --out outDir --svg
"""

import argparse
import json
import os
import sys

from .datahub import DataHub
from .fonthub import FontEntry
from .pipeline import runPipeline

PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#0f9b8e",
           "#f032e6", "#9a6324", "#1f6f43", "#800000", "#2b6fb3", "#808000",
           "#c94f7c", "#5a7d2a", "#7a4fd0", "#b8860b"]


def resultToSvg(result):
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" '
             'width="512" height="512">',
             '<g transform="scale(1,-1) translate(0,-900)">']
    for s in result["strokes"]:
        color = PALETTE[s["index"] % len(PALETTE)]
        parts.append('<path d="%s" fill="%s" fill-opacity="0.85" '
                     'fill-rule="nonzero"/>' % (s["path"], color))
    parts.append("</g></svg>")
    return "\n".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description="汉字矢量笔画拆解")
    ap.add_argument("--root", default=".",
                    help="数据根目录（含 makemeahanzi-master / hanzi_chaizi-master）")
    ap.add_argument("--font", required=True, help="目标字体 ttf/otf 路径")
    ap.add_argument("--chars", required=True, help="要拆解的字符串")
    ap.add_argument("--out", default="out", help="输出目录")
    ap.add_argument("--svg", action="store_true", help="同时输出 SVG")
    ap.add_argument("--no-clamp", action="store_true", help="禁用布尔收口")
    args = ap.parse_args(argv)

    hub = DataHub(args.root)
    fe = FontEntry(args.font)
    fe.buildLibraryB(hub)
    added = fe.completeLibraryB(hub)
    if added:
        print("B库自举补全 %d 类" % len(added))
    os.makedirs(args.out, exist_ok=True)

    for ch in args.chars:
        r = runPipeline(hub, fe, ch, applyBooleanClamp=not args.no_clamp)
        if "error" in r:
            print("跳过 %s: %s" % (ch, r["error"]))
            continue
        base = os.path.join(args.out, "U%04X_%s" % (ord(ch), ch))
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False)
        if args.svg:
            with open(base + ".svg", "w", encoding="utf-8") as f:
                f.write(resultToSvg(r))
        uc = r["unionCheck"]
        print("%s: %d笔 覆盖%.1f%% 溢出%.1f%%" % (
            ch, len(r["strokes"]), uc["cover"], uc["excess"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
