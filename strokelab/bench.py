# -*- coding: utf-8 -*-
"""strokelab.bench — 难例逐笔基准。

金标准快照：对难例套件（包围/交叉/同类笔画/小笔画 × 代表字体）保存每笔
矢量路径为 golden；此后每次改动跑 --check，逐笔算与 golden 的面积 IoU，
低于阈值即视为该笔形状回退。golden 带人工核验状态：
  verified   已目检确认正确，IoU 回退视为破坏；
  known_bad  已知错误形态（如黑体中的横折），IoU 变化不计为回退，
             修好后应 --seed 更新并改为 verified；
  unverified 尚未目检，回退仅提示。

用法：
  python -m strokelab.bench --root . --seed          # 生成/更新 golden + 目检页
  python -m strokelab.bench --root . --check         # 逐笔 IoU 对比
  python -m strokelab.bench --root . --html          # 只重新生成目检页
"""

import argparse
import json
import os

from .datahub import DataHub
from .fonthub import FontEntry
from .pipeline import runPipeline
from .geometry import parseContours, flattenSegs

SUITE_FONTS = [
    "HarmonyOS_Sans_SC.ttf",
    "NotoSansSC-VariableFont_wght.ttf",
    "simhei.ttf",
    "ZCOOLXiaoWei-Regular.ttf",
    "ZCOOLKuaiLe-Regular.ttf",
]
SUITE_CHARS = "口日中国十木天交三川林爱汉"

# 本轮会话中已目检确认的用例；--seed 时写入状态
VERIFIED = {("HarmonyOS_Sans_SC.ttf", c) for c in "口中天日国"}
KNOWN_BAD = {("simhei.ttf", "中")}

IOU_THRESHOLD = 0.85

COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0",
          "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff", "#9a6324",
          "#fffac8", "#800000", "#aaffc3"]


def caseKey(font, ch):
    return "%s_U%04X" % (os.path.splitext(font)[0], ord(ch))


def pathToShape(pathD):
    """笔画路径 → shapely 多边形（buffer(0) 收拾自交，多环并集）。"""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polys = []
    for c in parseContours(pathD):
        pts = flattenSegs(c["segs"], 10)
        if len(pts) >= 3:
            p = Polygon(pts).buffer(0)
            if not p.is_empty:
                polys.append(p)
    if not polys:
        return None
    return unary_union(polys)


def strokeIoU(pathA, pathB):
    if pathA == pathB:
        return 1.0
    a, b = pathToShape(pathA), pathToShape(pathB)
    if a is None and b is None:
        return 1.0
    if a is None or b is None:
        return 0.0
    u = a.union(b).area
    if u <= 0:
        return 0.0
    return a.intersection(b).area / u


def runSuite(root):
    hub = DataHub(root)
    results = {}
    for f in SUITE_FONTS:
        fp = os.path.join(root, "Fonts", f)
        if not os.path.exists(fp):
            print("跳过缺失字体:", f)
            continue
        font = FontEntry(fp)
        font.buildLibraryB(hub)
        font.completeLibraryB(hub)  # 与生产路径一致（自举补全）
        for ch in SUITE_CHARS:
            r = runPipeline(hub, font, ch)
            if "error" in r:
                results[(f, ch)] = {"error": r["error"]}
                continue
            results[(f, ch)] = {
                "strokes": [{"index": s["index"], "type": s["type"],
                             "path": s["path"],
                             "retain": round(s["retainRatio"], 3),
                             "failed": s["failed"]} for s in r["strokes"]],
            }
    return results


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def goldenDir(root):
    """golden/目检页固定在仓库 bench/（随版本提交），与数据根目录解耦。"""
    d = os.path.join(REPO_DIR, "bench", "golden")
    os.makedirs(d, exist_ok=True)
    return d


def cmdSeed(root):
    gd = goldenDir(root)
    results = runSuite(root)
    for (f, ch), r in results.items():
        if "error" in r:
            print("ERROR", f, ch, r["error"])
            continue
        key = caseKey(f, ch)
        p = os.path.join(gd, key + ".json")
        status = "unverified"
        if (f, ch) in VERIFIED:
            status = "verified"
        elif (f, ch) in KNOWN_BAD:
            status = "known_bad"
        # 已有 golden 且人工改过状态时保留原状态
        if os.path.exists(p):
            try:
                old = json.load(open(p, encoding="utf-8"))
                if old.get("status") in ("verified", "known_bad"):
                    status = old["status"]
            except Exception:
                pass
        json.dump({"font": f, "ch": ch, "status": status,
                   "strokes": r["strokes"]},
                  open(p, "w", encoding="utf-8"), ensure_ascii=False)
        print("seed", f, ch, status)
    writeHtml(root, results)


