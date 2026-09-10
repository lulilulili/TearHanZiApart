# -*- coding: utf-8 -*-
"""strokelab.classify — 笔画分类器、规则表、Unicode 笔画区命名、类型匹配。"""

import math

from .geometry import resamplePolyline, dist

# 用户整理的 32 类笔画 × 独立/少相交代表字规则表（B/A 库建库依据）
PROBE_TABLE = {
    "横": "一 二·下 三·下",
    "竖": "川·中 川·右 旧·左",
    "撇": "八·左 儿·左 川·左",
    "点": "太·下 犬·右上",
    "捺": "八·右 人·右下",
    "提": "刁·下 冰·左下 打·左下",
    "横折": "己尸匡口日",
    "横钩": "买·上",
    "横撇": "水·左",
    "横折钩": "门·右 司·右 月·右 刀·右上",
    "横折提": "认·左下",
    "横折弯": "朵·右上",
    "横折弯钩": "几·右 九·右 乙",
    "横斜钩": "飞·下",
    "横折折": "卍·左 卐·右 凹·上",
    "横折折撇": "及·右",
    "横折折折": "凸·右",
    "横折折折钩": "乃·右",
    "横撇弯钩": "队·左上",
    "竖折": "区·左下 凶·下",
    "竖钩": "小·中 水·中 丁·下",
    "竖提": "以·左 长·左 氏·左下",
    "竖弯": "四·右 西·中 酉·中",
    "竖弯钩": "儿·右 七·下 元·右下",
    "竖折撇": "专·下",
    "竖折折": "卐·左 鼎·左",
    "竖折折钩": "与·中",
    "撇折": "公·下",
    "撇点": "巡·右上",
    "斜钩": "弋·右下",
    "弯钩": "了·下",
    "卧钩": "心·下",
}
TYPE_ORDER = list(PROBE_TABLE.keys())

# 位置词 → 归一化坐标（楷体空间 y 向上；x 0=左 1=右，y 0=下 1=上）
PROBE_POSITIONS = {
    "上": (0.5, 0.8), "下": (0.5, 0.2), "左": (0.2, 0.5), "右": (0.8, 0.5),
    "中": (0.5, 0.5), "左上": (0.2, 0.8), "右上": (0.8, 0.8),
    "左下": (0.2, 0.2), "右下": (0.8, 0.2),
}


def parseProbes(spec):
    """规则表条目解析，三种写法共存（空格可选）：
      "太"      自动匹配（按类型投票，兼容旧格式）
      "太4"     明确笔序：取第 4 笔
      "太·右下" 相对位置：类型过滤后取质心最靠近该方位的笔
    → [(字, 笔序0基|None, 位置词|None), ...]"""
    out = []
    i = 0
    while i < len(spec):
        ch = spec[i]
        i += 1
        if ch.isspace():
            continue
        idx = None
        pos = None
        j = i
        while j < len(spec) and spec[j].isdigit():
            j += 1
        if j > i:
            idx = int(spec[i:j]) - 1
            i = j
        elif i < len(spec) and spec[i] == "·":
            i += 1
            for w in sorted(PROBE_POSITIONS, key=len, reverse=True):
                if spec.startswith(w, i):
                    pos = w
                    i += len(w)
                    break
        out.append((ch, idx, pos))
    return out

SINGLE_STROKE_TYPES = {"一": "横", "丨": "竖", "丶": "点", "丿": "撇",
                       "亅": "竖钩", "乙": "横折弯钩"}

# 相似笔画组（用户规则）：不同字体设计有微妙差别，同一笔可能在类型边界
# 摇摆——一个字体的横折钩在另一个字体形如横折。模板匹配时同组类型互为
# 候选（借用顺序=组内顺序），配合模板-结构一致性检查逐个试。
SIMILAR_GROUPS = [
    ["提", "横"],
    ["横折", "横折钩", "横撇", "横钩"],
    ["横折折", "横折提"],
    ["横折折撇", "横折折折"],
    ["竖", "竖撇"],
    ["撇", "提"],
    ["点", "捺"],
    ["竖提", "斜钩", "竖弯"],
    ["竖", "竖钩", "弯钩"],
    ["竖折折", "竖折弯钩"],
    ["扁斜钩", "竖弯钩"],
    ["横斜钩", "横斜弯钩"],
]


def similarTypes(t):
    """t 的相似类型候选序列（不含 t 自身；按组内顺序、跨组去重）。"""
    out = []
    for grp in SIMILAR_GROUPS:
        if t in grp:
            for x in grp:
                if x != t and x not in out:
                    out.append(x)
    return out

