#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/diff_runs.py — 两次校验 run 的逐字翻转对比（修复/回退/码型迁移清单）。

用法:
    python -X utf8 tools/diff_runs.py <基线.jsonl> <新.jsonl> [--details]

输出：修复字串 / 回退字串 / 码型迁移表 / 各码计数 delta / 软指标聚合 delta。
回退字必须逐字列出供目检——cross 门判定（±2 噪声 vs >5 系统性）依赖这份清单。
jsonl 契约：每行 {"ch", "skip"?} 或 {"ch", "fails":[{"code","detail"}], "m":{...}}；
fails==[] 且非 skip 即 PASS。resume 追加可能产生重复行，取最后一条。
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("current")
    ap.add_argument("--details", action="store_true",
                    help="逐字打印 fails 详情")
    a = ap.parse_args()
    base, cur = load(a.baseline), load(a.current)

    common = sorted(set(base) & set(cur))
    onlyBase = sorted(set(base) - set(cur))
    onlyCur = sorted(set(cur) - set(base))

    fixed, broken, migrated, skipChanged = [], [], [], []
    cB, cC = Counter(), Counter()
    mB, mC = Counter(), Counter()
    testedB = testedC = passB = passC = 0
    for ch in common:
        rb, rc = base[ch], cur[ch]
        if rb.get("skip") != rc.get("skip"):
            skipChanged.append(ch)
            continue
        if rb.get("skip"):
            continue
        testedB += 1
        testedC += 1
        pb, pc = isPass(rb), isPass(rc)
        passB += pb
        passC += pc
        for f in (rb.get("fails") or []):
            cB[f["code"]] += 1
        for f in (rc.get("fails") or []):
            cC[f["code"]] += 1
        for k in ("compQuota", "compOut", "orderX", "reclass"):
            mB[k] += (rb.get("m") or {}).get(k, 0) or 0
            mC[k] += (rc.get("m") or {}).get(k, 0) or 0
        if not pb and pc:
            fixed.append(ch)
        elif pb and not pc:
            broken.append(ch)
        elif not pb and not pc and codes(rb) != codes(rc):
            migrated.append((ch, "+".join(sorted(codes(rb))),
                             "+".join(sorted(codes(rc)))))

    print("基线 %s: 测%d 过%d (%.2f%%)" % (
        a.baseline, testedB, passB, passB / testedB * 100 if testedB else 0))
    print("当前 %s: 测%d 过%d (%.2f%%)" % (
        a.current, testedC, passC, passC / testedC * 100 if testedC else 0))
    print("净变化: %+d 字" % (passC - passB))
    print()
    print("修复 %d: %s" % (len(fixed), "".join(fixed)))
    print("回退 %d: %s" % (len(broken), "".join(broken)))
    if migrated:
        print("码型迁移 %d:" % len(migrated))
        for ch, b, c in migrated:
            print("  %s: %s → %s" % (ch, b, c))
    if skipChanged:
        print("缺字状态变化 %d: %s" % (len(skipChanged), "".join(skipChanged)))
    if onlyBase or onlyCur:
        print("仅基线有 %d, 仅当前有 %d（字集不同，按共同集统计）"
              % (len(onlyBase), len(onlyCur)))
    print()
    allCodes = sorted(set(cB) | set(cC))
    print("失败码 delta: " + "  ".join(
        "%s:%d→%d(%+d)" % (c, cB[c], cC[c], cC[c] - cB[c]) for c in allCodes))
    print("软指标 delta: " + "  ".join(
        "%s:%d→%d(%+d)" % (k, mB[k], mC[k], mC[k] - mB[k])
        for k in ("compQuota", "compOut", "orderX", "reclass")))
    if a.details:
        print()
        for ch in broken:
            print("回退 %s: %s" % (ch, json.dumps(
                cur[ch].get("fails"), ensure_ascii=False)))
    # 回退>0 时退出码非零，方便脚本化门禁
    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
