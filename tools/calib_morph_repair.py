# -*- coding: utf-8 -*-
"""形态修复器·交界毛刺置换 标定驱动（标定用，可删）。

用法:
  python -X utf8 tools/calib_morph_repair.py pick20    # simsun cross 失败字含 bridges≥2 抽 20
  python -X utf8 tools/calib_morph_repair.py cases     # 病例集 off/on 对照 + SVG 归档
  python -X utf8 tools/calib_morph_repair.py health    # SC 通过抽样100(seed 7) 零翻转门
  python -X utf8 tools/calib_morph_repair.py 任意字串 [字体]   # 定点 off/on 对照
选项:
  --no-svg  跳过 SVG 归档

病例集 = simsun 永(横折钩毛边) + simsun/simhei/simkai 草 + simsun 威我鬼
       + pick20（缓存 verifyOut/morphRepair/pick20.txt）。
达标线（任务裁定）：病例集 morphJunc/morphSpur 显著下降且目检毛边消失
≥60%；健康集 SC 通过抽样100（seed 7）零 fails 翻转、零 retain 下降>0.02。
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from strokelab.datahub import DataHub            # noqa: E402
from strokelab.fonthub import FontEntry          # noqa: E402
import strokelab.boolean as bl                   # noqa: E402
from strokelab.pipeline import runPipeline       # noqa: E402
from strokelab.verify import verifyChar          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "verifyOut", "morphRepair")
# 基线口径随主线走：d0eb244 横捺撇门禁归档（sample 933/957、cross
# 287/247/276）。2b-* 归档是横捺撇五连提交前的旧世界，切割/映射全变，
# 健康池与 cross 失败池按当前 HEAD 取才有意义。
CROSS_SIMSUN = os.path.join(ROOT, "verifyOut", "runs", "hnp-gate-cross",
                            "simsun.jsonl")
SAMPLE_SC = os.path.join(ROOT, "verifyOut", "runs", "hnp-gate-sample",
                         "HarmonyOS_Sans_SC.jsonl")
PICK20 = os.path.join(OUT_DIR, "pick20.txt")

NAMED_CASES = [("simsun.ttc", "永草威我鬼"),
               ("simhei.ttf", "草"),
               ("simkai.ttf", "草")]

PALETTE = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
           "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff",
           "#9a6324", "#fffac8", "#800000", "#aaffc3", "#808000", "#ffd8b1",
           "#000075", "#808080"]

_FONTS = {}


def loadFont(name):
    if name not in _FONTS:
        hub = DataHub(ROOT)
        fe = FontEntry(os.path.join(ROOT, "Fonts", name))
        fe.buildLibraryB(hub)
        fe.completeLibraryB(hub)
        _FONTS[name] = (hub, fe)
    return _FONTS[name]


def morphM(v):
    m = v.get("m", {})
    return (m.get("morphJunc", 0), m.get("morphSpur", 0), m.get("morphBad", 0))


def codesOf(v):
    return "+".join(sorted({f["code"] for f in v["fails"]})) or "PASS"


# ---------------------------------------------------------------- SVG 归档

def _svgPanel(result, dx, hot):
    """一侧面板：逐笔填色（fill-rule=evenodd 与 verify 消费语义一致），
    hot 笔（置换双方）描红边。画法照 verifyOut/yong_cross_debug.svg。"""
    parts = ['<g transform="translate(%d,0)"><g transform="scale(1,-1) '
             'translate(0,-900)">' % dx]
    for s in result["strokes"]:
        if s.get("failed") or not s.get("path"):
            continue
        stroke = ('stroke="#d00" stroke-width="4"' if s["index"] in hot
                  else 'stroke="#333" stroke-width="1"')
        parts.append('<path d="%s" fill="%s" fill-opacity="0.75" '
                     'fill-rule="evenodd" %s/>' %
                     (s["path"], PALETTE[s["index"] % len(PALETTE)], stroke))
    parts.append("</g></g>")
    return "".join(parts)


def writeCompareSvg(fontName, ch, r0, r1, note):
    """off|on 双面板对照 SVG → verifyOut/morphRepair/。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    hot = set()
    for k, _n, j in (r1.get("morphRepairs") or []):
        hot.add(k)
        hot.add(j)
    body = (_svgPanel(r0, 0, set()) + _svgPanel(r1, 1250, hot) +
            '<text x="20" y="-120" font-size="60">off</text>'
            '<text x="1270" y="-120" font-size="60">on %s</text>' % note)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" '
           'viewBox="-100 -180 2600 1300" width="1000">'
           '<g transform="translate(0,0)">%s</g></svg>' % body)
    stem = os.path.splitext(fontName)[0]
    path = os.path.join(OUT_DIR, "%s_%s.svg" % (stem, ch))
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path


# ---------------------------------------------------------------- 模式

def comparePair(fontName, ch, svg=True):
    """单字 off/on 四跑对照 → 行记录 dict。"""
    hub, fe = loadFont(fontName)
    bl.MORPH_REPAIR = False
    v0 = verifyChar(hub, fe, ch)
    r0 = runPipeline(hub, fe, ch)
    bl.MORPH_REPAIR = True
    v1 = verifyChar(hub, fe, ch)
    r1 = runPipeline(hub, fe, ch)
    bl.MORPH_REPAIR = False
    row = {"font": fontName, "ch": ch,
           "c0": codesOf(v0), "c1": codesOf(v1),
           "m0": morphM(v0), "m1": morphM(v1),
           "retain0": v0["m"].get("retain"), "retain1": v1["m"].get("retain"),
           "repairs": r1.get("morphRepairs") or []}
    if svg:
        row["svg"] = writeCompareSvg(fontName, ch, r0, r1,
                                     "repairs=%s" % row["repairs"])
    return row


