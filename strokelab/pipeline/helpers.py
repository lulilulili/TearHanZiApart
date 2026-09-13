# -*- coding: utf-8 -*-
"""strokelab.pipeline.helpers — 模块级纯函数（无管线状态依赖）。

从单文件 pipeline.py 原样迁出：匈牙利指派、折返计数、轴向守卫判据、
自洽回灌种子提取、二遍择优守卫等。所有算法注释（事故史档案）随代码保留。
"""

import math

from ..geometry import (dist, parseContours, flattenSegs, bboxOfPoints,
                        nearestOnPolyline, polylineLength, recenterMedian,
                        shapeDescriptor)

def _resampleByArcN(poly, n):
    """按弧长等距重采样为恰 n 点（首点保留；退化折线按首点填充）。"""
    L = polylineLength(poly)
    if L < 1e-6:
        return [tuple(poly[0])] * n
    step = L / (n - 1)
    out = [tuple(poly[0])]
    carry = 0.0
    for i in range(len(poly) - 1):
        x1, y1 = poly[i]
        x2, y2 = poly[i + 1]
        seg = math.hypot(x2 - x1, y2 - y1)
        if seg < 1e-9:
            continue
        t = step - carry
        while t <= seg and len(out) < n:
            u = t / seg
            out.append((x1 + (x2 - x1) * u, y1 + (y2 - y1) * u))
            t += step
        carry = seg - (t - step)
    while len(out) < n:
        out.append(tuple(poly[-1]))
    return out


def _medianDeviation(placed, kaiPlaced):
    """模板骨架放置后与楷体中轴线的形态偏差：按弧长重采样 24 点对齐的
    平均点距 / 楷体中轴线包围盒对角线。None=无法比较。"""
    if len(placed) < 2 or len(kaiPlaced) < 2:
        return None
    b = bboxOfPoints(kaiPlaced)
    diag = math.hypot(b.w, b.h)
    if diag < 1e-6:
        return None
    n = 24
    a = _resampleByArcN(placed, n)
    b2 = _resampleByArcN(kaiPlaced, n)
    avg = sum(dist(p, q) for p, q in zip(a, b2)) / n
    return avg / diag


def _hungarian(cost):
    """O(n^3) 匈牙利算法（方阵最小代价完美匹配），返回每行匹配的列号。"""
    n = len(cost)
    INF = 1e18
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    ans = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            ans[p[j] - 1] = j - 1
    return ans


def _switchbackCount(m, thr=75.0):
    """折返计数：相邻段方向角变化超 thr 的内点数（中轴线蛇形伪影探测）。"""
    n = 0
    cosThr = math.cos(math.radians(thr))
    for i in range(1, len(m) - 1):
        ax, ay = m[i][0] - m[i - 1][0], m[i][1] - m[i - 1][1]
        bx, by = m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la > 1e-9 and lb > 1e-9 and \
           (ax * bx + ay * by) / (la * lb) < cosThr:
            n += 1
    return n


def _axisFails(result):
    """横/竖的切割主轴偏离楷体该笔的弦向>32°。
    → [(笔序, 偏差度), ...]"""
    bad = []
    for s in result["strokes"]:
        if s["failed"] or s["type"] not in ("横", "竖"):
            continue
        d = shapeDescriptor([s["path"]])
        if d and d["elong"] >= 1.8:
            ang = math.degrees(d["mainAngle"]) % 180.0
            medians = result.get("kai", {}).get("medians", [])
            km = medians[s["index"]] if s["index"] < len(medians) else []
            if len(km) >= 2 and dist(tuple(km[0]), tuple(km[-1])) > 1e-6:
                ref = math.degrees(math.atan2(km[-1][1] - km[0][1],
                                             km[-1][0] - km[0][0])) % 180.0
            else:
                ref = 0.0 if s["type"] == "横" else 90.0
            dev = abs(ang - ref)
            dev = min(dev, 180.0 - dev)
            if dev > 32.0:
                bad.append((s["index"], dev))
    return bad