# 楷体类型 → 目标字体取材映射（用户裁定规则表 v13，覆盖审计后全部 70 类）。
# 文鼎楷体笔形与标准笔画差异大（书法方言），每类指定目标字体取字集：
# take = 按优先级递减的 (笔画区码位|None, 探针字|None, 位置词|None)——
#   码位有字形则直取，探针字提取（孤立连通组）作为并行候选，逐笔匹配时
#   由模板-结构一致性偏差闸挑选；fuse = 整字融合字形做模板（乙/几）；
# compose = 组合笔画序列（按顺序首尾拼接，如 鬮14 提折钩=竖+横折钩）。
KAI_TARGET_MAP = {
    "横": {"take": [(0x31D0, "一", None)]},
    "竖": {"take": [(0x31D1, "旧", "左")]},
    "点": {"take": [(0x31D4, "勺", "中")]},
    "撇": {"take": [(0x31D2, "八", "左")]},
    "横折": {"take": [(0x31D5, "己", "上")]},
    "捺": {"take": [(0x31D2, "八", "右"), (0x31DF, "儿", "右")]},
    "横折钩": {"take": [(0x31C6, "刁", "右")]},
    "竖折": {"take": [(0x31C4, "亡", "下")]},
    "竖钩": {"take": [(0x31DA, "小", "中")]},
    "横撇": {"take": [(0x31D6, "买", "上")]},
    "撇折": {"take": [(0x31DC, "公", "下")]},
    "竖撇": {"take": [(0x31D1, "旧", "左"), (0x31DA, "小", "中")]},
    "竖折提": {"take": [(0x31DF, "儿", "右")]},
    "提": {"take": [(0x31C6, "刁", "左")]},
    "捺提": {"take": [(0x31DF, "儿", "右")]},
    "竖捺": {"take": [(0x31DB, "巡", "右")]},
    "捺折": {"take": [(0x31D1, "旧", "左"), (0x31D2, "八", "左")]},
    "竖折折钩": {"take": [(0x31C9, "亏", "下")]},
    "横钩": {"take": [(0x31D6, "买", "上")]},
    "弯钩": {"take": [(0x31D9, "以", "左"), (0x31D1, "旧", "左")]},
    "横折折": {"take": [(0x31CA, "认", "左下")]},
    "撇钩": {"take": [(0x31D2, "八", "左")]},
    "横捺": {"take": [(0x31D0, "一", None)]},
    "竖提": {"take": [(0x31DF, "儿", "右"), (0x31C2, "弋", "右下")]},
    "捺撇": {"take": [(0x31D2, "八", "左")]},
    "横折提": {"take": [(0x31C8, "几", "右")]},
    "横捺钩": {"take": [(0x31DC, "公", "下")], "fuse": "乙"},
    "横捺撇": {"take": [(0x31D6, "买", "上")]},
    "竖折折": {"take": [(0x31DE, "吳", "中"), (0x31E5, None, None),
                        (0x31C9, "亏", "下")]},
    "横折撇": {"take": [(0x31C6, "刁", "右")]},
    "横捺提": {"take": [(0x31C8, "几", "右")]},
    "撇点": {"take": [(0x31DB, "巡", "右")]},
    "横提": {"take": [(0x31C0, "刁", "左")]},
    "卧钩": {"take": [(0x31DF, "儿", "右")]},
    "竖弯钩": {"take": [(0x31DF, "儿", "右")]},
    "横折折折钩": {"take": [(0x31E1, "乃", "右")]},
    "横折折撇": {"take": [(0x31CB, "及", "右")]},
    "横撇钩": {"take": [(0x31D6, "买", "上")]},
    "横折折提": {"take": [(0x31DC, "公", "下")], "fuse": "乙"},
    "竖撇折": {"take": [(0x31DC, "公", "下")]},
    "斜钩": {"take": [(0x31DF, "儿", "右")]},
    "捺折撇": {"take": [(0x31C6, "刁", "右"), (0x31D1, "旧", "右")]},
    "捺折钩": {"take": [(0x31C6, "刁", "右"), (0x31D1, "旧", "右")]},
    "横折弯钩": {"fuse": "乙"},
    "横捺折": {"take": [(0x31D5, "己", "上")]},
    "横撇折折钩": {"take": [(0x31E1, "乃", "右")]},
    "横捺折钩": {"take": [(0x31C6, "刁", "右")]},
    "横撇弯钩": {"fuse": "乙"},
    "横折捺钩": {"take": [(0x31CB, "及", "右")]},
    "横折捺撇": {"take": [(0x31CB, "及", "右")]},
    "横折折折": {"take": [(0x31CE, "凸", "右"), (0x31CB, "及", "右")]},
    "竖捺折": {"take": [(0x31CB, "及", "右"), (0x31DC, "公", "下")]},
    "横捺撇钩": {"take": [(0x31D6, "买", "上")]},
    "竖折撇": {"take": [(0x31CB, "及", "右")]},
    "捺折提": {"take": [(0x31C6, "刁", "右")]},
    "竖捺撇": {"take": [(0x31CB, "及", "右"), (0x31D9, "以", "左")]},
    "横折捺折": {"take": [(0x31CB, "及", "右")]},
    "竖折撇钩": {"take": [(0x31DF, "儿", "右"), (0x31C9, "亏", "下")]},
    "捺撇钩": {"take": [(0x31C6, "刁", "右")]},
    "横撇折提": {"fuse": "乙"},
    "捺折撇钩": {"take": [(0x31C6, "刁", "右")]},
    "撇捺折": {"take": [(0x31DC, "公", "下")]},
    "捺撇折钩": {"take": [(0x31CC, "郎", "右")]},
    "撇折撇": {"take": [(0x31CC, "郎", "右")]},
    "横折撇钩": {"take": [(0x31CC, "郎", "右")]},
    "捺折折": {"take": [(0x31DC, "公", "下")]},
    "提折折折": {"fuse": "几"},
    "横撇捺钩": {"take": [(0x31CC, "郎", "右")]},
    "捺折捺": {"take": [(0x31D1, "旧", "左")]},
    "提折钩": {"compose": [(0x31D1, "旧", "左"), (0x31C6, "刁", "右")]},
}


