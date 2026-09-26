#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/adjudicate_hnp.py — 横捺撇 75 字队列的部件一致性裁决 → kaiTypeFixes。

背景:楷体分类器把带顿笔肩角的折笔读成方言型"横捺撇"(横+短捺过渡+撇),
全库 75 字。映射表曾把该型指到 U+31D6 ㇖(横钩,无下落臂)——好·子部
㇇ 的撇段整段丢失。数据治本:逐字裁决其真身复合笔形,并入 kaiTypeFixes
(datahub 加载侧只放行复合→复合修正,横捺撇→横撇/横折 合法)。

裁决顺序(与 verify --audit-kai 同一部件桶机制):
 1. 部件多数票:该笔所在 (部件,槽序) 桶里**其他字**同槽笔的类型众数
    (样本≥3、≥60%、复合型且≠横捺撇)——口角族由此判 横折;
 2. 桶不可裁(同为横捺撇=系统性方言,票决原理性失效)→ 部件正形表:
    深层部件属 ㇇ 族(子孚了矦…)→横撇、㇖ 族(你尔欠)→横钩——标准
    笔顺表口径,几何角度无法区分 子㇇(-130°) 与 口角(-116..-134°),
    实测两族区间完全重叠,故以部件身份定型;
 3. 其余默认 横折(队列里全部为口/框角:吴口另臣吕韋登高… 楷体口角
    顿笔肩产生 捺 过渡、右竖内倾读作 撇,末臂净角-116..-134°为证)。

用法:
  python -X utf8 tools/adjudicate_hnp.py            # 干跑,打印裁决表
  python -X utf8 tools/adjudicate_hnp.py --apply    # 并入 strokelab/kaiTypeFixes.json