def cmdCheck(root):
    gd = goldenDir(root)
    results = runSuite(root)
    rows = []
    broken = 0
    for (f, ch), r in sorted(results.items()):
        key = caseKey(f, ch)
        p = os.path.join(gd, key + ".json")
        if not os.path.exists(p):
            rows.append((f, ch, "NO_GOLDEN", "", ""))
            continue
        g = json.load(open(p, encoding="utf-8"))
        if "error" in r:
            rows.append((f, ch, "ERROR", g["status"], r["error"]))
            broken += 1
            continue
        if len(r["strokes"]) != len(g["strokes"]):
            rows.append((f, ch, "STROKE_COUNT %d→%d" % (
                len(g["strokes"]), len(r["strokes"])), g["status"], ""))
            broken += 1
            continue
        ious = [strokeIoU(a["path"], b["path"])
                for a, b in zip(g["strokes"], r["strokes"])]
        bad = [(i, v) for i, v in enumerate(ious) if v < IOU_THRESHOLD]
        newFail = [s["index"] for s in r["strokes"] if s["failed"]]
        tag = "ok"
        if bad or newFail:
            tag = "DIFF " + " ".join("s%d=%.2f" % (i, v) for i, v in bad)
            if newFail:
                tag += " fail" + str(newFail)
            if g["status"] == "verified":
                broken += 1
                tag = "BROKEN " + tag
        rows.append((f, ch, tag, g["status"],
                     "min=%.2f" % min(ious) if ious else ""))
    wf = max(len(f) for f, _, _, _, _ in rows)
    for f, ch, tag, status, extra in rows:
        print("%-*s %s %-10s %-24s %s" % (wf, f, ch, status, tag, extra))
    print("\n%d 个用例; 破坏(verified 回退/错误): %d" % (len(rows), broken))
    writeHtml(root, results)
    return broken


def writeHtml(root, results):
    gd = goldenDir(root)
    cells = []
    for (f, ch), r in sorted(results.items()):
        key = caseKey(f, ch)
        status = "unverified"
        p = os.path.join(gd, key + ".json")
        if os.path.exists(p):
            try:
                status = json.load(open(p, encoding="utf-8")).get(
                    "status", "unverified")
            except Exception:
                pass
        if "error" in r:
            inner = "<p>ERROR: %s</p>" % r["error"]
        else:
            paths = "".join(
                '<path d="%s" fill="%s" fill-opacity="0.8" fill-rule="nonzero"/>'
                % (s["path"], COLORS[s["index"] % len(COLORS)])
                for s in r["strokes"] if s["path"])
            legend = " ".join(
                '<span style="color:%s">%s%.2f%s</span>' % (
                    COLORS[s["index"] % len(COLORS)], s["type"], s["retain"],
                    "×" if s["failed"] else "")
                for s in r["strokes"])
            inner = ('<svg viewBox="-80 -220 1180 1180" width="150" height="150">'
                     '<g transform="scale(1,-1) translate(0,-760)">%s</g></svg>'
                     '<div style="font-size:10px">%s</div>') % (paths, legend)
        badge = {"verified": "✅", "known_bad": "❌", "unverified": "❓"}[status]
        cells.append(
            '<div style="display:inline-block;border:1px solid #ccc;margin:2px;'
            'padding:2px;width:160px;vertical-align:top">'
            '<div style="font-size:11px">%s %s <b>%s</b></div>%s</div>'
            % (badge, f.replace("-Regular", "").replace(".ttf", ""), ch, inner))
    html = ('<!DOCTYPE html><meta charset="utf-8"><title>strokelab bench</title>'
            '<p>✅ verified（回退即报警）　❌ known_bad（已知错误）　'
            '❓ unverified（待目检；确认后把 golden json 的 status 改为 '
            'verified/known_bad）</p>' + "".join(cells))
    out = os.path.join(REPO_DIR, "bench", "review.html")
    open(out, "w", encoding="utf-8").write(html)
    print("目检页:", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--html", action="store_true")
    a = ap.parse_args()
    if a.seed:
        cmdSeed(a.root)
    elif a.check:
        raise SystemExit(1 if cmdCheck(a.root) else 0)
    elif a.html:
        writeHtml(a.root, runSuite(a.root))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
