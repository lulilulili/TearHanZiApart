# -*- coding: utf-8 -*-
"""strokelab.datahub — makemeahanzi / hanzi_chaizi 数据中心 + A库（文鼎楷体）。"""

import json
import os

from .classify import (PROBE_TABLE, TYPE_ORDER, SINGLE_STROKE_TYPES,
                       IDS_OPS2, IDS_OPS3, typeOfStroke, matchTier, findLibEntry)

DEFAULT_CHIPS = list("十口头木中大天日水永汉字国你好我爱")

# 楷体类型修正表：部件一致性审计（verify --audit-kai）的多数派裁决，
# 修 classifyMedian 在个别字上的误判（魂3竖折→撇折）。生成：审计后由
# kaiAudit.json 转换，随包提交。
_FIXES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "kaiTypeFixes.json")
# 全量套用会把平撇这类名义撇/几何横的笔改成几何共识标签，反而在
# 轴向校验里制造争议（抽样 80.8%→78.6%）；仅保留**复合笔形修正**
# （骨架不同的结构性误判，如竖折→撇折、横捺撇→横折钩——这才是清单
# 长尾噪声的来源），单元素笔形（横竖撇捺点提互换）不动。
try:
    with open(_FIXES_PATH, encoding="utf-8") as _f:
        _raw = json.load(_f)
    _SINGLE = set("横竖撇捺点提")
    KAI_TYPE_FIXES = {}
    for _ch, _m in _raw.items():
        # 单元素互换整体不套用（几何共识标签在轴向校验里制造争议），
        # 但**降级为点**放行：点不参与 TYPE 轴向校验，只减误报不增
        # （糹3 竖→点：弦长163-176 被 classifyMedian 按长度判竖，
        # 审计 12/17 票裁定真身是点）
        _keep = {_i: _t for _i, _t in _m.items()
                 if (_t not in _SINGLE) or _t == "点"}
        if _keep:
            KAI_TYPE_FIXES[_ch] = _keep
except Exception:
    KAI_TYPE_FIXES = {}

# 省形/异体别名表（种子字统计 seedStats 聚合）：部件在字内的实际笔数
# 与其种子字条目稳定不一致的规则（艹4/3、尚→⺌、攸省笔…）。COMP 配额
# 校验与后续 C库部件模板按此表修正种子配额，把"结构同构"前提外的
# 248 条字例拉回前提内。
_ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "componentAliases.json")
try:
    with open(_ALIAS_PATH, encoding="utf-8") as _f:
        _a = json.load(_f)
    COMPONENT_ALIASES = _a.get("componentRules", {})
    COMPONENT_CHAR_EXCEPTIONS = _a.get("charExceptions", {})
except Exception:
    COMPONENT_ALIASES = {}
    COMPONENT_CHAR_EXCEPTIONS = {}


