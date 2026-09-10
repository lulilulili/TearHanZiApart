# -*- coding: utf-8 -*-
"""strokelab.geometry — 纯矢量几何核心（与原 JS 版逐函数对应）。

坐标约定：makemeahanzi 空间，0..1024，y 向上，基线 y=0。
段表示：("L"|"C", p0, c1, c2, p1)，直线段的 c1/c2 为三等分点。
轮廓表示：dict {"segs": [seg,...], 可选 poly/area/isHole/group}。
"""

import math

# ---------------------------------------------------------------- 基本量

def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def lineSeg(p0, p1):
    return ("L", p0, lerp(p0, p1, 1 / 3), lerp(p0, p1, 2 / 3), p1)


def cubicSeg(p0, c1, c2, p1):
    return ("C", p0, c1, c2, p1)


def bezPoint(s, t):
    a = lerp(s[1], s[2], t)
    b = lerp(s[2], s[3], t)
    c = lerp(s[3], s[4], t)
    return lerp(lerp(a, b, t), lerp(b, c, t), t)


def bezTangent(s, t):
    a = lerp(s[1], s[2], t)
    b = lerp(s[2], s[3], t)
    c = lerp(s[3], s[4], t)
    d = lerp(a, b, t)
    e = lerp(b, c, t)
    vx, vy = e[0] - d[0], e[1] - d[1]
    L = math.hypot(vx, vy)
    if L < 1e-9:
        vx, vy = s[4][0] - s[1][0], s[4][1] - s[1][1]
        L = math.hypot(vx, vy) or 1.0
    return (vx / L, vy / L)


def bezSplit(s, t):
    """De Casteljau：矢量精确切割。"""
    a = lerp(s[1], s[2], t)
    b = lerp(s[2], s[3], t)
    c = lerp(s[3], s[4], t)
    d = lerp(a, b, t)
    e = lerp(b, c, t)
    m = lerp(d, e, t)
    return cubicSeg(s[1], a, d, m), cubicSeg(m, e, c, s[4])


def bezSlice(s, t0, t1):
    if t0 <= 1e-6 and t1 >= 1 - 1e-6:
        return s
    seg = s
    if t0 > 1e-6:
        seg = bezSplit(seg, t0)[1]
    t1r = (t1 - t0) / (1 - t0)
    if t1r < 1 - 1e-6:
        seg = bezSplit(seg, max(0.0, min(1.0, t1r)))[0]
    return seg


def segLength(s):
    L = 0.0
    prev = s[1]
    for k in range(1, 9):
        p = bezPoint(s, k / 8)
        L += dist(prev, p)
        prev = p
    return L


# ---------------------------------------------------------------- 路径解析/输出

def _fmt(v):
    r = round(v * 10) / 10
    return str(int(r)) if abs(r - round(r)) < 0.05 else ("%.1f" % r)


def parseContours(pathStr):
    """M/L/Q/C/Z → [{"segs": [...]}]，Q 升为 C。"""
    tk = pathStr.replace(",", " ").split()
    contours = []
    cur = None
    pos = (0.0, 0.0)
    start = (0.0, 0.0)
    i = 0

    def num():
        nonlocal i
        v = float(tk[i])
        i += 1
        return v

    while i < len(tk):
        c = tk[i]
        i += 1
        if c == "M":
            if cur and cur["segs"]:
                contours.append(cur)
            pos = (num(), num())
            start = pos
            cur = {"segs": []}
        elif c == "L":
            p = (num(), num())
            cur["segs"].append(lineSeg(pos, p))
            pos = p
        elif c == "Q":
            q = (num(), num())
            p = (num(), num())
            cur["segs"].append(cubicSeg(
                pos,
                (pos[0] + 2 / 3 * (q[0] - pos[0]), pos[1] + 2 / 3 * (q[1] - pos[1])),
                (p[0] + 2 / 3 * (q[0] - p[0]), p[1] + 2 / 3 * (q[1] - p[1])), p))
            pos = p
        elif c == "C":
            c1 = (num(), num())
            c2 = (num(), num())
            p = (num(), num())
            cur["segs"].append(cubicSeg(pos, c1, c2, p))
            pos = p
        elif c == "Z":
            if dist(pos, start) > 0.6:
                cur["segs"].append(lineSeg(pos, start))
            pos = start
            if cur["segs"]:
                contours.append(cur)
            cur = None
    if cur and cur["segs"]:
        contours.append(cur)
    return contours


def contourToPath(segs, close=True):
    if not segs:
        return ""
    out = ["M %s %s" % (_fmt(segs[0][1][0]), _fmt(segs[0][1][1]))]
    for s in segs:
        if s[0] == "L":
            out.append("L %s %s" % (_fmt(s[4][0]), _fmt(s[4][1])))
        else:
            out.append("C %s %s %s %s %s %s" % (
                _fmt(s[2][0]), _fmt(s[2][1]), _fmt(s[3][0]), _fmt(s[3][1]),
                _fmt(s[4][0]), _fmt(s[4][1])))
    if close:
        out.append("Z")
    return " ".join(out)


