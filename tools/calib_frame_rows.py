#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/calib_frame_rows.py — 融合框组级交叉行标定(G8.5-F 接线前提)。

对抗评审裁定:medialJunctions 的全字级 18/18 不变量不可直接组级接线,
组级须重标定。本脚本对"融合框"组(框笔+≥2根楷体横档融为一个连通组,
如宋体鬼的田部)构建正确 nonzero 组区域,统计交叉点行与楷体档数的
对应关系,产出接线可行性数据。

用法: python -X utf8 tools/calib_frame_rows.py [字体] [字集]
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strokelab import DataHub, FontEntry, runPipeline           # noqa: E402
from strokelab.geometry import medialJunctions, parseContours    # noqa: E402
from strokelab import boolean as bl                              # noqa: E402


def groupRegion(result, gid):
    """按 boolean.glyphRegion 的 nonzero 语义构建单组区域:取该组的
    轮廓子集(result.contours 携带 group/isHole),交给 glyphRegion——
    其"每外环先减己孔再并"语义正是评审要求的正确构造。"""
    cons = []
    for c in result["contours"]:
        if c["group"] != gid:
            continue
        for pc in parseContours(c["path"]):
            pc["isHole"] = c["isHole"]
            pc["group"] = 0          # 单组内局部编号
            cons.append(pc)
    if not cons:
        return None
    region = bl.glyphRegion(cons)
    if region is None or region.is_empty:
        return None
    if hasattr(region, "geoms"):
        region = max(region.geoms, key=lambda g: g.area)
    return region


def clusterRows(junctions, gap=40.0):
    """交叉点按 y 聚类成"行"(带内多交叉点合并):→ [(行y, 点数), ...]"""
    ys = sorted(j[1] for j in junctions)
    rows = []
    for y in ys:
        if rows and y - rows[-1][-1] <= gap:
            rows[-1].append(y)
        else:
            rows.append([y])
    return [(sum(r) / len(r), len(r)) for r in rows]


def main():
    fontFile = sys.argv[1] if len(sys.argv) > 1 else "simsun.ttc"
    chars = sys.argv[2] if len(sys.argv) > 2 else "鬼白自目田日曲典里甫魁魂晚醒"
    hub = DataHub(ROOT)
    font = FontEntry(os.path.join(ROOT, "Fonts", fontFile))
    font.buildLibraryB(hub)
    font.completeLibraryB(hub)
    print("字体:", fontFile)
    for ch in chars:
        if not font.hasChar(ch):
            continue
        r = runPipeline(hub, font, ch)
        if "error" in r:
            print(ch, "ERROR", r["error"])
            continue
        types = r["kai"]["strokeTypes"]
        for g in r["groups"]:
            ks = g["strokes"]
            if len(ks) < 3:
                continue
            frames = [k for k in ks if "折" in types[k]]
            rungs = [k for k in ks if types[k] in ("横", "提")]
            if not frames or len(rungs) < 1:
                continue
            region = groupRegion(r, g["id"])
            if region is None:
                continue
            js = medialJunctions(region)
            rows = clusterRows(js)
            print("%s 组%d[%s] 框%s 档%s → 交叉点%d 行%s" % (
                ch, g["id"],
                " ".join("%d:%s" % (k, types[k]) for k in ks),
                frames, rungs, len(js),
                ["%d@y%d" % (n, y) for y, n in rows]))


if __name__ == "__main__":
    main()