IDS_OPS2 = "⿰⿱⿴⿵⿶⿷⿸⿹⿺⿻"
IDS_OPS3 = "⿲⿳"

# U+31C0..31E5 笔画区（Unicode 官方命名，中文名按缩写还原）
CJK_STROKE_NAMES = {
    0x31C0: "提", 0x31C1: "弯钩", 0x31C2: "斜钩", 0x31C3: "扁斜钩", 0x31C4: "竖弯",
    0x31C5: "横折折", 0x31C6: "横折钩", 0x31C7: "横撇", 0x31C8: "横折弯钩",
    0x31C9: "竖折弯钩", 0x31CA: "横折提", 0x31CB: "横折折撇", 0x31CC: "横撇弯钩",
    0x31CD: "横折弯", 0x31CE: "横折折折", 0x31CF: "捺", 0x31D0: "横", 0x31D1: "竖",
    0x31D2: "撇", 0x31D3: "竖撇", 0x31D4: "点", 0x31D5: "横折", 0x31D6: "横钩",
    0x31D7: "竖折", 0x31D8: "竖弯左", 0x31D9: "竖提", 0x31DA: "竖钩", 0x31DB: "撇点",
    0x31DC: "撇折", 0x31DD: "提捺", 0x31DE: "竖折折", 0x31DF: "竖弯钩",
    0x31E0: "横斜弯钩", 0x31E1: "横折折折钩", 0x31E2: "撇钩", 0x31E3: "圈",
    0x31E4: "横斜钩", 0x31E5: "竖折撇",
}
CJK_STROKE_ABBR = {
    0x31C0: "T", 0x31C1: "WG", 0x31C2: "XG", 0x31C3: "BXG", 0x31C4: "SW",
    0x31C5: "HZZ", 0x31C6: "HZG", 0x31C7: "HP", 0x31C8: "HZWG", 0x31C9: "SZWG",
    0x31CA: "HZT", 0x31CB: "HZZP", 0x31CC: "HPWG", 0x31CD: "HZW", 0x31CE: "HZZZ",
    0x31CF: "N", 0x31D0: "H", 0x31D1: "S", 0x31D2: "P", 0x31D3: "SP", 0x31D4: "D",
    0x31D5: "HZ", 0x31D6: "HG", 0x31D7: "SZ", 0x31D8: "SWZ", 0x31D9: "ST",
    0x31DA: "SG", 0x31DB: "PD", 0x31DC: "PZ", 0x31DD: "TN", 0x31DE: "SZZ",
    0x31DF: "SWG", 0x31E0: "HXWG", 0x31E1: "HZZZG", 0x31E2: "PG", 0x31E3: "Q",
    0x31E4: "HXG", 0x31E5: "SZP",
}

_ALIAS = {"竖折钩": "竖弯钩", "撇提": "撇折", "横折捺": "横斜钩",
          "横折折钩": "横折弯钩", "点钩": "弯钩", "提钩": "弯钩",
          "横撇折钩": "横撇弯钩", "撇捺": "撇点"}


def _polyLen(sec):
    return sum(dist(sec[i], sec[i + 1]) for i in range(len(sec) - 1))


def _netAngle(sec):
    return math.degrees(math.atan2(sec[-1][1] - sec[0][1], sec[-1][0] - sec[0][0]))


def _elemOfAngle(ang):
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


