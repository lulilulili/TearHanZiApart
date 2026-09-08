# -*- coding: utf-8 -*-
"""
prepareData.py — 汉字矢量笔画拆解实验的数据预处理

产出 strokeData.js，供 charStrokeLab.html 加载：
  1. makemeahanzi（文鼎楷体）逐字的笔画轮廓 + 中轴线 + 部件归属(matches) + 结构树
  2. hanzi_chaizi 的拆字信息
  3. 标准笔画库 A（文鼎楷体，来源=makemeahanzi 某字第几笔 / 单笔画整字）
  4. 标准笔画库 B（每个目标字体，来源=U+31C0..31EF 笔画区字形，或从孤立笔画字符中拆出）
  5. 目标字体逐字的真实轮廓（统一归一化到 makemeahanzi 的 1024 坐标系，y 向上，基线 y=0）

坐标约定：全部数据处于 makemeahanzi 空间 —— viewBox 0..1024，y 向上，
渲染时外层加 transform="scale(1,-1) translate(0,-900)"。
"""

import json
import math
import os
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen

BaseDir = os.path.dirname(os.path.abspath(__file__))
RootDir = os.path.dirname(BaseDir)
FontsDir = os.path.join(RootDir, "Fonts")
GraphicsPath = os.path.join(RootDir, "makemeahanzi-master", "graphics.txt")
DictionaryPath = os.path.join(RootDir, "makemeahanzi-master", "dictionary.txt")
ChaiziJtPath = os.path.join(RootDir, "hanzi_chaizi-master", "raw_data", "chaizi-jt.txt")
ChaiziFtPath = os.path.join(RootDir, "hanzi_chaizi-master", "raw_data", "chaizi-ft.txt")
OutputPath = os.path.join(BaseDir, "strokeData.js")

# 演示字符（可自行增删后重跑脚本）
DemoChars = list("十口头木中大天日小水永汉字国你好我")
# 探针字符：用于从目标字体中提取"孤立笔画"，建立标准笔画库 B
ProbeChars = list("一丨丶丿乙亅二三十八人入头小六心卜了又才川不米")

# U+31C0..31EF CJK 笔画区名称表
CjkStrokeNames = {
    0x31C0: "提", 0x31C1: "横折折", 0x31C2: "竖提", 0x31C3: "横提",
    0x31C4: "撇折", 0x31C5: "竖折", 0x31C6: "横折提", 0x31C7: "撇点",
    0x31C8: "横折折撇", 0x31C9: "竖折折钩", 0x31CA: "横撇弯钩", 0x31CB: "竖折折",
    0x31CC: "横折弯", 0x31CD: "横撇", 0x31CE: "捺", 0x31CF: "斜钩",
    0x31D0: "横", 0x31D1: "竖", 0x31D2: "撇", 0x31D3: "点",
    0x31D4: "捺", 0x31D5: "横折", 0x31D6: "横折钩", 0x31D7: "竖钩",
    0x31D8: "竖弯", 0x31D9: "竖弯钩", 0x31DA: "斜钩", 0x31DB: "横折弯钩",
    0x31DC: "横折折折钩", 0x31DD: "弯钩", 0x31DE: "竖折", 0x31DF: "横折折折",
    0x31E0: "横折提", 0x31E1: "横折折撇", 0x31E2: "竖折撇", 0x31E3: "横斜钩",
    0x31E4: "横折弯", 0x31E5: "横撇弯钩", 0x31E6: "竖提", 0x31E7: "横折折折",
    0x31E8: "横斜钩", 0x31E9: "扁斜钩", 0x31EA: "竖折折", 0x31EB: "横折折折钩",
    0x31EC: "横捺", 0x31ED: "点提", 0x31EE: "撇钩", 0x31EF: "折刀头",
}


# ---------------------------------------------------------------- 几何工具

