#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/coverage_set.py — 验收覆盖字集生成器（确定性，两机可复现）。

字集构成（用户验收格式 2026-09-13）：
  ① 偏旁覆盖：295 种偏旁各取一个代表字 = 该偏旁下笔画数最少者
     （几何最干净，暴露该偏旁的基本拆分形态），并列取字典序最小；
  ② 结构覆盖：除"独体"外的每种 IDS 结构（左右/上下/左中右/上中下/
     全包围/上三包围/下三包围/左三包围/左上包围/右上包围/左下包围/
     镶嵌）各取 8 个与①不重复的字——同样按（笔画数, 字典序）取最小，
     偏旁去重优先（8 个字尽量覆盖 8 种不同偏旁，避免结构组内偏旁扎堆）。

用法：
    python -X utf8 tools/coverage_set.py            # 打印统计并写盘
输出：verifyOut/coverageChars.txt（①+②顺序拼接，供 verify_batch
--chars-file 或 coverage preset 使用）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IDS_NAME = {"⿰": "左右", "⿱": "上下", "⿲": "左中右", "⿳": "上中下",
            "⿴": "全包围", "⿵": "上三包围", "⿶": "下三包围",
            "⿷": "左三包围", "⿸": "左上包围", "⿹": "右上包围",
            "⿺": "左下包围", "⿻": "镶嵌"}


def buildCoverageSet(hub, perStruct=8):
    """→ (radicalPart:[(偏旁,字)], structPart:{结构:[字]})"""
    import json as _json
    info = {}
    for ch in sorted(hub.graphicsIndex):
        e = hub.dictEntry(ch)
        if not e:
            continue
        line = hub.graphicsIndex[ch]
        # 轻量取笔画数：medians 数组顶层元素数（免全量 geom 缓存驻留）
        try:
            nStrokes = len(_json.loads(line)["medians"])
        except Exception:
            continue
        d = e.get("decomposition") or ""
        struct = IDS_NAME.get(d[0] if d else "", None)
        info[ch] = (e.get("radical") or "", nStrokes, struct)

    # ① 偏旁代表字
    byRad = {}
    for ch, (rad, n, _s) in info.items():
        if not rad:
            continue
        cur = byRad.get(rad)
        if cur is None or (n, ch) < (info[cur][1], cur):
            byRad[rad] = ch
    radicalPart = sorted(byRad.items())
    chosen = {ch for _r, ch in radicalPart}

    # ② 结构×8（与①不重复；组内偏旁尽量互异）
    structPart = {}
    for sName in IDS_NAME.values():
        cands = sorted((ch for ch, (_r, _n, s) in info.items()
                        if s == sName and ch not in chosen),
                       key=lambda c: (info[c][1], c))
        picked, seenRad = [], set()
        for ch in cands:                     # 第一轮：偏旁互异优先
            if len(picked) >= perStruct:
                break
            if info[ch][0] in seenRad:
                continue
            picked.append(ch)
            seenRad.add(info[ch][0])
        for ch in cands:                     # 第二轮：补足
            if len(picked) >= perStruct:
                break
            if ch not in picked:
                picked.append(ch)
        structPart[sName] = picked
        chosen.update(picked)
    return radicalPart, structPart


def main():
    from strokelab.datahub import DataHub
    hub = DataHub(ROOT)
    radicalPart, structPart = buildCoverageSet(hub)
    chars = "".join(ch for _r, ch in radicalPart)
    for sName in IDS_NAME.values():
        chars += "".join(structPart.get(sName, []))
    out = os.path.join(ROOT, "verifyOut", "coverageChars.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(chars)
    print("① 偏旁代表 %d 字" % len(radicalPart))
    for sName in IDS_NAME.values():
        got = structPart.get(sName, [])
        note = "" if len(got) == 8 else "（仅 %d 可用）" % len(got)
        print("② %-6s %s%s" % (sName, "".join(got), note))
    print("合计 %d 字 → %s" % (len(chars), out))


if __name__ == "__main__":
    main()
