#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/build_radicals.py — 部首语义 registry 生成器（架构项③，确定性两机一致）。

从 makemeahanzi dictionary/graphics 聚合生成 data/radicals.json。每个部首
按 dictionary 的 radical 字段**原样分条**（氵/水/氺 各自成条、不合并计数），
variants 字段只做同源位形标注。条目结构：
    radical        部首本形
    variants       同源位形组（无组则 [自身]）
    count          成员字数（graphicsIndex 有字形且 dictEntry.radical==本形）
    representative 代表字 = 成员中笔画最少者，并列取字典序最小
                   （复用 tools/coverage_set.py 的 (笔画数, 字) 选法）
    positions      该偏旁在成员字 decomposition 首算子槽位的分布占比
                   （左/右/上/下/中/外/内/叠/独体/未定位，比例 3 位小数）
    semanticHints  成员 etymology.hint 出现最多的前 3 个
    effect         {color, particle}：种子表（与 viewer FALLBACK_RULES 一致）
                   或 semanticHints 关键词兜底色；无匹配 → null
    memberSample   按 (笔画数, 字典序) 前 12 个成员字

用法：python -X utf8 tools/build_radicals.py
输出：data/radicals.json（一部首一行，git 可 diff；遍历一律 sorted 保证
      两机字节级一致）——生成后提交入库。
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strokelab.classify import IDS_OPS2, IDS_OPS3   # noqa: E402

IDS_ALL = set(IDS_OPS2 + IDS_OPS3)

# 同源位形分组基表（用户裁定 2026-09-13）：只做 variants 标注，不并条目。
# 阝 一组同录 邑/阜（左右耳旁在 makemeahanzi 里同用 阝 字符）。
VARIANT_GROUPS = [
    ["氵", "水", "氺"], ["亻", "人"], ["忄", "心"], ["扌", "手"],
    ["讠", "言"], ["钅", "金"], ["饣", "食"], ["纟", "糸", "糹"],
    ["艹", "艸"], ["辶", "辵"], ["阝", "邑", "阜"], ["犭", "犬"],
    ["礻", "示"], ["衤", "衣"], ["⺼", "月", "肉"], ["王", "玉"],
    ["灬", "火"], ["刂", "刀"], ["罒", "网"], ["⺮", "竹"],
    ["鸟", "鳥"], ["鱼", "魚"],
]
VARIANT_OF = {}
for _grp in VARIANT_GROUPS:
    for _r in _grp:
        VARIANT_OF[_r] = _grp

# effect 种子表：与 viewer/js/fxSingle.js 的 FALLBACK_RULES 保持一致
SEED_EFFECTS = {}
for _r in "氵水氺":
    SEED_EFFECTS[_r] = {"color": "#1e6fd9", "particle": "water"}
for _r in "灬火":
    SEED_EFFECTS[_r] = {"color": "#d92b2b", "particle": "fire"}
SEED_EFFECTS["鬼"] = {"color": "#1b1b1f", "particle": "smoke"}
for _r in "艹艸":
    SEED_EFFECTS[_r] = {"color": "#2e9e44", "particle": "leaf"}
SEED_EFFECTS["木"] = {"color": "#8b5a2b", "particle": "wood"}

# 兜底色：semanticHints 分词后含关键词（整词匹配，容复数 s；教训：子串匹配
# 曾把 wheat 误中 heat 给 麥/麦 染上火红）→ 色系；顺序即优先级
HINT_COLOR_RULES = [
    (("water", "river"), "#1e6fd9"),            # 蓝系
    (("fire", "heat"), "#d92b2b"),              # 红系
    (("plant", "grass", "tree"), "#2e9e44"),    # 绿系
    (("metal", "gold"), "#c9a227"),             # 金系
    (("earth", "stone"), "#a67c3d"),            # 土黄系
]

# IDS 首算子 → 顶层槽位名（包围类统一 外/内；⿻ 两槽同名 叠）
POS_NAMES = {
    "⿰": ["左", "右"], "⿱": ["上", "下"],
    "⿲": ["左", "中", "右"], "⿳": ["上", "中", "下"],
    "⿴": ["外", "内"], "⿵": ["外", "内"], "⿶": ["外", "内"],
    "⿷": ["外", "内"], "⿸": ["外", "内"], "⿹": ["外", "内"],
    "⿺": ["外", "内"], "⿻": ["叠", "叠"],
}


def collectLeaves(ids, pos):
    """从 ids[pos] 起解析一棵 IDS 子树 → (叶子字符集合, 下一读位)。
    '？' 占位符不计叶子。残缺序列由调用方捕 IndexError。"""
    c = ids[pos]
    pos += 1
    if c in IDS_ALL:
        arity = 3 if c in IDS_OPS3 else 2
        leaves = set()
        for _ in range(arity):
            sub, pos = collectLeaves(ids, pos)
            leaves |= sub
        return leaves, pos
    return ({c} if c != "？" else set()), pos


