#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/g8_demote_archive.py — 矩阵归并 2a 出口物：G8 采纳字（攮/銲）
撤除前后逐笔切割结果归档。

对每个 (字体, 字) 用例在两态（ARB_G8_EXEC=True 基线执行态 / False 撤除
态）各跑一遍管线 + verifyChar，把逐笔切割终态渲染成独立 SVG（逐笔填色，
照 bench/review.html 的画法：同 viewBox/翻转变换/调色板），连同 fails
与 G8 迹对比写 README.md，存 verifyOut/g8_demote/。

单进程运行：包属性赋值 pl.ARB_G8_EXEC 即生效（sys.modules 运行期读取
语义，同 LADDER_PROBE），无需环境变量。

用法:
    python -X utf8 tools/g8_demote_archive.py            # 默认 攮(SC)+銲(simsun)
    python -X utf8 tools/g8_demote_archive.py --cases simsun.ttc:銲
"""

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_CASES = "HarmonyOS_Sans_SC.ttf:攮,simsun.ttc:銲"
SVG_TPL = ('<svg xmlns="http://www.w3.org/2000/svg" '
           'viewBox="-80 -220 1180 1180" width="600" height="600">'
           '<g transform="scale(1,-1) translate(0,-760)">%s</g></svg>')


def renderSvg(strokes):
    """逐笔填色 SVG（bench.writeHtml 同款画法；path 加 title 便于目检
    定位笔序号/类型）。"""
    from strokelab.bench import COLORS
    parts = []
    for s in strokes:
        if not s["path"]:
            continue
        parts.append(
            '<path d="%s" fill="%s" fill-opacity="0.8" fill-rule="nonzero">'
            '<title>%d %s%s</title></path>' % (
                s["path"], COLORS[s["index"] % len(COLORS)], s["index"],
                s["type"], "×" if s["failed"] else ""))
    return SVG_TPL % "".join(parts)


def runOneState(hub, font, ch, execOn):
    """一个态跑 verifyChar+runPipeline，返回 {fails, g8, strokes}；
    跑完恢复包默认值。"""
    import strokelab.pipeline as pl
    import strokelab.verify as V
    saved = pl.ARB_G8_EXEC
    pl.ARB_G8_EXEC = execOn
    try:
        rec = V.verifyChar(hub, font, ch)
        r = pl.runPipeline(hub, font, ch)
    finally:
        pl.ARB_G8_EXEC = saved
    return {"fails": rec["fails"],
            "g8": [t for t in r.get("trace") or []
                   if t.get("level") == "G8"],
            "strokes": r["strokes"]}


def caseReadme(fontFile, ch, states):
    """一个用例的 README 段落（两态 fails/G8 迹对比 + 文件清单）。"""
    stem = os.path.splitext(fontFile)[0]
    lines = ["## %s %s" % (fontFile, ch), ""]
    for tag, label in (("exec", "执行态 ARB_G8_EXEC=True（默认，回退阀后）"),
                       ("demote", "撤除态 ARB_G8_EXEC=False（诊断实验）")):
        st = states[tag]
        codes = "+".join(sorted(f["code"] for f in st["fails"])) or "PASS"
        lines.append("- **%s**：verifyChar=%s → `%s_%s_%s.svg`" % (
            label, codes, ch, stem, tag))
        for f in st["fails"]:
            lines.append("  - %s: %s" % (f["code"], f["detail"]))
        for t in st["g8"]:
            lines.append("  - G8 迹: `%s`"
                         % json.dumps(t, ensure_ascii=False))
    lines.append("")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=DEFAULT_CASES,
                    help="逗号分隔 字体文件:字（默认 %s）" % DEFAULT_CASES)
    ap.add_argument("--out",
                    default=os.path.join(ROOT, "verifyOut", "g8_demote"))
    a = ap.parse_args()
    from strokelab import DataHub, FontEntry
    os.makedirs(a.out, exist_ok=True)
    hub = DataHub(ROOT)
    md = ["# G8 序保持互换撤除实验（矩阵归并 2a）采纳字归档", "",
          "生成 %s。两态逐笔切割终态 SVG（逐笔填色，bench/review.html "
          "同款画法）+ verifyChar fails 与 G8 决策迹对比。全库枚举后"
          "回退阀已扳回执行态为默认（通过→失败 9 字，见 docs/矩阵归并"
          "设计.md 实施记录）；本归档为 2a 出口物与 2b 硬前置。"
          % datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
    fonts = {}
    for case in a.cases.split(","):
        fontFile, ch = case.strip().split(":")
        if fontFile not in fonts:
            font = FontEntry(os.path.join(ROOT, "Fonts", fontFile))
            font.buildLibraryB(hub)
            font.completeLibraryB(hub)
            fonts[fontFile] = font
        font = fonts[fontFile]
        stem = os.path.splitext(fontFile)[0]
        states = {}
        for tag, execOn in (("exec", True), ("demote", False)):
            states[tag] = runOneState(hub, font, ch, execOn)
            svgPath = os.path.join(a.out,
                                   "%s_%s_%s.svg" % (ch, stem, tag))
            with open(svgPath, "w", encoding="utf-8") as f:
                f.write(renderSvg(states[tag]["strokes"]))
            print("写出", svgPath, "fails=",
                  [f["code"] for f in states[tag]["fails"]], flush=True)
        md.extend(caseReadme(fontFile, ch, states))
    readmePath = os.path.join(a.out, "README.md")
    with open(readmePath, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("归档 README:", readmePath)


if __name__ == "__main__":
    main()