def flattenSegs(segs, step=12.0):
    pts = []
    for s in segs:
        n = max(1, int(math.ceil(segLength(s) / step)))
        for k in range(n):
            pts.append(bezPoint(s, k / n))
    if segs:
        pts.append(segs[-1][4])
    return pts


# ---------------------------------------------------------------- 折线几何

class BBox:
    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0


def bboxOfPoints(pts):
    x0 = y0 = 1e18
    x1 = y1 = -1e18
    for p in pts:
        if p[0] < x0: x0 = p[0]
        if p[1] < y0: y0 = p[1]
        if p[0] > x1: x1 = p[0]
        if p[1] > y1: y1 = p[1]
    return BBox(x0, y0, x1, y1)


def signedArea(pts):
    s = 0.0
    for i in range(len(pts) - 1):
        s += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return s / 2.0


def pointInPolygon(pt, poly):
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def nearestOnPolyline(pt, poly):
    """→ dict(d, pt, tan, idx)"""
    bd = 1e18
    bp = poly[0]
    bt = (1.0, 0.0)
    bi = 0
    x, y = pt
    for i in range(len(poly) - 1):
        ax, ay = poly[i]
        bx, by = poly[i + 1]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0
        if L2 > 1e-9:
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
        qx, qy = ax + dx * t, ay + dy * t
        d = math.hypot(x - qx, y - qy)
        if d < bd:
            L = math.sqrt(L2) or 1.0
            bd, bp, bt, bi = d, (qx, qy), (dx / L, dy / L), i
    return {"d": bd, "pt": bp, "tan": bt, "idx": bi}


def polylineLength(poly):
    return sum(dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1))


def resamplePolyline(pts, step=15.0):
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
    if dist(out[-1], pts[-1]) > 1e-6:
        out.append(tuple(pts[-1]))
    return out


def analyzeContours(contours):
    """外轮廓/孔洞/连通组标注。
    判孔用绕向法：nonzero 字体外环与孔环绕向相反（以面积最大轮廓的
    绕向为正类）。旧的嵌套深度奇偶法与 nonzero 填充不等价——"孔中
    悬浮实体"（亘的日中横悬在内腔孔洞里）会被大孔整个抠掉。
    嵌套关系仍用于孔洞归属（挂到最深的包含外环）。"""
    for c in contours:
        c["poly"] = flattenSegs(c["segs"], 10)
        c["area"] = signedArea(c["poly"])

    def spread(c):
        n = len(c["poly"])
        return [c["poly"][int(i * n / 8)] for i in range(8)]

    def nested(a, b):
        pts = spread(a)
        cnt = sum(1 for p in pts if pointInPolygon(p, b["poly"]))
        return cnt >= len(pts) * 0.8

    n = len(contours)
    outerSign = 1.0
    if contours:
        outerSign = 1.0 if max(contours, key=lambda c: abs(c["area"]))["area"] >= 0 \
            else -1.0
    depth = [0] * n
    for i in range(n):
        for j in range(n):
            if i != j and nested(contours[i], contours[j]):
                depth[i] += 1
    groupCount = 0
    for i, c in enumerate(contours):
        c["isHole"] = c["area"] * outerSign < 0
        c["group"] = -1
    for c in contours:
        if not c["isHole"]:
            c["group"] = groupCount
            groupCount += 1
    for i, c in enumerate(contours):
        if not c["isHole"]:
            continue
        best, bestDepth = -1, -1
        for j in range(n):
            if not contours[j]["isHole"] and nested(c, contours[j]) and depth[j] > bestDepth:
                bestDepth, best = depth[j], j
        c["group"] = contours[best]["group"] if best >= 0 else 0
    return contours


def straightenIfNearLine(pts, tolRatio=0.06, tolAbs=5.0):
    """近直折线吸直：所有点到首尾弦的偏差 < max(tolAbs, tolRatio×弦长)
    且弧长≈弦长（防折返形误吸）时，替换为沿弦均匀分布的同点数直线。
    楷体顿笔的小弯不该传染给无衬线体的横竖骨架——鸿蒙的标准横竖 D
    骨架就该是直线。"""
    if len(pts) < 3:
        return pts
    ax, ay = pts[0]
    bx, by = pts[-1]
    L = math.hypot(bx - ax, by - ay)
    if L < 1e-6:
        return pts
    ux, uy = (bx - ax) / L, (by - ay) / L
    tol = max(tolAbs, tolRatio * L)
    for p in pts:
        if abs((p[0] - ax) * -uy + (p[1] - ay) * ux) > tol:
            return pts
    if polylineLength(pts) > L * 1.15:
        return pts
    n = len(pts)
    return [(ax + ux * L * i / (n - 1), ay + uy * L * i / (n - 1))
            for i in range(n)]