def SplitQuadImplied(points):
    """qCurveTo 的多个 off-curve 点 → 一串 (ctrl, on) 二次段（TrueType 隐含 on 点）。"""
    result = []
    n = len(points)
    for i in range(n - 2):
        ctrl = points[i]
        nxt = points[i + 1]
        implied = ((ctrl[0] + nxt[0]) / 2.0, (ctrl[1] + nxt[1]) / 2.0)
        result.append((ctrl, implied))
    if n >= 2:
        result.append((points[-2], points[-1]))
    else:
        result.append((points[0], points[0]))
    return result


def QuadToCubic(p0, ctrl, p1):
    c1 = (p0[0] + 2.0 / 3.0 * (ctrl[0] - p0[0]), p0[1] + 2.0 / 3.0 * (ctrl[1] - p0[1]))
    c2 = (p1[0] + 2.0 / 3.0 * (ctrl[0] - p1[0]), p1[1] + 2.0 / 3.0 * (ctrl[1] - p1[1]))
    return c1, c2


def Fmt(v):
    r = round(v, 1)
    if abs(r - round(r)) < 0.05:
        return str(int(round(r)))
    return ("%.1f" % r)


def GlyphToContours(font, glyphName, scale, dx):
    """字形 → 轮廓列表，每条轮廓是 SVG 路径串（仅 M/L/C/Z，已归一化到 mmh 空间）。"""
    glyphSet = font.getGlyphSet()
    pen = RecordingPen()
    glyphSet[glyphName].draw(pen)

    def Tx(pt):
        return (pt[0] * scale + dx, pt[1] * scale)

    contours = []
    cur = []
    start = None
    prev = None
    for op, args in pen.value:
        if op == "moveTo":
            if cur:
                contours.append(cur)
            start = Tx(args[0])
            prev = start
            cur = ["M %s %s" % (Fmt(start[0]), Fmt(start[1]))]
        elif op == "lineTo":
            p = Tx(args[0])
            cur.append("L %s %s" % (Fmt(p[0]), Fmt(p[1])))
            prev = p
        elif op == "curveTo":
            pts = [Tx(a) for a in args]
            for i in range(0, len(pts), 3):
                c1, c2, p1 = pts[i], pts[i + 1], pts[i + 2]
                cur.append("C %s %s %s %s %s %s" % (
                    Fmt(c1[0]), Fmt(c1[1]), Fmt(c2[0]), Fmt(c2[1]), Fmt(p1[0]), Fmt(p1[1])))
                prev = p1
        elif op == "qCurveTo":
            if args[-1] is None:
                # TrueType 全 off-curve 轮廓：起点为首末控制点中点
                pts = [Tx(a) for a in args[:-1]]
                first = pts[0]
                last = pts[-1]
                impliedStart = ((first[0] + last[0]) / 2.0, (first[1] + last[1]) / 2.0)
                if not cur:
                    cur = ["M %s %s" % (Fmt(impliedStart[0]), Fmt(impliedStart[1]))]
                    start = impliedStart
                prev = impliedStart
                pts = pts + [impliedStart]
            else:
                pts = [Tx(a) for a in args]
            segs = SplitQuadImplied([prev] + pts) if len(pts) > 2 else [(pts[0], pts[1])] if len(pts) == 2 else []
            if len(pts) == 1:
                segs = [(prev, pts[0])]
            for ctrl, on in segs:
                c1, c2 = QuadToCubic(prev, ctrl, on)
                cur.append("C %s %s %s %s %s %s" % (
                    Fmt(c1[0]), Fmt(c1[1]), Fmt(c2[0]), Fmt(c2[1]), Fmt(on[0]), Fmt(on[1])))
                prev = on
        elif op == "closePath":
            cur.append("Z")
            contours.append(cur)
            cur = []
            prev = start
    if cur:
        cur.append("Z")
        contours.append(cur)
    return [" ".join(c) for c in contours]