def topSlotLeaves(decomposition):
    """→ (首算子, [顶层各槽位的叶子集合])；非 IDS/残缺分解 → (None, [])。"""
    if not decomposition or decomposition[0] not in IDS_ALL:
        return None, []
    op = decomposition[0]
    arity = 3 if op in IDS_OPS3 else 2
    slots = []
    pos = 1
    try:
        for _ in range(arity):
            leaves, pos = collectLeaves(decomposition, pos)
            slots.append(leaves)
    except IndexError:          # 数据噪声：IDS 序列不完整 → 按独体计
        return None, []
    return op, slots


def radicalPosition(entry, radical, variantSet):
    """成员字里该偏旁的顶层槽位名。先精确本形匹配，再放宽到同源位形
    （如 radical=水 而分解写作 氺/氵）；两轮都不中 → 未定位。"""
    op, slots = topSlotLeaves(entry.get("decomposition") or "")
    if op is None:
        return "独体"
    names = POS_NAMES[op]
    for i, leaves in enumerate(slots):
        if radical in leaves:
            return names[i]
    for i, leaves in enumerate(slots):
        if leaves & variantSet:
            return names[i]
    return "未定位"


def resolveEffect(radical, semanticHints):
    """种子表优先；否则按 semanticHints 关键词映射兜底色（particle 空）。
    整词匹配（容复数 s），防 wheat→heat 类子串误伤。"""
    if radical in SEED_EFFECTS:
        return dict(SEED_EFFECTS[radical])
    words = set(re.findall(r"[a-z]+", " ".join(semanticHints).lower()))
    for keywords, color in HINT_COLOR_RULES:
        if any(k in words or (k + "s") in words for k in keywords):
            return {"color": color, "particle": None}
    return None


def buildRegistry(hub):
    """→ [registry 条目]，按 radical 字典序。成员口径与 coverage_set 一致：
    graphicsIndex 有字形且 dictEntry 有 radical 字段的字。"""
    membersByRad = {}                        # radical → [(笔画数, 字)]
    for ch in sorted(hub.graphicsIndex):
        e = hub.dictEntry(ch)
        if not e or not e.get("radical"):
            continue
        try:                                 # 轻量取笔画数（同 coverage_set）
            nStrokes = len(json.loads(hub.graphicsIndex[ch])["medians"])
        except Exception:
            continue
        membersByRad.setdefault(e["radical"], []).append((nStrokes, ch))

    entries = []
    for radical in sorted(membersByRad):
        pairs = sorted(membersByRad[radical])        # (笔画, 字典序) 升序
        chars = [ch for _n, ch in pairs]
        variants = VARIANT_OF.get(radical, [radical])
        variantSet = set(variants)
        posCount, hintCount = {}, {}
        for ch in chars:
            e = hub.dictEntry(ch)
            posName = radicalPosition(e, radical, variantSet)
            posCount[posName] = posCount.get(posName, 0) + 1
            hint = (e.get("etymology") or {}).get("hint")
            if hint:
                hintCount[hint] = hintCount.get(hint, 0) + 1
        total = len(chars)
        positions = {p: round(c / total, 3) for p, c in
                     sorted(posCount.items(), key=lambda kv: (-kv[1], kv[0]))}
        semanticHints = [h for h, _c in
                         sorted(hintCount.items(),
                                key=lambda kv: (-kv[1], kv[0]))[:3]]
        entries.append({
            "radical": radical,
            "variants": list(variants),
            "count": total,
            "representative": pairs[0][1],
            "positions": positions,
            "semanticHints": semanticHints,
            "effect": resolveEffect(radical, semanticHints),
            "memberSample": chars[:12],
        })
    return entries


def main():
    from strokelab.datahub import DataHub
    hub = DataHub(ROOT)
    entries = buildRegistry(hub)
    outDir = os.path.join(ROOT, "data")
    os.makedirs(outDir, exist_ok=True)
    outPath = os.path.join(outDir, "radicals.json")
    with open(outPath, "w", encoding="utf-8", newline="\n") as f:
        f.write('{"radicals": [\n')
        f.write(",\n".join(json.dumps(e, ensure_ascii=False) for e in entries))
        f.write("\n]}\n")
    seeds = sum(1 for e in entries if e["effect"] and e["effect"]["particle"])
    tinted = sum(1 for e in entries
                 if e["effect"] and not e["effect"]["particle"])
    plain = len(entries) - seeds - tinted
    grouped = sum(1 for e in entries if len(e["variants"]) > 1)
    print("部首 %d 条 → %s" % (len(entries), outPath))
    print("effect：种子 %d / 兜底色 %d / 无 %d；同源位形标注 %d 条"
          % (seeds, tinted, plain, grouped))
    top = sorted(entries, key=lambda e: (-e["count"], e["radical"]))[:10]
    print("成员数 Top10：" +
          "　".join("%s%d" % (e["radical"], e["count"]) for e in top))


if __name__ == "__main__":
    main()
