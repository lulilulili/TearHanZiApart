#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/parity_check.py — 重构等价性硬门:逐路径字节级对比。

用法:
    python -X utf8 tools/parity_check.py capture out.json   # 重构前抓基线
    python -X utf8 tools/parity_check.py compare out.json   # 重构后比对

字集 = DEFAULT_CHIPS × (HarmonyOS_Sans_SC, simhei);对比 strokes[].path
字符串完全一致 + ladderRealign/holeCount 一致。任何差异退出码非零。
numpy 向量化与 outlineCenterline 抽取两轮重构均用此法验证(0 差异先例)。
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FONTS = ["HarmonyOS_Sans_SC.ttf", "simhei.ttf"]


def snapshot():
    from strokelab import DataHub, FontEntry, runPipeline
    from strokelab.datahub import DEFAULT_CHIPS
    hub = DataHub(ROOT)
    out = {}
    for fname in FONTS:
        font = FontEntry(os.path.join(ROOT, "Fonts", fname))
        font.buildLibraryB(hub)
        font.completeLibraryB(hub)
        for ch in DEFAULT_CHIPS:
            r = runPipeline(hub, font, ch)
            if "error" in r:
                out["%s|%s" % (fname, ch)] = {"error": r["error"]}
                continue
            out["%s|%s" % (fname, ch)] = {
                "paths": [s["path"] for s in r["strokes"]],
                "ladder": r.get("ladderRealign") or [],
                "holes": r.get("holeCount", 0),
            }
        print("完成", fname, flush=True)
    return out


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("capture", "compare"):
        print(__doc__)
        sys.exit(2)
    mode, path = sys.argv[1], sys.argv[2]
    cur = snapshot()
    if mode == "capture":
        json.dump(cur, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        print("基线已写 %s (%d 例)" % (path, len(cur)))
        return
    base = json.load(open(path, encoding="utf-8"))
    bad = 0
    for k in sorted(set(base) | set(cur)):
        a, b = base.get(k), cur.get(k)
        if a != b:
            bad += 1
            if bad <= 10:
                print("不一致:", k)
    print("%d 例: %d 不一致" % (len(base), bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