def FlattenContour(pathStr, step=14.0):
    """SVG 路径串（M/L/C/Z）→ 折线采样点列表，用于几何判定。"""
    tokens = pathStr.replace(",", " ").split()
    pts = []
    i = 0
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    while i < len(tokens):
        t = tokens[i]
        if t == "M":
            cur = (float(tokens[i + 1]), float(tokens[i + 2]))
            start = cur
            pts.append(cur)
            i += 3
        elif t == "L":
            p = (float(tokens[i + 1]), float(tokens[i + 2]))
            d = math.hypot(p[0] - cur[0], p[1] - cur[1])
            n = max(1, int(d / step))
            for k in range(1, n + 1):
                u = k / float(n)
                pts.append((cur[0] + (p[0] - cur[0]) * u, cur[1] + (p[1] - cur[1]) * u))
            cur = p
            i += 3
        elif t == "Q":
            c = (float(tokens[i + 1]), float(tokens[i + 2]))
            p = (float(tokens[i + 3]), float(tokens[i + 4]))
            n = max(2, int(math.hypot(p[0] - cur[0], p[1] - cur[1]) / step) + 1)
            for k in range(1, n + 1):
                u = k / float(n)
                a = 1 - u
                pts.append((a * a * cur[0] + 2 * a * u * c[0] + u * u * p[0],
                            a * a * cur[1] + 2 * a * u * c[1] + u * u * p[1]))
            cur = p
            i += 5
        elif t == "C":
            c1 = (float(tokens[i + 1]), float(tokens[i + 2]))
            c2 = (float(tokens[i + 3]), float(tokens[i + 4]))
            p = (float(tokens[i + 5]), float(tokens[i + 6]))
            n = max(2, int(math.hypot(p[0] - cur[0], p[1] - cur[1]) / step) + 1)
            for k in range(1, n + 1):
                u = k / float(n)
                a = 1 - u
                pts.append((a ** 3 * cur[0] + 3 * a * a * u * c1[0] + 3 * a * u * u * c2[0] + u ** 3 * p[0],
                            a ** 3 * cur[1] + 3 * a * a * u * c1[1] + 3 * a * u * u * c2[1] + u ** 3 * p[1]))
            cur = p
            i += 7
        elif t == "Z":
            pts.append(start)
            cur = start
            i += 1
        else:
            i += 1
    return pts


def SignedArea(pts):
    s = 0.0
    for i in range(len(pts) - 1):
        s += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return s / 2.0


def PointInPolygon(pt, poly):
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xInt = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xInt:
                inside = not inside
        j = i
    return inside


def GroupContours(contourStrs):
    """轮廓 → 连通组（外轮廓 + 其孔洞）。返回每条轮廓的组号列表。"""
    polys = [FlattenContour(c) for c in contourStrs]
    n = len(polys)
    depth = [0] * n
    parent = [-1] * n
    for i in range(n):
        if not polys[i]:
            continue
        probe = polys[i][0]
        for j in range(n):
            if i == j or not polys[j]:
                continue
            if PointInPolygon(probe, polys[j]):
                depth[i] += 1
    order = sorted(range(n), key=lambda k: depth[k])
    groupOf = [-1] * n
    groups = []
    for i in order:
        if depth[i] % 2 == 0:
            groupOf[i] = len(groups)
            groups.append(i)
        else:
            best = -1
            bestDepth = -1
            probe = polys[i][0] if polys[i] else (0, 0)
            for j in range(n):
                if depth[j] % 2 == 0 and polys[j] and PointInPolygon(probe, polys[j]):
                    if depth[j] > bestDepth:
                        bestDepth = depth[j]
                        best = j
            groupOf[i] = groupOf[best] if best >= 0 else 0
    return groupOf


def PolylineDistance(pt, poly):
    best = 1e18
    x, y = pt
    for i in range(len(poly) - 1):
        x1, y1 = poly[i]
        x2, y2 = poly[i + 1]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        if L2 < 1e-9:
            d = math.hypot(x - x1, y - y1)
        else:
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
            d = math.hypot(x - x1 - t * dx, y - y1 - t * dy)
        if d < best:
            best = d
    return best


# ---------------------------------------------------------------- 笔画分类（基于中轴线）

def ResamplePolyline(pts, step=15.0):
    if len(pts) < 2:
        return list(pts)
    out = [tuple(pts[0])]
    carry = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1e-9:
            continue
        t = step - carry
        while t <= L:
            u = t / L
            out.append((x1 + (x2 - x1) * u, y1 + (y2 - y1) * u))
            t += step
        carry = L - (t - step)
    if out[-1] != tuple(pts[-1]):
        out.append(tuple(pts[-1]))
    return out