class DataHub:
    def __init__(self, root):
        self.root = root
        self.graphicsIndex = {}
        self.dictIndex = {}
        self.chaiziJt = {}
        self.chaiziFt = {}
        self._kaiCache = {}
        self._geomCache = {}
        self._dictCache = {}
        self.libraryA = None
        self._load()

    # ------------------------------------------------------------ 加载
    def _indexByChar(self, path, target):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if len(line) < 16:
                    continue
                target[line[14]] = line

    def _loadChaizi(self, path, target):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                cols = line.rstrip("\n").rstrip("\r").split("\t")
                if len(cols) >= 2:
                    target[cols[0]] = [c.split() for c in cols[1:]]

    def _load(self):
        mmh = os.path.join(self.root, "makemeahanzi-master")
        self._indexByChar(os.path.join(mmh, "graphics.txt"), self.graphicsIndex)
        self._indexByChar(os.path.join(mmh, "dictionary.txt"), self.dictIndex)
        cz = os.path.join(self.root, "hanzi_chaizi-master", "raw_data")
        self._loadChaizi(os.path.join(cz, "chaizi-jt.txt"), self.chaiziJt)
        self._loadChaizi(os.path.join(cz, "chaizi-ft.txt"), self.chaiziFt)
        self.buildLibraryA()

    # ------------------------------------------------------------ 查询
    def hasKai(self, ch):
        return ch in self.graphicsIndex

    def geom(self, ch):
        if ch not in self._geomCache:
            line = self.graphicsIndex.get(ch)
            self._geomCache[ch] = json.loads(line) if line else None
        return self._geomCache[ch]

    def dictEntry(self, ch):
        if ch not in self._dictCache:
            line = self.dictIndex.get(ch)
            self._dictCache[ch] = json.loads(line) if line else None
        return self._dictCache[ch]

    def components(self, ch, depth=2):
        """结构分解的部件清单（≤depth 级，去重保序）。
        → [{"char": 部件, "level": 1|2}, ...]；'？' 占位符跳过。"""
        IDS = set("⿰⿱⿲⿳⿴⿵⿶⿷⿸⿹⿺⿻")
        out = []
        seenChars = set()

        def rec(c, level):
            if level > depth:
                return
            e = self.dictEntry(c)
            if not e:
                return
            for comp in (e.get("decomposition") or ""):
                if comp in IDS or comp == "？":
                    continue
                if comp not in seenChars:
                    seenChars.add(comp)
                    out.append({"char": comp, "level": level})
                rec(comp, level + 1)

        rec(ch, 1)
        return out

    # ------------------------------------------------------------ 结构
    def buildStructureTree(self, ch, depth=0, seen=None):
        if seen is None:
            seen = set()
        node = {"char": ch}
        if depth >= 4 or ch in seen:
            return node
        entry = self.dictEntry(ch)
        if not entry:
            return node
        seen = seen | {ch}
        ids = list(entry.get("decomposition") or "")
        if not ids or ids[0] not in (IDS_OPS2 + IDS_OPS3):
            return node
        pos = [0]

        def parse():
            c = ids[pos[0]]
            pos[0] += 1
            if c in IDS_OPS2:
                return {"op": c, "children": [parse(), parse()]}
            if c in IDS_OPS3:
                return {"op": c, "children": [parse(), parse(), parse()]}
            return {"leaf": c}

        try:
            tree = parse()
        except IndexError:
            return node

        def attach(t, d):
            if "leaf" in t:
                if t["leaf"] == "？":
                    return {"char": "？"}
                return self.buildStructureTree(t["leaf"], d, seen)
            return {"op": t["op"], "children": [attach(x, d + 1) for x in t["children"]]}

        sub = attach(tree, depth + 1)
        if "op" in sub:
            node["op"] = sub["op"]
            node["children"] = sub["children"]
        return node

    def deepenMatches(self, ch, depth=0):
        """matches 只标第一层部件；递归用部件自身 matches 细化层级。"""
        entry = self.dictEntry(ch)
        g = self.geom(ch)
        if not entry or not g or not entry.get("matches"):
            return None
        result = [list(p) if p else None for p in entry["matches"]]
        if depth >= 3:
            return result
        tree = self.buildStructureTree(ch)
        children = tree.get("children") or []
        byComp = {}
        for si, p in enumerate(entry["matches"]):
            if p:
                byComp.setdefault(p[0], []).append(si)
        for ci, strokeIdxs in byComp.items():
            if ci >= len(children):
                continue
            compChar = children[ci].get("char")
            if not compChar or compChar == "？":
                continue
            sub = self.deepenMatches(compChar, depth + 1)
            if sub is None or len(sub) != len(strokeIdxs):
                continue
            for j, si in enumerate(strokeIdxs):
                if sub[j]:
                    result[si] = [ci] + sub[j]
        return result

    def kai(self, ch):
        if ch in self._kaiCache:
            return self._kaiCache[ch]
        g = self.geom(ch)
        if not g:
            self._kaiCache[ch] = None
            return None
        entry = self.dictEntry(ch) or {}
        medians = g["medians"]
        strokeTypes = [typeOfStroke(ch, i, medians) for i in range(len(medians))]
        fixes = KAI_TYPE_FIXES.get(ch)
        if fixes:
            for idx, t in fixes.items():
                i = int(idx)
                if 0 <= i < len(strokeTypes):
                    strokeTypes[i] = t
        data = {
            "strokes": g["strokes"],
            "medians": medians,
            "strokeTypes": strokeTypes,
            "radical": entry.get("radical", ""),
            "decomposition": entry.get("decomposition", ""),
            "matches": self.deepenMatches(ch) or entry.get("matches", []),
            "structure": self.buildStructureTree(ch),
            "chaiziJt": self.chaiziJt.get(ch, []),
            "chaiziFt": self.chaiziFt.get(ch, []),
            "components": self.components(ch),
        }
        self._kaiCache[ch] = data
        return data

    # ------------------------------------------------------------ A 库
    def buildLibraryA(self):
        lib = {}
        for ch in "一丨丶丿乙亅":
            g = self.geom(ch)
            if not g or len(g["strokes"]) != 1:
                continue
            t = SINGLE_STROKE_TYPES[ch]
            lib.setdefault(t, []).append({
                "type": t, "path": g["strokes"][0], "median": g["medians"][0],
                "tier": 1, "source": "整字「%s」(U+%04X)" % (ch, ord(ch)),
                "kind": "wholeChar",
            })
        for t in TYPE_ORDER:
            for ch in PROBE_TABLE[t]:
                g = self.geom(ch)
                if not g:
                    continue
                for idx, m in enumerate(g["medians"]):
                    tier = matchTier(typeOfStroke(ch, idx, g["medians"]), t)
                    if not tier:
                        continue
                    lib.setdefault(t, []).append({
                        "type": t, "path": g["strokes"][idx], "median": m,
                        "tier": tier, "source": "「%s」第%d笔" % (ch, idx + 1),
                        "kind": "charStroke",
                    })
            if t in lib:
                lib[t].sort(key=lambda e: e["tier"])
                lib[t] = lib[t][:4]
        self.libraryA = lib

    def libraryAFor(self, ch):
        g = self.geom(ch)
        extra = {}
        if g:
            for idx, m in enumerate(g["medians"]):
                t = typeOfStroke(ch, idx, g["medians"])
                if t not in self.libraryA and t not in extra:
                    extra[t] = [{"type": t, "path": g["strokes"][idx], "median": m,
                                 "tier": 2, "source": "「%s」第%d笔" % (ch, idx + 1),
                                 "kind": "charStroke"}]
        merged = dict(self.libraryA)
        merged.update(extra)
        return merged