def _reMedianFromStroke(median, strokePath, width):
    """自洽回灌的种子提取：用笔画自身几何重提中轴——轻平滑（拐角除外）+
    对本笔轮廓断面居中，迭代两轮。B 骨架抖动、只按己方样本拟合造成的
    贴边/锯齿都会被实际笔画区域的断面中点洗掉。"""
    cons = []
    for c in parseContours(strokePath):
        poly = flattenSegs(c["segs"], 10)
        if len(poly) >= 3:
            cons.append({"poly": poly, "isHole": False})
    m = [tuple(p) for p in median]
    if not cons or len(m) < 2:
        return None
    cap = max(1.6 * width, 40.0)
    cos35 = math.cos(math.radians(35))
    for _ in range(2):
        if len(m) >= 3:
            sm = [m[0]]
            for i in range(1, len(m) - 1):
                v1 = (m[i][0] - m[i - 1][0], m[i][1] - m[i - 1][1])
                v2 = (m[i + 1][0] - m[i][0], m[i + 1][1] - m[i][1])
                l1, l2 = math.hypot(*v1), math.hypot(*v2)
                if l1 > 1e-6 and l2 > 1e-6 and \
                   (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) < cos35:
                    sm.append(m[i])
                else:
                    sm.append((0.25 * m[i - 1][0] + 0.5 * m[i][0] + 0.25 * m[i + 1][0],
                               0.25 * m[i - 1][1] + 0.5 * m[i][1] + 0.25 * m[i + 1][1]))
            sm.append(m[-1])
            m = sm
        m = recenterMedian(m, cons, cap)
    return m


def _selfSeeds(result):
    seeds = []
    changed = False
    for s in result["strokes"]:
        rm = None
        if not s["failed"] and s["path"]:
            rm = _reMedianFromStroke(s["median"], s["path"], s["width"])
        if rm is None:
            seeds.append(s["median"])
            continue
        seeds.append(rm)
        if not changed:
            # 种子与精调中轴几乎重合时不算变化——重跑必然收敛回原样、
            # 过不了采纳门槛，白付一遍全程
            m0 = [tuple(p) for p in s["median"]]
            disp = sum(nearestOnPolyline(p, m0)["d"]
                       for p in rm) / (len(rm) or 1)
            if disp > max(2.5, 0.08 * s["width"]):
                changed = True
    return seeds if changed else None


def _meanOf(result, key):
    ss = result["strokes"]
    return sum(s[key] for s in ss) / (len(ss) or 1)


def _strokeCenter(path):
    pts = []
    for c in parseContours(path):
        pts.extend(flattenSegs(c["segs"], 40))
    if not pts:
        return None
    b = bboxOfPoints(pts)
    return ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)


def _secondPassBetter(r2, r1, expCenters, diag):
    # 并集硬保证守卫：第二遍不得引入覆盖/溢出违规（流江"水"的二遍
    # 曾溢出 14.2% 仍被 retain 虚高采纳）
    u1 = r1.get("unionCheck") or {}
    u2 = r2.get("unionCheck") or {}
    if u2.get("cover", 100) < min(99.0, u1.get("cover", 100)) - 0.05:
        return False
    if abs(u2.get("excess", 0)) > max(0.5, abs(u1.get("excess", 0))):
        return False
    f1 = sum(1 for s in r1["strokes"] if s["failed"])
    f2 = sum(1 for s in r2["strokes"] if s["failed"])
    # 结构位置守卫：每笔质心对楷体映射位置的偏差不得比第一遍显著恶化——
    # shapeSim 尺度不变、看不见"整笔挪去别人地盘"（爱的竖曾被换到左下角
    # 反而 sim 升高）
    for a, b, exp in zip(r1["strokes"], r2["strokes"], expCenters):
        if a["failed"] or b["failed"] or exp is None:
            continue
        ca, cb = _strokeCenter(a["path"]), _strokeCenter(b["path"])
        if ca is None or cb is None:
            continue
        if dist(cb, exp) > dist(ca, exp) + 0.04 * diag:
            return False
    if f2 != f1:
        return f2 < f1
    # retain 升但 sim 明显降可能是"整块吞并"式虚高，双指标把关
    return (_meanOf(r2, "retainRatio") > _meanOf(r1, "retainRatio") + 0.005
            and _meanOf(r2, "shapeSim") >= _meanOf(r1, "shapeSim") - 1.0)