def NetAngle(sec):
    return math.degrees(math.atan2(sec[-1][1] - sec[0][1], sec[-1][0] - sec[0][0]))


def SectionLen(sec):
    return sum(math.hypot(sec[i + 1][0] - sec[i][0], sec[i + 1][1] - sec[i][1])
               for i in range(len(sec) - 1))


def ElemOfAngle(ang):
    if -20 <= ang <= 38:
        return "横"
    if 38 < ang <= 80:
        return "提"
    if -115 <= ang < -65:
        return "竖"
    if -65 <= ang < -20:
        return "捺"
    if ang < -115 or ang > 155:
        return "撇"
    return "提"


def ClassifyMedian(median):
    """中轴线折线 → 笔画名（横竖撇捺点提 + 折/钩组合）。y 向上坐标系。

    步骤：重采样 → 按拐角(转向>42°)分段 → 丢弃楷体起笔小段 → 末端判钩 →
    每段按净方向分类 → 依"折"规则组合命名。
    """
    pts = ResamplePolyline([tuple(p) for p in median], 15.0)
    total = SectionLen(pts)
    if total < 1e-6 or len(pts) < 2:
        return "点"

    # 拐角检测：窗口化切向差（对圆角过渡鲁棒），非极大值抑制
    angles = []
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        angles.append(math.degrees(math.atan2(dy, dx)))

    def AngDiff(a, b):
        d = a - b
        while d > 180:
            d -= 360
        while d < -180:
            d += 360
        return d

    w = 2
    turns = []
    for i in range(len(angles)):
        a = angles[max(0, i - w)]
        b = angles[min(len(angles) - 1, i + w)]
        turns.append(abs(AngDiff(b, a)))
    corners = []
    i = 1
    while i < len(angles) - 1:
        if turns[i] > 48 and turns[i] >= turns[i - 1] and turns[i] >= turns[i + 1]:
            if not corners or i - corners[-1] > w:
                corners.append(i)
                i += w
        i += 1

    sections = []
    last = 0
    for c in corners:
        if c - last >= 1:
            sections.append(pts[last:c + 1])
        last = c
    sections.append(pts[last:])
    sections = [s for s in sections if len(s) >= 2]

    # 合并过短的中间段
    def MergeTiny(secs):
        out = []
        for s in secs:
            if out and SectionLen(s) < max(22.0, total * 0.06):
                out[-1] = out[-1] + s[1:]
            else:
                out.append(s)
        return out

    sections = MergeTiny(sections)

    # 楷体起笔顿笔：首段很短且后面还有段 → 并入判定时忽略
    if len(sections) >= 2 and SectionLen(sections[0]) < max(55.0, total * 0.13):
        sections = sections[1:]

    # 钩：末段短且方向向上/左上/左
    hook = False
    if len(sections) >= 2:
        lastLen = SectionLen(sections[-1])
        if lastLen < max(70.0, total * 0.24):
            ang = NetAngle(sections[-1])
            if ang > 95 or ang < -155 \
               or (70 < ang <= 95 and lastLen < max(80.0, total * 0.15)) \
               or (60 < ang <= 70 and lastLen < 60):
                hook = True
                sections = sections[:-1]

    elems = []
    for s in sections:
        e = ElemOfAngle(NetAngle(s))
        if not elems or elems[-1] != e:
            elems.append(e)

    if not elems:
        return "点"
    if len(elems) == 1:
        name = elems[0]
        secLen = total
        if name in ("捺", "竖", "撇", "提") and secLen < 160:
            name = "点"
        elif name == "横" and secLen < 80:
            name = "点"
    else:
        parts = [elems[0]]
        for e in elems[1:]:
            parts.append("折" if e in ("横", "竖") else e)
        name = "".join(parts)
    if hook:
        name += "钩"
    alias = {
        "竖折钩": "竖弯钩", "撇提": "撇折", "捺钩": "斜钩", "横折捺": "横斜钩",
        "横折折钩": "横折弯钩", "点钩": "弯钩", "提钩": "弯钩",
        "横撇折钩": "横折弯钩",
    }
    return alias.get(name, name)