def straightenSections(pts, cornerDeg=40.0):
    """分段吸直（吸直的推广）：按拐角把折线切段，各段近直则替换为直线，
    拐角点保留；两条长直臂之间的短碎段（拐角区 Voronoi/精调抖动）坍缩
    为两臂直线的交点=尖拐角；端部短残段沿邻臂直线投影延伸吸收。同一
    类型的 D 骨架应当拓扑一致——横折无论何来都该是干净的"7"字两直段
    +尖角，不该带弧弯变"C"样；弯钩碗底、钩尾这类真曲段总长超阈值，
    不满足坍缩条件，保持原形。"""
    if len(pts) < 3:
        return pts
    rs = resamplePolyline([tuple(p) for p in pts], 15)
    if len(rs) < 4:
        return straightenIfNearLine(pts)
    angles = [math.degrees(math.atan2(rs[i + 1][1] - rs[i][1],
                                      rs[i + 1][0] - rs[i][0]))
              for i in range(len(rs) - 1)]

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
    corners = []
    i = 1
    while i < len(angles) - 1:
        if turns[i] > cornerDeg and turns[i] >= turns[i - 1] \
                and turns[i] >= turns[i + 1]:
            if not corners or i - corners[-1] > w:
                corners.append(i)
                i += w
        i += 1
    if not corners:
        return straightenIfNearLine(pts)

    bounds = [0] + corners + [len(rs) - 1]
    secs = [(a, b) for a, b in zip(bounds[:-1], bounds[1:]) if b > a]
    total = polylineLength(rs)
    longTh = max(60.0, 0.15 * total)

    def secPts(s):
        return rs[s[0]:s[1] + 1]

    def isLong(s):
        seg = secPts(s)
        c = dist(seg[0], seg[-1])
        return c >= longTh and polylineLength(seg) <= c * 1.08

    kinds = ["L" if isLong(s) else "s" for s in secs]
    if "L" not in kinds:
        return _legacyStraighten(rs, secs)

    def lineOf(s):
        a, b = rs[s[0]], rs[s[1]]
        L = dist(a, b)
        return a, ((b[0] - a[0]) / L, (b[1] - a[1]) / L)

    def intersect(sA, sB):
        aA, uA = lineOf(sA)
        aB, uB = lineOf(sB)
        den = uA[0] * uB[1] - uA[1] * uB[0]
        if abs(den) < 0.05:
            return None
        wx, wy = aB[0] - aA[0], aB[1] - aA[1]
        t = (wx * uB[1] - wy * uB[0]) / den
        return (aA[0] + uA[0] * t, aA[1] + uA[1] * t)

    out = []

    def emit(seq):
        for p in seq:
            if out and dist(out[-1], p) < 1e-6:
                continue
            out.append((p[0], p[1]))

    n = len(secs)
    i = 0
    while i < n:
        if kinds[i] == "L":
            emit(straightenIfNearLine(secPts(secs[i])))
            i += 1
            continue
        j = i
        runLen = 0.0
        while j < n and kinds[j] == "s":
            runLen += polylineLength(secPts(secs[j]))
            j += 1
        prevL = secs[i - 1] if i > 0 and kinds[i - 1] == "L" else None
        nextL = secs[j] if j < n and kinds[j] == "L" else None
        runPts = rs[secs[i][0]:secs[j - 1][1] + 1]
        gapMid = runPts[len(runPts) // 2]
        if prevL and nextL and runLen <= longTh:
            X = intersect(prevL, nextL)
            if X and dist(X, gapMid) <= runLen * 1.5 + 40:
                emit([X])
            else:
                emit(runPts)
        elif prevL is None and nextL is not None and runLen <= max(45.0, total * 0.08):
            aN, uN = lineOf(nextL)
            e = runPts[0]
            t = (e[0] - aN[0]) * uN[0] + (e[1] - aN[1]) * uN[1]
            if t < -5:
                emit([(aN[0] + uN[0] * t, aN[1] + uN[1] * t)])
        elif nextL is None and prevL is not None and runLen <= max(45.0, total * 0.08):
            aP, uP = lineOf(prevL)
            bP = rs[prevL[1]]
            e = runPts[-1]
            t = (e[0] - bP[0]) * uP[0] + (e[1] - bP[1]) * uP[1]
            if t > 5:
                emit([(bP[0] + uP[0] * t, bP[1] + uP[1] * t)])
        else:
            emit(runPts)
        i = j
    return out if len(out) >= 2 else pts


def _legacyStraighten(rs, secs):
    """无长直臂（全曲/全碎）：逐段近直吸直，拐角保留（原行为）。"""
    out = []
    for k, s in enumerate(secs):
        sec = straightenIfNearLine(rs[s[0]:s[1] + 1])
        if out:
            sec = sec[1:]
        out.extend(sec)
    return out if len(out) >= 2 else rs


def outlineCenterline(loops, step=8.0):
    """从孤立笔画轮廓直接提取中线（Voronoi 中轴的图直径路径）。
    纯矢量确定性：边界按 step 加密采样 → Voronoi 边 → 只留完全在
    墨内的边建图 → 两次 Dijkstra 取最远叶对的路径 = 中线主干（分叉
    自动剪除，钩在直径路径端部天然保留）。B 库骨架由此完全取决于
    目标字体轮廓自身几何——臂长比例、弯直全是字体自己的，楷体中轴线
    不再参与形状（bbox 映射楷体中线在臂比悬殊时会斜穿墨块，己的短竖
    横折映到鸿蒙长竖 ㇕ 曾不可救药）。返回点列或 None（退化）。"""
    import heapq
    from shapely.geometry import MultiPoint, Polygon, Point
    from shapely.ops import voronoi_diagram, unary_union

    polys = []
    for lp in loops:
        if len(lp) >= 4:
            try:
                pg = Polygon(lp)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                if not pg.is_empty:
                    polys.append(pg)
            except Exception:
                pass
    if not polys:
        return None
    region = polys[0]
    for pg in polys[1:]:
        try:
            region = region.symmetric_difference(pg)
        except Exception:
            region = region.buffer(0).symmetric_difference(pg.buffer(0))
    if region.is_empty or region.area < 25:
        return None
    if hasattr(region, "geoms"):
        region = max(region.geoms, key=lambda g: g.area)

    bnd = []
    for ring in [region.exterior] + list(region.interiors):
        coords = list(ring.coords)
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            n = max(1, int(math.ceil(L / step)))
            for k in range(n):
                t = k / n
                bnd.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    if len(bnd) < 8:
        return None
    try:
        vd = voronoi_diagram(MultiPoint(bnd), edges=True)
    except Exception:
        return None
    inner = region.buffer(-0.05) if region.area > 100 else region

    def key(p):
        return (round(p[0], 1), round(p[1], 1))

    adj = {}
    edgeGeoms = []
    for geom in getattr(vd, "geoms", []):
        if hasattr(geom, "geoms"):
            edgeGeoms.extend(geom.geoms)
        else:
            edgeGeoms.append(geom)
    for geom in edgeGeoms:
        coords = list(geom.coords)
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            try:
                if not (inner.contains(Point(a)) and inner.contains(Point(b))):
                    continue
            except Exception:
                continue
            ka, kb = key(a), key(b)
            if ka == kb:
                continue
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            adj.setdefault(ka, {})[kb] = min(adj.get(ka, {}).get(kb, 1e18), L)
            adj.setdefault(kb, {})[ka] = min(adj.get(kb, {}).get(ka, 1e18), L)
    if len(adj) < 2:
        return None

    def farthest(src):
        distMap = {src: 0.0}
        prev = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > distMap.get(u, 1e18):
                continue
            for v, w in adj[u].items():
                nd = d + w
                if nd < distMap.get(v, 1e18):
                    distMap[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        far = max(distMap, key=distMap.get)
        return far, prev, distMap[far]

    start = next(iter(adj))
    u, _, _ = farthest(start)
    v, prev, _ = farthest(u)
    path = [v]
    while path[-1] != u:
        path.append(prev[path[-1]])
    pts = [(p[0], p[1]) for p in path]
    if polylineLength(pts) < 4:
        return None

    # 端枝剪除：矩形端帽处中轴分叉出 45° 角枝（伸向端帽角落、到边界
    # 余隙递减趋零），直径路径会带上一条。从两端向内丢弃余隙 <0.8×
    # 路径中位余隙的点，剪掉角枝、留主干
    bndRings = [region.exterior] + list(region.interiors)

    def clearance(p):
        pt = Point(p)
        return min(r.distance(pt) for r in bndRings)

    clr = [clearance(p) for p in pts]
    sc = sorted(clr)
    medClr = sc[len(sc) // 2]
    lo = 0
    hi = len(pts) - 1
    while lo < hi and clr[lo] < medClr * 0.9:
        lo += 1
    while hi > lo and clr[hi] < medClr * 0.9:
        hi -= 1
    if hi - lo >= 1:
        pts = pts[lo:hi + 1]
    if polylineLength(pts) < 4:
        return None

    # 轻度平滑压 Voronoi 采样抖动（端点不动）
    for _ in range(2):
        if len(pts) < 3:
            break
        pts = [pts[0]] + \
            [((pts[i - 1][0] + pts[i][0] + pts[i + 1][0]) / 3.0,
              (pts[i - 1][1] + pts[i][1] + pts[i + 1][1]) / 3.0)
             for i in range(1, len(pts) - 1)] + [pts[-1]]

    # 端点补齐：中轴天然停在距端帽约半笔宽处，沿端部切向延伸至轮廓。
    # 切向取端部 ~40 单位整段方向（最后一小段可能是剪剩的角枝残尾，
    # 沿它延伸会把 45° 刺重新长出来）
    def endTangent(ptsIn, endIdx):
        n2 = len(ptsIn)
        i0 = 0 if endIdx == 0 else n2 - 1
        inward = 1 if endIdx == 0 else -1
        p = ptsIn[i0]
        j = i0
        acc = 0.0
        while 0 <= j + inward < n2 and acc < 40.0:
            j += inward
            acc = math.hypot(p[0] - ptsIn[j][0], p[1] - ptsIn[j][1])
        q = ptsIn[j]
        dx, dy = p[0] - q[0], p[1] - q[1]
        L = math.hypot(dx, dy)
        return (dx / L, dy / L) if L > 1e-6 else None

    def extend(ptsIn):
        out = list(ptsIn)
        for endIdx in (0, -1):
            tang = endTangent(out, endIdx)
            if not tang:
                continue
            ux, uy = tang
            p = out[endIdx]
            lo2, hi2 = 0.0, 400.0
            for _ in range(18):
                midT = (lo2 + hi2) / 2
                if region.contains(Point(p[0] + ux * midT, p[1] + uy * midT)):
                    lo2 = midT
                else:
                    hi2 = midT
            if lo2 > 2.0:
                ext = (p[0] + ux * lo2 * 0.9, p[1] + uy * lo2 * 0.9)
                if endIdx == 0:
                    out.insert(0, ext)
                else:
                    out.append(ext)
        return out

    return extend(pts)

    # 端点补齐：中轴天然停在距端帽约半笔宽处，沿端部切向延伸至轮廓
    def extend(ptsIn):
        out = list(ptsIn)
        for endIdx, refIdx in ((0, 1), (-1, -2)):
            p = out[endIdx]
            q = out[refIdx]
            dx, dy = p[0] - q[0], p[1] - q[1]
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            ux, uy = dx / L, dy / L
            lo, hi = 0.0, 400.0
            for _ in range(18):
                midT = (lo + hi) / 2
                if region.contains(Point(p[0] + ux * midT, p[1] + uy * midT)):
                    lo = midT
                else:
                    hi = midT
            if lo > 2.0:
                ext = (p[0] + ux * lo * 0.9, p[1] + uy * lo * 0.9)
                if endIdx == 0:
                    out.insert(0, ext)
                else:
                    out.append(ext)
        return out

    return extend(pts)


def midpointRectify(med, loops):
    """法向中点矫正：中线每点沿"候选法向"与轮廓两侧求交、移到最窄合法
    弦的中点。候选方向 = 该点局部切向 + 全线主导方向（角度直方图里占
    弧长≥20%的方向）——顿笔小弯处的局部切向被弯带歪、法线斜穿笔杆
    弦宽异常，而真垂直于笔杆的方向弦必最窄，取最窄弦即自动选对方向，
    顿笔点被拉回杆芯。中线形状由此完全取决于轮廓自身几何，楷体初值
    只再贡献方向与点数。弦宽异常（>2.5×中位或不足其1/5）的点不动。
    loops: 已展平的闭合轮廓点列列表。"""
    n = len(med)
    if n < 2 or not loops:
        return med
    edges = []
    for lp in loops:
        m = len(lp)
        if m < 3:
            continue
        for i in range(m):
            a, b = lp[i], lp[(i + 1) % m]
            if abs(a[0] - b[0]) > 1e-9 or abs(a[1] - b[1]) > 1e-9:
                edges.append((a, b))
    if not edges:
        return med

    # 主导方向：分段角度直方图（mod 180°, 15°桶, 弧长加权），≥20%弧长的桶
    binLen = [0.0] * 12
    binAng = [0.0] * 12
    total = 0.0
    for i in range(n - 1):
        dx, dy = med[i + 1][0] - med[i][0], med[i + 1][1] - med[i][1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            continue
        a = math.degrees(math.atan2(dy, dx)) % 180.0
        b = int(a // 15) % 12
        binLen[b] += L
        binAng[b] += a * L
        total += L
    domAngles = [binAng[b] / binLen[b] for b in range(12)
                 if total > 0 and binLen[b] >= total * 0.2]

    def chordAt(p, tangDeg):
        """沿 tangDeg 方向的法向弦：(宽, 中点偏移, nx, ny) 或 None。"""
        rad = math.radians(tangDeg)
        nx, ny = -math.sin(rad), math.cos(rad)
        sPos, sNeg = None, None
        for (q0, q1) in edges:
            ex, ey = q1[0] - q0[0], q1[1] - q0[1]
            den = ex * ny - ey * nx
            if abs(den) < 1e-9:
                continue
            wx, wy = p[0] - q0[0], p[1] - q0[1]
            u = (wx * ny - wy * nx) / den
            if u < -1e-9 or u > 1 + 1e-9:
                continue
            s = ((q0[0] + u * ex - p[0]) * nx + (q0[1] + u * ey - p[1]) * ny)
            if s > 1e-6:
                if sPos is None or s < sPos:
                    sPos = s
            elif s < -1e-6:
                if sNeg is None or s > sNeg:
                    sNeg = s
        if sPos is None or sNeg is None:
            return None
        return (sPos - sNeg, (sPos + sNeg) / 2.0, nx, ny)

    hits = []
    for i in range(n):
        p = med[i]
        a = med[max(0, i - 1)]
        b = med[min(n - 1, i + 1)]
        tx, ty = b[0] - a[0], b[1] - a[1]
        cands = list(domAngles)
        if math.hypot(tx, ty) > 1e-6:
            cands.append(math.degrees(math.atan2(ty, tx)))
        best = None
        second = None
        for ang in cands:
            h = chordAt(p, ang)
            if h and abs(h[1]) <= h[0]:
                if best is None or h[0] < best[0]:
                    second = best
                    best = h
                elif second is None or h[0] < second[0]:
                    second = h
        # 方向歧义保护：拐角区两个方向的弦宽接近（差<25%）说明该点
        # 同时"属于"两臂，任选一方向矫正会来回摆产生锯齿——不动
        if best and second and second[0] < best[0] * 1.25 and \
                abs(second[2] * best[2] + second[3] * best[3]) < 0.7:
            best = None
        hits.append(best)
    widths = sorted(h[0] for h in hits if h)
    if not widths:
        return med
    medW = widths[len(widths) // 2]
    out = []
    for i, p in enumerate(med):
        h = hits[i]
        if h and 0.2 * medW <= h[0] <= 2.5 * medW:
            out.append((p[0] + h[2] * h[1], p[1] + h[3] * h[1]))
        else:
            out.append(tuple(p))
    return out


# ---------------------------------------------------------------- 尺度不变形状描述子

def shapeDescriptor(paths):
    """切向直方图(mod180°,9桶,弧长加权) + PCA主轴/伸长率 + 充实率。"""
    hist = [0.0] * 9
    cx = cy = totalLen = areaSum = 0.0
    x0 = y0 = 1e18
    x1 = y1 = -1e18
    pieces = []
    for d in paths:
        for c in parseContours(d):
            poly = flattenSegs(c["segs"], 14)
            areaSum += signedArea(poly)
            for i in range(len(poly) - 1):
                a, b = poly[i], poly[i + 1]
                L = dist(a, b)
                if L < 1e-6:
                    continue
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                ang = math.atan2(b[1] - a[1], b[0] - a[0])
                pieces.append((mx, my, L, ang))
                cx += mx * L
                cy += my * L
                totalLen += L
                x0 = min(x0, a[0]); y0 = min(y0, a[1])
                x1 = max(x1, a[0]); y1 = max(y1, a[1])
    if totalLen < 1e-6:
        return None
    cx /= totalLen
    cy /= totalLen
    sxx = syy = sxy = 0.0
    for mx, my, L, ang in pieces:
        dx, dy = mx - cx, my - cy
        sxx += dx * dx * L
        syy += dy * dy * L
        sxy += dx * dy * L
        a = math.degrees(ang) % 180.0
        hist[min(8, int(a / 20))] += L
    sxx /= totalLen; syy /= totalLen; sxy /= totalLen
    tr = sxx + syy
    det = sxx * syy - sxy * sxy
    disc = math.sqrt(max(0.0, tr * tr / 4 - det))
    elong = math.sqrt(max(tr / 2 + disc, 1e-9) / max(tr / 2 - disc, 1e-9))
    mainAngle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    hsum = sum(hist) or 1.0
    hist = [h / hsum for h in hist]
    bw = max(1.0, x1 - x0)
    bh = max(1.0, y1 - y0)
    fill = min(1.0, abs(areaSum) / (bw * bh))
    return {"hist": hist, "elong": min(elong, 8.0), "mainAngle": mainAngle, "fill": fill}


def shapeDistance(a, b):
    if not a or not b:
        return 9.0
    hd = sum(abs(a["hist"][i] - b["hist"][i]) for i in range(9))
    ad = abs(a["mainAngle"] - b["mainAngle"]) * 180 / math.pi
    ad = min(ad, 180 - ad) / 90
    ed = min(1.5, abs(math.log(a["elong"] / b["elong"])))
    fd = abs(a["fill"] - b["fill"])
    return hd * 1.1 + ad * 0.9 + ed * 0.6 + fd * 1.2


def shapeSimilarity(a, b):
    return max(0, round(100 * (1 - shapeDistance(a, b) / 3)))


# ---------------------------------------------------------------- 中轴线精调

def corridorOffset(pt, tanDir, contours, cap, touch):
    """沿 pt 处法向找字形边界双侧交点，返回 pt 所在（或紧邻）实体断面的
    中点偏移量（沿法向的带符号距离）；断面过宽或找不到则返回 None。"""
    dx, dy = tanDir
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return None
    nx, ny = -dy / L, dx / L
    px, py = pt

    def filled(q):
        cnt = 0
        for c in contours:
            if pointInPolygon(q, c["poly"]):
                cnt += -1 if c["isHole"] else 1
        return cnt > 0

    ts = []
    for c in contours:
        poly = c["poly"]
        for j in range(len(poly) - 1):
            x1, y1 = poly[j]
            x2, y2 = poly[j + 1]
            ex, ey = x2 - x1, y2 - y1
            det = ex * ny - ey * nx
            if abs(det) < 1e-12:
                continue
            t = (ex * (y1 - py) - ey * (x1 - px)) / det
            s = (nx * (y1 - py) - ny * (x1 - px)) / det
            if 0.0 <= s < 1.0 and abs(t) <= cap:
                ts.append(t)
    ts.sort()
    best = None
    for j in range(len(ts) - 1):
        t0, t1 = ts[j], ts[j + 1]
        if t1 - t0 < 2.0 or t1 - t0 > cap * 1.5:
            continue
        if t0 > touch or t1 < -touch:
            continue
        mid = (t0 + t1) / 2
        if not filled((px + nx * mid, py + ny * mid)):
            continue
        if best is None or abs(mid) < abs(best):
            best = mid
    return best


def corridorPoint(pt, tanDir, contours, cap, touch):
    """corridorOffset 的取点版：返回断面中点坐标，找不到返回 None。"""
    off = corridorOffset(pt, tanDir, contours, cap, touch)
    if off is None:
        return None
    dx, dy = tanDir
    L = math.hypot(dx, dy) or 1.0
    return (pt[0] - dy / L * off, pt[1] + dx / L * off)


def recenterMedian(m, contours, cap):
    """中轴线垂直断面居中：沿各点法向找字形边界双侧交点，移到所在实体
    断面的中点。治精调只按己方样本拟合导致的贴边漂移（口的竖曾贴住
    内侧缘，外缘样本反被邻笔评分抢走）。拐角点与断面过宽（跨越交叠
    区/邻笔）处不动。"""
    n = len(m)
    out = list(m)
    cos35 = math.cos(math.radians(35))
    touch = max(8.0, cap * 0.15)
    for i in range(n):
        a, b = m[max(0, i - 1)], m[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        if 0 < i < n - 1:
            v1 = (m[i][0] - m[i - 1][0], m[i][1] - m[i - 1][1])
            v2 = (m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            if l1 > 1e-6 and l2 > 1e-6 and \
               (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) < cos35:
                continue
        off = corridorOffset(m[i], (dx, dy), contours, cap, touch)
        if off is not None and abs(off) > 0.5:
            L = math.hypot(dx, dy)
            out[i] = (m[i][0] - dy / L * off, m[i][1] + dx / L * off)
    return out


def refineMedianFit(m, m0, pts, width):
    """直笔→平移+主方向伸缩；折笔→按拐角分段平移（中位数偏移+位移硬上限）。"""
    maxDrift = max(80.0, width * 1.3)

    def medOf(arr):
        s = sorted(arr)
        return s[len(s) // 2]

    mLen = polylineLength(m)
    chord = dist(m[0], m[-1])
    cap = width * 0.7

    def clampVec(dx, dy):
        L = math.hypot(dx, dy)
        if L > cap:
            return (dx * cap / L, dy * cap / L)
        return (dx, dy)

    corners = []
    for i in range(1, len(m) - 1):
        ax, ay = m[i][0] - m[i - 1][0], m[i][1] - m[i - 1][1]
        bx, by = m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la < 8 or lb < 8:
            continue
        cosv = (ax * bx + ay * by) / (la * lb)
        if cosv < math.cos(math.radians(35)):
            corners.append(i)

    if not corners and chord > 1e-6 and chord / mLen > 0.85:
        mcx = sum(p[0] for p in m) / len(m)
        mcy = sum(p[1] for p in m) / len(m)
        dxs, dys = [], []
        for p in pts:
            nr = nearestOnPolyline(p, m)
            dxs.append(p[0] - nr["pt"][0])
            dys.append(p[1] - nr["pt"][1])
        dx, dy = clampVec(medOf(dxs) * 0.55, medOf(dys) * 0.55)
        ux, uy = (m[-1][0] - m[0][0]) / chord, (m[-1][1] - m[0][1]) / chord
        projS = sorted((p[0] - mcx) * ux + (p[1] - mcy) * uy for p in pts)
        lo = projS[int(len(projS) * 0.03)]
        hi = projS[int(len(projS) * 0.97)]
        projM = [(p[0] - mcx) * ux + (p[1] - mcy) * uy for p in m]
        mLo, mHi = min(projM), max(projM)
        span = max(1.0, mHi - mLo)
        su = max(0.7, min(1.8, (hi - lo) / span))
        offU = (lo + hi) / 2 - (mLo + mHi) / 2 * su
        result = []
        for p in m:
            a = (p[0] - mcx) * ux + (p[1] - mcy) * uy
            b = (p[0] - mcx) * -uy + (p[1] - mcy) * ux
            a2 = a * su + offU
            result.append((mcx + ux * a2 - uy * b + dx, mcy + uy * a2 + ux * b + dy))
        # 位移上限：沿弦向用 maxDrift（伸缩需要），横向从紧——直笔端点
        # 横向大漂会扎进邻笔轮廓（口的横两端曾潜入左右竖腿）
        latCap = max(30.0, width * 0.45)
        out = []
        for p, q in zip(result, m0):
            ddx, ddy = p[0] - q[0], p[1] - q[1]
            al = max(-maxDrift, min(maxDrift, ddx * ux + ddy * uy))
            lt = max(-latCap, min(latCap, ddx * -uy + ddy * ux))
            out.append((q[0] + ux * al - uy * lt, q[1] + uy * al + ux * lt))
        return out
    else:
        bounds = [0] + corners + [len(m) - 1]
        nSec = len(bounds) - 1

        def secOfSeg(i):
            for s in range(nSec):
                if bounds[s] <= i < bounds[s + 1]:
                    return s
            return nSec - 1

        accX = [[] for _ in range(nSec)]
        accY = [[] for _ in range(nSec)]
        ptsSec = [[] for _ in range(nSec)]
        for p in pts:
            nr = nearestOnPolyline(p, m)
            s = secOfSeg(nr["idx"])
            accX[s].append(p[0] - nr["pt"][0])
            accY[s].append(p[1] - nr["pt"][1])
            ptsSec[s].append(p)
        offs = []
        for s in range(nSec):
            if len(accX[s]) >= 3:
                offs.append(clampVec(medOf(accX[s]) * 0.55, medOf(accY[s]) * 0.55))
            else:
                offs.append(None)
        for s in range(nSec):
            if offs[s] is None:
                offs[s] = (offs[s - 1] if s > 0 and offs[s - 1] else None) \
                          or (offs[s + 1] if s + 1 < nSec and offs[s + 1] else None) \
                          or (0.0, 0.0)
        result = []
        for i, p in enumerate(m):
            sA = sB = None
            for s in range(nSec):
                if bounds[s] <= i <= bounds[s + 1]:
                    if sA is None:
                        sA = s
                    else:
                        sB = s
            o1 = offs[sA] if sA is not None else (0.0, 0.0)
            o2 = offs[sB] if sB is not None else o1
            result.append((p[0] + (o1[0] + o2[0]) / 2, p[1] + (o1[1] + o2[1]) / 2))
        # 折笔分段伸缩（仅两段折）：按本段样本沿段轴投影跨度缩放，锚定拐角
        # 向自由端生长——楷体折段比例偏短时（口的横折竖段）单靠平移够不到底
        if nSec == 2 and len(m) >= 3:
            cIdx = bounds[1]
            corner = result[cIdx]
            for s in range(2):
                if len(ptsSec[s]) < 6:
                    continue
                endIdx = 0 if s == 0 else len(m) - 1
                ax, ay = result[endIdx][0] - corner[0], result[endIdx][1] - corner[1]
                La = math.hypot(ax, ay)
                if La < 40:
                    continue
                ux, uy = ax / La, ay / La
                projS = sorted((p[0] - corner[0]) * ux + (p[1] - corner[1]) * uy
                               for p in ptsSec[s])
                su = max(0.85, min(1.35, projS[int(len(projS) * 0.97)] / La))
                lo, hi = (0, cIdx) if s == 0 else (cIdx, len(m) - 1)
                for i in range(lo, hi + 1):
                    if i == cIdx:
                        continue
                    p = result[i]
                    a = (p[0] - corner[0]) * ux + (p[1] - corner[1]) * uy
                    b = (p[0] - corner[0]) * -uy + (p[1] - corner[1]) * ux
                    result[i] = (corner[0] + ux * a * su - uy * b,
                                 corner[1] + uy * a * su + ux * b)
        # 位移上限：横向仍用 maxDrift；沿笔轴放宽——折段伸缩必须能越过初始端点
        alongCap = max(maxDrift, 0.45 * mLen)
        out = []
        for p, q in zip(result, m0):
            dx, dy = p[0] - q[0], p[1] - q[1]
            tx, ty = nearestOnPolyline(q, m0)["tan"]
            al = max(-alongCap, min(alongCap, dx * tx + dy * ty))
            lt = max(-maxDrift, min(maxDrift, dx * -ty + dy * tx))
            out.append((q[0] + tx * al - ty * lt, q[1] + ty * al + tx * lt))
        return out