def runCases(svg=True):
    cases = list(NAMED_CASES)
    if os.path.exists(PICK20):
        picked = open(PICK20, encoding="utf-8").read().strip()
        if picked:
            cases.append(("simsun.ttc", picked))
    rows = []
    for fontName, chars in cases:
        for ch in chars:
            try:
                row = comparePair(fontName, ch, svg)
            except Exception as e:
                print("%s %s ERROR %r" % (fontName, ch, e), flush=True)
                continue
            rows.append(row)
            flag = "=" if row["c0"] == row["c1"] else (
                "FIX" if row["c1"] == "PASS" else (
                    "BREAK" if row["c0"] == "PASS" else "MIG"))
            print("%s %s  %-14s->%-14s %-5s junc %d->%d spur %d->%d "
                  "bad %d->%d 置换%s" % (
                      row["font"][:6], ch, row["c0"], row["c1"], flag,
                      row["m0"][0], row["m1"][0], row["m0"][1], row["m1"][1],
                      row["m0"][2], row["m1"][2], row["repairs"]), flush=True)
    print()
    j0 = sum(r["m0"][0] for r in rows)
    j1 = sum(r["m1"][0] for r in rows)
    s0 = sum(r["m0"][1] for r in rows)
    s1 = sum(r["m1"][1] for r in rows)
    b0 = sum(r["m0"][2] for r in rows)
    b1 = sum(r["m1"][2] for r in rows)
    fired = sum(1 for r in rows if r["repairs"])
    better = sum(1 for r in rows
                 if sum(r["m1"][:2]) < sum(r["m0"][:2]))
    worse = sum(1 for r in rows if sum(r["m1"][:2]) > sum(r["m0"][:2]))
    broke = [r["ch"] for r in rows
             if r["c0"] == "PASS" and r["c1"] != "PASS"]
    fixed = [r["ch"] for r in rows
             if r["c1"] == "PASS" and r["c0"] != "PASS"]
    print("病例 %d：置换触发 %d；morphJunc %d->%d  morphSpur %d->%d  "
          "morphBad %d->%d" % (len(rows), fired, j0, j1, s0, s1, b0, b1))
    print("junc+spur 改善 %d / 恶化 %d；verify FIX %s BREAK %s"
          % (better, worse, "".join(fixed) or "-", "".join(broke) or "-"))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "cases.json"), "w",
              encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


def pick20():
    """simsun cross 失败字含 bridges≥2 的笔者，按基线文件序取前 20。"""
    hub, fe = loadFont("simsun.ttc")
    bl.MORPH_REPAIR = False
    named = set("永草威我鬼")
    out = []
    for line in open(CROSS_SIMSUN, encoding="utf-8"):
        d = json.loads(line)
        if d.get("skip") or not d.get("fails") or d["ch"] in named:
            continue
        ch = d["ch"]
        try:
            r = runPipeline(hub, fe, ch)
        except Exception:
            continue
        if "error" in r:
            continue
        if any((s.get("bridges") or 0) >= 2 for s in r["strokes"]
               if not s["failed"]):
            out.append(ch)
            print("命中 %d: %s" % (len(out), ch), flush=True)
        if len(out) >= 20:
            break
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(PICK20, "w", encoding="utf-8") as f:
        f.write("".join(out))
    print("pick20 ->", PICK20, "".join(out))


def health():
    """SC 通过抽样 100（seed 7，基线 2b-sample-on-final 通过字）off/on
    对照：达标=零 fails 翻转、零 retain 下降>0.02。"""
    chars = []
    for line in open(SAMPLE_SC, encoding="utf-8"):
        d = json.loads(line)
        if not d.get("skip") and not d.get("fails"):
            chars.append(d["ch"])
    random.seed(7)
    sample = random.sample(chars, 100)
    hub, fe = loadFont("HarmonyOS_Sans_SC.ttf")
    flips, drops, fired = [], [], 0
    for i, ch in enumerate(sample):
        bl.MORPH_REPAIR = False
        v0 = verifyChar(hub, fe, ch)
        bl.MORPH_REPAIR = True
        v1 = verifyChar(hub, fe, ch)
        r1 = runPipeline(hub, fe, ch)
        bl.MORPH_REPAIR = False
        if r1.get("morphRepairs"):
            fired += 1
            print("  置换触发 %s: %s" % (ch, r1["morphRepairs"]), flush=True)
        if codesOf(v0) != codesOf(v1):
            flips.append((ch, codesOf(v0), codesOf(v1)))
            print("  翻转 %s: %s -> %s" % (ch, codesOf(v0), codesOf(v1)),
                  flush=True)
        d = (v0["m"].get("retain") or 0) - (v1["m"].get("retain") or 0)
        if d > 0.02:
            drops.append((ch, d))
            print("  retain 下降 %s: %.3f" % (ch, d), flush=True)
        if (i + 1) % 20 == 0:
            print("... %d/100" % (i + 1), flush=True)
    print()
    print("健康集 100：翻转 %d，retain 下降>0.02 %d，置换触发 %d"
          % (len(flips), len(drops), fired))
    print("达标" if not flips and not drops else "不达标")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "cases"
    svg = "--no-svg" not in sys.argv
    args = [a for a in sys.argv[2:] if not a.startswith("-")]
    if which == "cases":
        runCases(svg)
    elif which == "pick20":
        pick20()
    elif which == "health":
        health()
    else:
        for ch in which:
            row = comparePair(args[0] if args else "simsun.ttc", ch, svg)
            print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