# 单笔画整字的权威笔画类型（几何分类对整字大尺寸笔画会失真，直接指定）
SingleStrokeCharTypes = {
    "一": "横", "丨": "竖", "丶": "点", "丿": "撇", "亅": "竖钩", "乙": "横折弯钩",
}


def TypeOfStroke(ch, strokeIndex, medians):
    if len(medians) == 1 and ch in SingleStrokeCharTypes:
        return SingleStrokeCharTypes[ch]
    return ClassifyMedian(medians[strokeIndex])


# ---------------------------------------------------------------- 数据加载

def LoadMakeMeAHanzi(neededChars):
    graphics = {}
    with open(GraphicsPath, "r", encoding="utf-8") as f:
        for line in f:
            ch = line[14]  # {"character":"X"...
            if ch in neededChars:
                graphics[ch] = json.loads(line)
    dictionary = {}
    with open(DictionaryPath, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            dictionary[entry["character"]] = entry
    return graphics, dictionary


def LoadChaizi(path):
    table = {}
    if not os.path.exists(path):
        return table
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 2:
                table[cols[0]] = [c.split() for c in cols[1:]]
    return table


def BuildStructureTree(ch, dictionary, depth=0, maxDepth=4, seen=None):
    """递归展开 makemeahanzi 的 IDS 分解，生成结构树。"""
    if seen is None:
        seen = set()
    node = {"char": ch}
    if depth >= maxDepth or ch in seen:
        return node
    entry = dictionary.get(ch)
    if not entry:
        return node
    seen = seen | {ch}
    ids = entry.get("decomposition") or ""
    if not ids or ids[0] not in "⿰⿱⿲⿳⿴⿵⿶⿷⿸⿹⿺⿻":
        return node
    # 解析 IDS 串（前缀表达式）
    pos = [0]

    def Parse():
        c = ids[pos[0]]
        pos[0] += 1
        if c in "⿰⿱⿴⿵⿶⿷⿸⿹⿺⿻":
            return {"op": c, "children": [Parse(), Parse()]}
        if c in "⿲⿳":
            return {"op": c, "children": [Parse(), Parse(), Parse()]}
        return {"leaf": c}

    try:
        tree = Parse()
    except IndexError:
        return node

    def Attach(t, d):
        if "leaf" in t:
            c = t["leaf"]
            if c == "？":
                return {"char": "？"}
            return BuildStructureTree(c, dictionary, d, maxDepth, seen)
        return {"op": t["op"],
                "children": [Attach(x, d + 1) for x in t["children"]]}

    sub = Attach(tree, depth + 1)
    node.update(sub if "op" in sub else {})
    return node


# ---------------------------------------------------------------- 标准笔画库

def BuildLibraryA(graphics, dictionary):
    """文鼎楷体标准笔画库：单笔画整字 + 演示字里的笔画兜底。"""
    lib = {}  # type -> entry
    singleStrokeChars = "一丨丶丿乙亅"
    for ch in singleStrokeChars:
        g = graphics.get(ch)
        if not g or len(g["strokes"]) != 1:
            continue
        t = TypeOfStroke(ch, 0, g["medians"])
        lib.setdefault(t, []).append({
            "type": t, "path": g["strokes"][0], "median": g["medians"][0],
            "source": "整字「%s」(U+%04X)" % (ch, ord(ch)), "kind": "wholeChar",
        })
    for ch, g in graphics.items():
        for idx, (s, m) in enumerate(zip(g["strokes"], g["medians"])):
            t = TypeOfStroke(ch, idx, g["medians"])
            if t not in lib:
                lib[t] = [{
                    "type": t, "path": s, "median": m,
                    "source": "「%s」第%d笔" % (ch, idx + 1), "kind": "charStroke",
                }]
    return lib


def BuildLibraryB(fontPath, graphics):
    """目标字体标准笔画库：优先 U+31C0..31EF，其次从探针字的孤立笔画拆取。"""
    font = TTFont(fontPath, fontNumber=0, lazy=True)
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    scale = 1024.0 / upm
    hmtx = font["hmtx"]
    lib = {}
    entries = []

    def CenterDx(glyphName):
        aw = hmtx[glyphName][0] * scale
        return (1024.0 - aw) / 2.0

    # 来源 1：Unicode 笔画区
    for cp in range(0x31C0, 0x31F0):
        if cp not in cmap:
            continue
        gname = cmap[cp]
        try:
            contours = GlyphToContours(font, gname, scale, CenterDx(gname))
        except Exception:
            continue
        if not contours:
            continue
        t = CjkStrokeNames.get(cp, "?")
        entry = {"type": t, "contours": contours,
                 "source": "U+%04X %s" % (cp, chr(cp)), "kind": "unicode"}
        entries.append(entry)
        if t not in lib:
            lib[t] = entry

    # 来源 2：探针字的孤立笔画
    for ch in ProbeChars:
        g = graphics.get(ch)
        if not g or ord(ch) not in cmap:
            continue
        gname = cmap[ord(ch)]
        try:
            contours = GlyphToContours(font, gname, scale, CenterDx(gname))
        except Exception:
            continue
        if not contours:
            continue
        medians = g["medians"]
        groupOf = GroupContours(contours)
        nGroups = max(groupOf) + 1 if groupOf else 0
        # 组 → 采样点 → 最近中轴线归属
        groupSamples = [[] for _ in range(nGroups)]
        for ci, c in enumerate(contours):
            groupSamples[groupOf[ci]].extend(FlattenContour(c, 18.0))
        groupToStroke = [-1] * nGroups
        strokeHitGroups = [set() for _ in medians]
        for gi, samples in enumerate(groupSamples):
            if not samples:
                continue
            votes = {}
            for p in samples:
                best, bestD = -1, 1e18
                for k, m in enumerate(medians):
                    d = PolylineDistance(p, m)
                    if d < bestD:
                        bestD, best = d, k
                votes[best] = votes.get(best, 0) + 1
            top, topCount = max(votes.items(), key=lambda kv: kv[1])
            if topCount >= len(samples) * 0.95:
                groupToStroke[gi] = top
                strokeHitGroups[top].add(gi)
        for gi in range(nGroups):
            k = groupToStroke[gi]
            if k < 0 or len(strokeHitGroups[k]) != 1:
                continue  # 非孤立
            t = TypeOfStroke(ch, k, medians)
            groupContours = [c for ci, c in enumerate(contours) if groupOf[ci] == gi]
            entry = {"type": t, "contours": groupContours,
                     "source": "从「%s」第%d笔拆出" % (ch, k + 1), "kind": "extracted",
                     "refMedian": medians[k]}
            entries.append(entry)
            if t not in lib:
                lib[t] = entry
    font.close()
    return lib, entries


# ---------------------------------------------------------------- 主流程

def DeepenMatches(ch, dictionary, graphics, depth=0):
    """makemeahanzi 的 matches 只标注第一层部件；递归用部件自身的
    matches 细化出多层级的笔画→部件索引路径。"""
    entry = dictionary.get(ch)
    g = graphics.get(ch)
    if not entry or not g:
        return None
    matches = entry.get("matches")
    if not matches:
        return None
    result = [list(p) if p else None for p in matches]
    if depth >= 3:
        return result
    tree = BuildStructureTree(ch, dictionary, maxDepth=2)
    children = tree.get("children") or []
    byComp = {}
    for si, p in enumerate(matches):
        if p:
            byComp.setdefault(p[0], []).append(si)
    for ci, strokeIdxs in byComp.items():
        if ci >= len(children):
            continue
        compChar = children[ci].get("char")
        if not compChar or compChar == "？":
            continue
        sub = DeepenMatches(compChar, dictionary, graphics, depth + 1)
        if sub is None or len(sub) != len(strokeIdxs):
            continue
        for j, si in enumerate(strokeIdxs):
            if sub[j]:
                result[si] = [ci] + sub[j]
    return result


def Main():
    allChars = set(DemoChars) | set(ProbeChars)
    print("加载 makemeahanzi ...")
    # 结构树需要部件字形，先全量加载 dictionary，graphics 按需扩充
    graphicsAll, dictionary = LoadMakeMeAHanzi(set())
    # 收集结构树引用到的部件字符
    needed = set(allChars)
    frontier = set(allChars)
    for _ in range(4):
        nxt = set()
        for ch in frontier:
            e = dictionary.get(ch)
            if not e:
                continue
            for c in (e.get("decomposition") or ""):
                if c not in "⿰⿱⿲⿳⿴⿵⿶⿷⿸⿹⿺⿻？" and c not in needed:
                    nxt.add(c)
        needed |= nxt
        frontier = nxt
    graphics, _ = LoadMakeMeAHanzi(needed)
    print("  graphics 条目: %d" % len(graphics))

    chaiziJt = LoadChaizi(ChaiziJtPath)
    chaiziFt = LoadChaizi(ChaiziFtPath)

    print("构建标准笔画库 A（文鼎楷体）...")
    libraryA = BuildLibraryA(graphics, dictionary)
    print("  A 库笔画类型数: %d" % len(libraryA))

    fonts = {}
    for fname in sorted(os.listdir(FontsDir)):
        if not fname.lower().endswith((".ttf", ".otf")):
            continue
        fontPath = os.path.join(FontsDir, fname)
        fontId = os.path.splitext(fname)[0]
        print("处理字体 %s ..." % fname)
        try:
            libB, entriesB = BuildLibraryB(fontPath, graphics)
        except Exception as ex:
            print("  ! 建库失败: %s" % ex)
            libB, entriesB = {}, []
        font = TTFont(fontPath, fontNumber=0, lazy=True)
        cmap = font.getBestCmap()
        upm = font["head"].unitsPerEm
        scale = 1024.0 / upm
        hmtx = font["hmtx"]
        glyphs = {}
        for ch in sorted(allChars):
            if ord(ch) not in cmap:
                continue
            gname = cmap[ord(ch)]
            try:
                aw = hmtx[gname][0] * scale
                contours = GlyphToContours(font, gname, scale, (1024.0 - aw) / 2.0)
            except Exception:
                continue
            if contours:
                glyphs[ch] = {"contours": contours}
        font.close()
        fonts[fontId] = {
            "file": fname, "unitsPerEm": upm,
            "libraryB": libB, "libraryBAll": entriesB,
            "glyphs": glyphs,
        }
        print("  字形 %d 个, B 库类型 %d, B 库条目 %d" % (len(glyphs), len(libB), len(entriesB)))

    print("整理每字数据 ...")
    charData = {}
    for ch in sorted(allChars):
        g = graphics.get(ch)
        if not g:
            continue
        entry = dictionary.get(ch, {})
        strokeTypes = [TypeOfStroke(ch, i, g["medians"]) for i in range(len(g["medians"]))]
        deepMatches = DeepenMatches(ch, dictionary, graphics) or entry.get("matches", [])
        charData[ch] = {
            "strokes": g["strokes"],
            "medians": g["medians"],
            "strokeTypes": strokeTypes,
            "radical": entry.get("radical", ""),
            "decomposition": entry.get("decomposition", ""),
            "matches": deepMatches,
            "structure": BuildStructureTree(ch, dictionary),
            "chaiziJt": chaiziJt.get(ch, []),
            "chaiziFt": chaiziFt.get(ch, []),
        }

    payload = {
        "demoChars": [c for c in DemoChars if c in charData],
        "probeChars": [c for c in ProbeChars if c in charData],
        "chars": charData,
        "libraryA": libraryA,
        "fonts": fonts,
    }
    with open(OutputPath, "w", encoding="utf-8") as f:
        f.write("// 由 prepareData.py 自动生成，勿手改\n")
        f.write("window.STROKE_DATA = ")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    size = os.path.getsize(OutputPath)
    print("完成: %s (%.1f MB)" % (OutputPath, size / 1e6))


if __name__ == "__main__":
    Main()