def classifyMedian(median):
    """中轴线 → 笔画名：重采样 → 拐角分段(窗口化切向差) → 丢楷体起笔顿笔 →
    判钩 → 每段净方向 → 折/钩组合命名。y 向上坐标系。"""
    pts = resamplePolyline([tuple(p) for p in median], 15)
    total = _polyLen(pts)
    if total < 1e-6 or len(pts) < 2:
        return "点"
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

    merged = []
    for s in sections:
        if merged and _polyLen(s) < max(22.0, total * 0.06):
            merged[-1] = merged[-1] + s[1:]
        else:
            merged.append(s)
    sections = merged
    if len(sections) >= 2 and _polyLen(sections[0]) < max(55.0, total * 0.13):
        sections = sections[1:]

    # 拐角过渡段吸收：折笔拐角在楷体中轴线上常留一小段斜向过渡（约
    # 40-70 单位），缩小的部件里超出上面的合并阈值后会独立成段，产生
    # 虚假中间元素（口部横折→横捺折并套错㇅模板）。方向介于前后段
    # 转向弧内的短中间段，按中点拆给两侧。
    changed = True
    while changed and len(sections) >= 3:
        changed = False
        for i in range(1, len(sections) - 1):
            sec = sections[i]
            if _polyLen(sec) >= max(70.0, total * 0.14):
                continue
            aPrev = _netAngle(sections[i - 1])
            aNext = _netAngle(sections[i + 1])
            turn = angDiff(aNext, aPrev)
            prog = angDiff(_netAngle(sec), aPrev)
            if abs(turn) < 55 or turn * prog <= 0 or abs(prog) >= abs(turn):
                continue
            half = max(1, len(sec) // 2)
            sections[i - 1] = sections[i - 1] + sec[1:half + 1]
            sections[i + 1] = sec[half:] + sections[i + 1][1:]
            del sections[i]
            changed = True
            break

    hook = False
    if len(sections) >= 2:
        lastLen = _polyLen(sections[-1])
        if lastLen < max(70.0, total * 0.24):
            ang = _netAngle(sections[-1])
            if ang > 95 or ang < -155 \
               or (70 < ang <= 95 and lastLen < max(80.0, total * 0.15)) \
               or (60 < ang <= 70 and lastLen < 60) \
               or (-155 <= ang < -95 and lastLen < max(60.0, total * 0.2)):
                # 最后一种：短尾段朝左下 —— 横钩/横折钩的钩常指向左下方，
                # 不能与真正的长撇段（横撇）混淆，故长度阈更紧
                hook = True
                sections = sections[:-1]

    elems = []
    for s in sections:
        e = _elemOfAngle(_netAngle(s))
        if e == "捺" and len(sections) == 1 and len(s) >= 5 and \
                dist(s[0], s[-1]) >= _polyLen(s) * 0.95:
            # 单段近直笔画没有走上面的"丢首段顿笔"逻辑：判成捺时裁掉首
            # 20% 弧长复核——顿笔会把小尺寸口部的竖拉过 -65° 边界（串）。
            # 只做捺→竖仲裁：平捺裁首会浅过 -20° 漂成横（处），弯曲段
            # （心的卧钩碗底）裁首方向也会漂，都不能重判。
            if _elemOfAngle(_netAngle(s[max(1, int(len(s) * 0.2)):])) == "竖":
                e = "竖"
        if not elems or elems[-1] != e:
            elems.append(e)
    if not elems:
        return "点"
    if len(elems) == 1:
        name = elems[0]
        if name in ("捺", "竖", "撇", "提") and total < 160:
            name = "点"
        elif name == "横" and total < 80:
            name = "点"
    else:
        parts = [elems[0]]
        for e in elems[1:]:
            parts.append("折" if e in ("横", "竖") else e)
        name = "".join(parts)
    if hook:
        if len(elems) == 1 and elems[0] == "捺":
            name = "卧钩" if _netAngle(pts) > -42 else "斜钩"
        else:
            name += "钩"
    return _ALIAS.get(name, name)


def typeOfStroke(ch, idx, medians):
    if len(medians) == 1 and ch in SINGLE_STROKE_TYPES:
        return SINGLE_STROKE_TYPES[ch]
    return classifyMedian(medians[idx])


def skeletonName(name):
    """笔画名骨架化：首元素 + 中间元素一律视作折 + 钩后缀（宽容匹配用）。"""
    hook = name.endswith("钩")
    core = name[:-1] if hook else name
    if not core:
        return name
    s = core[0] + "折" * (len(core) - 1)
    return s + ("钩" if hook else "")


def matchTier(classified, tableType):
    """1=完全一致 2=骨架一致 0=不匹配。"""
    if classified == tableType:
        return 1
    if skeletonName(classified) == skeletonName(tableType):
        return 2
    return 0


def findLibEntry(lib, t):
    """先精确键，再骨架宽容键。"""
    if t in lib:
        return lib[t]
    sk = skeletonName(t)
    for key in lib:
        if skeletonName(key) == sk:
            return lib[key]
    return None
