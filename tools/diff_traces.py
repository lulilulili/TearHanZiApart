#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/diff_traces.py — 两次 trace 批跑的逐字决策迹对比（隐性漂移仪）。

diff_runs 只看终态判定，对"锚定变了但终态同判"全盲——而 penMatrix 类
改动（矩阵归并 2b）的主要作用形态正是这种漂移（漂移还会收缩 G2/G3 对
该笔的保护面）。本工具逐字比对决策迹条目**多重集**，产出：

    隐性漂移字清单（终态同判但迹变）/ 迹条目 delta 汇总 / 逐字明细

用法:
    python -X utf8 tools/diff_traces.py <基线.trace.jsonl> <新.trace.jsonl> \
        [--details] [--max-drift N]

jsonl 契约 = tools/trace_stats.py 产物：每行 {"ch", "skip"?} 或
{"ch", "fails":[...], "m":{...}, "trace":[...]}；重复行取最后一条。
迹条目键 = 除 evidence 外全部字段（level/stroke(s)/from/to/action/
adopted/group/withPen/noPen…）——evidence 是浮点证据，数值抖动不算
漂移；条目增删/字段变化/重数变化都算。终态同判 = 通过性一致且失败码
集一致（即 diff_runs 的修复/回退/码型迁移三类都探不到的字）。
退出码：--max-drift 给定且隐性漂移字数超限时非零（默认不设卡，恒 0）。
"""

import argparse
import json
import sys
from collections import Counter


def load(path):
    recs = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            recs[r["ch"]] = r  # 重复行取最后一条
    return recs


def isPass(r):
    return (not r.get("skip")) and not (r.get("fails") or [])


def codes(r):
    return frozenset(f["code"] for f in (r.get("fails") or []))


def entryKey(entry):
    """迹条目→多重集键：剔除 evidence（浮点证据抖动不算漂移），其余
    字段全量参与（排序序列化保证键稳定）。"""
    core = {k: v for k, v in entry.items() if k != "evidence"}
    return json.dumps(core, ensure_ascii=False, sort_keys=True)


def traceBag(r):
    return Counter(entryKey(t) for t in (r.get("trace") or []))


def levelAction(key):
    """多重集键→(level, action) 聚合桶（delta 汇总用）。"""
    d = json.loads(key)
    return d.get("level", "?"), d.get("action", "")


def printBagDiff(ch, bagBase, bagCur):
    """一个字的迹条目增删明细（--details）。"""
    removed = bagBase - bagCur
    added = bagCur - bagBase
    print("漂移 %s:" % ch)
    for key, n in sorted(removed.items()):
        print("  -%s %s" % ("×%d" % n if n > 1 else "", key))
    for key, n in sorted(added.items()):
        print("  +%s %s" % ("×%d" % n if n > 1 else "", key))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("current")
    ap.add_argument("--details", action="store_true",
                    help="逐字打印隐性漂移字的迹条目增删")
    ap.add_argument("--max-drift", type=int, default=None,
                    help="隐性漂移字数门限，超限退出码非零（默认不设卡）")
    a = ap.parse_args()
    base, cur = load(a.baseline), load(a.current)

    common = sorted(set(base) & set(cur))
    onlyBase = sorted(set(base) - set(cur))
    onlyCur = sorted(set(cur) - set(base))

    drift = []          # 终态同判但迹变（diff_runs 盲区，本工具主产出）
    flippedTrace = []   # 终态翻转且迹变（diff_runs 射程，列出供交叉）
    flippedOnly = []    # 终态翻转但迹同（改判发生在迹覆盖面之外）
    aggBase, aggCur = Counter(), Counter()
    tested = tracedB = tracedC = 0
    bags = {}
    for ch in common:
        rb, rc = base[ch], cur[ch]
        if rb.get("skip") or rc.get("skip"):
            continue
        tested += 1
        bagB, bagC = traceBag(rb), traceBag(rc)
        tracedB += bool(bagB)
        tracedC += bool(bagC)
        for key, n in bagB.items():
            aggBase[levelAction(key)] += n
        for key, n in bagC.items():
            aggCur[levelAction(key)] += n
        sameVerdict = isPass(rb) == isPass(rc) and codes(rb) == codes(rc)
        if bagB == bagC:
            if not sameVerdict:
                flippedOnly.append(ch)
            continue
        if sameVerdict:
            drift.append(ch)
            bags[ch] = (bagB, bagC)
        else:
            flippedTrace.append(ch)

    print("基线 %s: 测%d 有迹%d" % (a.baseline, tested, tracedB))
    print("当前 %s: 测%d 有迹%d" % (a.current, tested, tracedC))
    if onlyBase or onlyCur:
        print("仅基线有 %d, 仅当前有 %d（字集不同，按共同集统计）"
              % (len(onlyBase), len(onlyCur)))
    print()
    print("隐性漂移(终态同判但迹变) %d: %s" % (len(drift), "".join(drift)))
    print("终态翻转且迹变 %d: %s" % (len(flippedTrace), "".join(flippedTrace)))
    print("终态翻转但迹同 %d: %s" % (len(flippedOnly), "".join(flippedOnly)))
    print()
    buckets = sorted(set(aggBase) | set(aggCur))
    print("迹条目 delta(级/动作): " + "  ".join(
        "%s:%d→%d(%+d)" % (
            lv + ("." + act if act else ""), aggBase[b], aggCur[b],
            aggCur[b] - aggBase[b])
        for b in buckets for lv, act in [b]))
    if a.details and drift:
        print()
        for ch in drift:
            printBagDiff(ch, *bags[ch])
    over = a.max_drift is not None and len(drift) > a.max_drift
    sys.exit(1 if over else 0)


if __name__ == "__main__":
    main()