"""

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strokelab.datahub import DataHub                       # noqa: E402
from strokelab.geometry import resamplePolyline, dist       # noqa: E402
from strokelab.verify import _parseIds, _subtreeStr         # noqa: E402

TARGET = "横捺撇"
SINGLE = set("横竖撇捺点提")
FIXES_PATH = os.path.join(ROOT, "strokelab", "kaiTypeFixes.json")

# 部件正形表(标准笔顺表口径,人工核定):系统性方言族票决原理性失效
# (全库同部件同槽全读横捺撇),真身由部件身份判定。
#   ㇇ 横撇族:子(好孨)/孚(蜉,票决亦横撇)/矦(候,⺁ 顶横撇);
#   ㇖ 横钩族:你(您3=尔2 横钩)/欠(吹4=欠2 横钩)。
# 其余队列部件(吴口另臣吕韋當登高亭喬色袁監㠯…)全为口/框角 → 横折。
CANON_COMP = {"子": "横撇", "了": "横撇", "予": "横撇", "孚": "横撇",
              "矦": "横撇", "⺁": "横撇",
              "你": "横钩", "尔": "横钩", "欠": "横钩"}


def lastArmAngle(median):
    """末臂净角:重采样15点,窗口化切向差找**最后一个**拐角峰(与
    classifyMedian 同参:w=2,峰阈48°),拐角→终点的净方向角。"""
    pts = resamplePolyline([tuple(p) for p in median], 15)
    angles = [math.degrees(math.atan2(pts[i + 1][1] - pts[i][1],
                                      pts[i + 1][0] - pts[i][0]))
              for i in range(len(pts) - 1)]

    def angDiff(a, b):
        d = a - b
        while d > 180:
            d -= 360
        while d < -180:
            d += 360
        return d

    w = 2
    turns = [abs(angDiff(angles[min(len(angles) - 1, i + w)],
                         angles[max(0, i - w)])) for i in range(len(angles))]
    corner = None
    i = 1
    while i < len(angles) - 1:
        if turns[i] > 48 and turns[i] >= turns[i - 1] and turns[i] >= turns[i + 1]:
            corner = i
            i += w
        i += 1
    if corner is None:
        corner = len(pts) // 2
    return math.degrees(math.atan2(pts[-1][1] - pts[corner][1],
                                   pts[-1][0] - pts[corner][0]))


def buildBuckets(hub):
    """(部件,槽序) → [(type, ch, strokeIdx)],与 verify.auditKai 同法。"""
    buckets = defaultdict(list)
    for ch in sorted(hub.graphicsIndex.keys()):
        kai = hub.kai(ch)
        if not kai:
            continue
        types = kai["strokeTypes"]
        entry = hub.dictEntry(ch) or {}
        matches = entry.get("matches") or []
        tree = _parseIds(entry.get("decomposition", ""))
        if tree is None or tree["c"] == "？":
            continue
        ordinalOf = Counter()
        for i, m in enumerate(matches):
            if i >= len(types) or not isinstance(m, list):
                continue
            n = tree
            ok = True
            for step in m:
                if n is None or step >= len(n["kids"]):
                    ok = False
                    break
                n = n["kids"][step]
            if not ok or n is None:
                continue
            comp = _subtreeStr(n)
            if "？" in comp or len(comp) == 0:
                continue
            o = ordinalOf[comp]
            ordinalOf[comp] += 1
            if comp != ch:
                buckets[(comp, o)].append((types[i], ch, i))
    return buckets


def compChain(hub, ch, i):
    """该笔 matches 深路径上的部件链(浅→深),供正形表匹配。"""
    kai = hub.kai(ch)
    entry = hub.dictEntry(ch) or {}
    deep = (kai["matches"] or [])[i] if i < len(kai["matches"] or []) else None
    tree = _parseIds(entry.get("decomposition", ""))
    chain = []
    n = tree
    if deep and tree:
        for step in deep:
            if n is None or step >= len(n["kids"]):
                break
            n = n["kids"][step]
            chain.append(_subtreeStr(n))
    return chain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    hub = DataHub(ROOT)
    queue = []      # (ch, strokeIdx)
    for ch in sorted(hub.graphicsIndex.keys()):
        kai = hub.kai(ch)
        if not kai:
            continue
        for i, t in enumerate(kai["strokeTypes"]):
            if t == TARGET:
                queue.append((ch, i))
    print("队列: %d 笔 / %d 字" % (len(queue), len({c for c, _ in queue})))
    buckets = buildBuckets(hub)
    # 反向索引:该笔属于哪个部件桶
    strokeBucket = {}
    for key, arr in buckets.items():
        for t, c, i in arr:
            if t == TARGET:
                strokeBucket[(c, i)] = key

    verdicts = {}
    stats = Counter()
    rows = []
    for ch, i in queue:
        kai = hub.kai(ch)
        verdict, how = None, ""
        key = strokeBucket.get((ch, i))
        if key:
            others = [t for t, c, k in buckets[key] if c != ch]
            if len(others) >= 3:
                top, n = Counter(others).most_common(1)[0]
                if n >= len(others) * 0.6 and top not in SINGLE \
                        and top != TARGET:
                    verdict = top
                    how = "部件票 %s %d/%d @%s" % (top, n, len(others), key[0])
        if verdict is None:
            for comp in compChain(hub, ch, i):
                hit = CANON_COMP.get(comp)
                if hit:
                    verdict = hit
                    how = "正形表 @%s" % comp
                    break
        if verdict is None:
            verdict = "横折"
            how = "默认口/框角(末臂%.0f°)" % lastArmAngle(kai["medians"][i])
        verdicts.setdefault(ch, {})[str(i)] = verdict
        stats[verdict] += 1
        rows.append((ch, i, verdict, how))
    for ch, i, v, how in rows:
        print("%s 笔%d(0基) → %-4s %s" % (ch, i, v, how))
    print("裁决分布:", dict(stats))
    if not a.apply:
        print("(干跑;--apply 并入 kaiTypeFixes.json)")
        return
    with open(FIXES_PATH, encoding="utf-8") as f:
        fixes = json.load(f)
    added = changed = 0
    for ch, m in verdicts.items():
        cur = fixes.setdefault(ch, {})
        for idx, t in m.items():
            if cur.get(idx) != t:
                changed += 1
            if idx not in cur:
                added += 1
            cur[idx] = t
    with open(FIXES_PATH, "w", encoding="utf-8") as f:
        json.dump(fixes, f, ensure_ascii=False, separators=(",", ":"))
    print("已并入 %s: 新增 %d 条 / 覆写 %d 条" % (FIXES_PATH, added, changed))


if __name__ == "__main__":
    main()
