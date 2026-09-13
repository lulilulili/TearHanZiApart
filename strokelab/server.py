# -*- coding: utf-8 -*-
"""strokelab.server — 本地算法服务 + 可视化前端静态托管。

    python -m strokelab.server --root <数据根目录> [--port 8632] [--open]

API：
    GET /api/fonts                     → 字体清单
    GET /api/library?font=<file>       → A库 + B库（含来源/骨架）
    GET /api/decompose?font=<f>&char=<c> → 完整拆解结果（含楷体数据/结构/校验）
    GET /api/family?comp=<c>&kind=<k>  → 同族字（kind ∈ phonetic|semantic|radical）
    GET /api/radicals                  → 部首语义 registry（data/radicals.json 缓存）
    GET /api/search?radical=<r>&structure=<s> → 检索（两参至少给一；radical 含
                                         同源位形等价，structure ∈ IDS首算子|独体）
    GET /api/dict?ch=<c>               → 字典完整条目 + hasGlyph/strokeCount
前端：/ → viewer/charStrokeLab.html

并发模型：ThreadingHTTPServer（每请求一线程），因此吞吐三件套全部用真锁：
    1. /api/decompose 按 (字体,字) key 级互斥——首到者拆解，后到者等锁醒来
       直接命中缓存（等价 Future 语义），并发重复请求只算一次；
    2. /api/library 与拆解结果缓存的是**序列化后的 bytes**（大响应 500KB+，
       此前每次命中都重新 json.dumps），键含 字体文件签名+算法签名，LRU 限
       RESP_CACHE_MAX 条防内存膨胀；
    3. 字体建库为字体级锁 + double-check，不同字体互不阻塞；B库骨架 dirty
       回写的 临时文件+os.replace 原子替换已下沉 fonthub._saveLibCache
       （_atomicWriteJson），本层只保单写者与缓存失效（见 _flushSkeletons）。
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .datahub import DataHub, DEFAULT_CHIPS
from .fonthub import FontEntry, _algoSignature
from .pipeline import runPipeline

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.normpath(os.path.join(PKG_DIR, "..", "viewer"))

# 序列化 bytes 缓存上限：单条拆解 ~25KB、library ~550KB，512 条量级封顶
# 约 10~30MB，防长跑进程内存膨胀
RESP_CACHE_MAX = 512

_state = {"hub": None, "root": ".", "fonts": {}, "lock": threading.Lock(),
          "radicals": {"radicals": []}, "radVariants": {},
          "fontLocks": {},      # 字体名 → 建库/骨架落盘锁（字体间互不阻塞）
          "fontLibVer": {},     # 字体名 → 库内容版本号（骨架懒算回写时 +1）
          "inflight": {},       # decompose 缓存键 → key级锁（仅在算时存在）
          "respCache": OrderedDict(),   # 缓存键 → 响应 bytes（LRU）
          "decomposeRuns": 0}   # 实际执行 runPipeline 的次数（并发去重打点）


def _loadRadicals():
    """启动时读一次 data/radicals.json 进缓存（tools/build_radicals.py 产物）；
    文件缺失/损坏回空 registry。顺带建 同源位形 等价表：任一变体 → 全组集合，
    供 /api/search 的 radical 参数展开（如 氵 ↔ 水/氺）。"""
    path = os.path.join(_state["root"], "data", "radicals.json")
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
    except Exception:
        reg = {"radicals": []}
    variants = {}
    for e in reg.get("radicals", []):
        grp = e.get("variants") or [e.get("radical")]
        for v in grp:
            variants.setdefault(v, set()).update(grp)
    _state["radicals"] = reg
    _state["radVariants"] = variants


def _fontsDir():
    return os.path.join(_state["root"], "Fonts")


def _listFonts():
    d = _fontsDir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith((".ttf", ".otf", ".ttc")))


def _fontFileSig(name):
    """字体文件签名（大小-mtime，与 FontEntry._fontSig 同构）。直接 stat 文件、
    不加载字体——缓存命中路径零建库成本；文件被替换时签名变化 → 旧缓存键
    自然失效（旧条目由 LRU 淘汰）。未知字体 → KeyError（接口层回 400）。"""
    if name not in _listFonts():
        raise KeyError("未知字体: " + name)
    try:
        st = os.stat(os.path.join(_fontsDir(), name))
        return "%d-%d" % (st.st_size, int(st.st_mtime))
    except OSError:
        return "?"


def _fontLock(name):
    """字体级锁（建库、骨架缓存落盘共用）：不同字体互不阻塞。"""
    with _state["lock"]:
        return _state["fontLocks"].setdefault(name, threading.Lock())


def _getFont(name):
    """FontEntry 单例获取：字体级锁 + double-check。此前建库持全局锁——
    建字体 A 的 40s 会把字体 B 的请求全堵死；改为每字体一把锁，全局锁只
    护 dict 存取的短临界区。"""
    if name not in _listFonts():
        raise KeyError("未知字体: " + name)
    with _state["lock"]:
        fe = _state["fonts"].get(name)
    if fe is not None:
        return fe
    with _fontLock(name):
        with _state["lock"]:
            fe = _state["fonts"].get(name)
        if fe is None:          # double-check：排队等锁期间他人已建好
            fe = FontEntry(os.path.join(_fontsDir(), name))
            fe.buildLibraryB(_state["hub"])
            added = fe.completeLibraryB(_state["hub"])
            if added:
                print("  %s 自举补全 %d 类: %s" % (name, len(added), " ".join(added)))
            with _state["lock"]:
                _state["fonts"][name] = fe
    return fe


# ---------------------------------------------------------------- 响应 bytes LRU
def _respCacheGet(key):
    """LRU 命中 → bytes；未命中 → None。命中移到队尾维持最近使用序。"""
    with _state["lock"]:
        body = _state["respCache"].get(key)
        if body is not None:
            _state["respCache"].move_to_end(key)
        return body


def _respCachePut(key, body):
    with _state["lock"]:
        cache = _state["respCache"]
        cache[key] = body
        cache.move_to_end(key)
        while len(cache) > RESP_CACHE_MAX:
            cache.popitem(last=False)   # 淘汰最久未用


def _flushSkeletons(font, fe):
    """B库骨架 dirty 批量回写：单写者 + 库缓存失效。

    批量语义在 fonthub（_skelDirty 仅在拆解懒算出新骨架时置位，整字拆完
    只全量回写一次）；原子性已下沉 fonthub._saveLibCache（临时文件+
    os.replace，见 _atomicWriteJson）——上一轮"只改 server.py"约束下的
    伪 hub 重定向 workaround（写盘目标改到 .blibCache/_tmp/ 再替换）
    已撤销，直接调用即可。本层职责只剩两件：
      1) 单写者：持字体锁，同字体并发拆解不交叠触发同一缓存文件回写
         （dirty 标志 double-check，后到者空转返回）；
      2) 失效：有骨架更新时字体库版本号 +1——ensureSkeleton 会就地改
         libraryBAll 条目（skeleton/outlineBBox 字段），/api/library 的
         bytes 缓存须随之失效。"""
    if not getattr(fe, "_skelDirty", False):
        return
    with _fontLock(font):
        if not getattr(fe, "_skelDirty", False):    # double-check：他人已回写
            return
        fe.saveSkeletonsIfDirty(_state["hub"])
        with _state["lock"]:
            _state["fontLibVer"][font] = _state["fontLibVer"].get(font, 0) + 1


def _runDecompose(font, ch, cacheKey):
    """实际拆解 + 序列化 + 入缓存。调用方必须持有 cacheKey 的 inflight 锁。
    serverTimings 的赋值顺序与旧实现一致（拆解 计时含骨架落盘），保证响应
    JSON 字段序不变。"""
    tL = time.perf_counter()
    fe = _getFont(font)
    tP = time.perf_counter()
    r = runPipeline(_state["hub"], fe, ch)
    _flushSkeletons(font, fe)
    r["serverTimings"] = {
        "库准备": round((tP - tL) * 1000),
        "拆解": round((time.perf_counter() - tP) * 1000)}
    body = json.dumps(r, ensure_ascii=False).encode("utf-8")
    _respCachePut(cacheKey, body)
    with _state["lock"]:
        _state["decomposeRuns"] += 1
        n = _state["decomposeRuns"]
    # 打点：每次**实际计算**打一行（缓存命中/等锁命中不打）——并发去重
    # 验证即数这行的条数
    print("[decompose#%d] %s %s %dB" % (n, font, ch, len(body)))
    return body


def _decomposeBody(font, ch):
    """/api/decompose 主体：bytes LRU + key 级锁去重。

    缓存键含 字体文件签名+算法签名：字体文件替换或核心源码变动自动失效。
    并发重复请求（同 字体+字）只算一次：首到者在 _state["inflight"] 登记
    key 锁并实际拆解，后到者阻塞在同一把锁上，醒来后 double-check 缓存直接
    命中——即 Future 语义（threading.Lock 版，不引额外依赖）。拆解抛异常时
    不缓存（与旧实现一致），inflight 条目在 finally 清掉；极端时序下（首到者
    失败瞬间又来新请求）可能重算一次，无正确性影响。"""
    cacheKey = ("decompose", font, ch, _fontFileSig(font), _algoSignature())
    body = _respCacheGet(cacheKey)
    if body is not None:
        return body
    with _state["lock"]:
        keyLock = _state["inflight"].setdefault(cacheKey, threading.Lock())
    try:
        with keyLock:
            body = _respCacheGet(cacheKey)  # double-check：等锁期间首到者已算完
            if body is None:
                body = _runDecompose(font, ch, cacheKey)
    finally:
        with _state["lock"]:
            _state["inflight"].pop(cacheKey, None)
    return body


def _libraryBody(font):
    """/api/library 主体：序列化 bytes 缓存（响应 500KB+，此前每次命中都重新
    json.dumps 约 40ms）。缓存键 = 字体文件签名+算法签名+库内容版本号：
    前两者管磁盘文件/源码变化，版本号管进程内变异——拆解可能懒算骨架并就地
    改 libraryBAll 条目（_flushSkeletons 时 +1），保证命中字节与实时序列化
    一致。JSON 结构与字段序不变；serverTimings 随首次序列化定格（重复请求
    字节级一致，正是缓存语义）。"""
    with _state["lock"]:
        ver = _state["fontLibVer"].get(font, 0)
    cacheKey = ("library", font, _fontFileSig(font), _algoSignature(), ver)
    body = _respCacheGet(cacheKey)
    if body is not None:
        return body
    tL = time.perf_counter()
    fe = _getFont(font)
    obj = {"A": _state["hub"].libraryA, "B": fe.libraryBAll,
           "Btypes": sorted(fe.libraryB.keys()),
           "serverTimings": {
               "库准备": round((time.perf_counter() - tL) * 1000)}}
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    _respCachePut(cacheKey, body)
    return body


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _jsonBytes(self, body, code=200):
        """已序列化 JSON bytes 直发（缓存命中零序列化）；头与 _json 完全一致。"""
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._jsonBytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), code)

    def _static(self, relPath):
        path = os.path.normpath(os.path.join(VIEWER_DIR, relPath))
        if not path.startswith(VIEWER_DIR) or not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = "text/html; charset=utf-8" if path.endswith(".html") else \
            "application/javascript" if path.endswith(".js") else \
            "text/css" if path.endswith(".css") else "application/octet-stream"
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _apiSearch(self, qs):
        """/api/search?radical=&structure= — 两参至少给一。radical 经同源位形
        等价表展开后并集，structure 精确匹配首算子/独体；两参同给取交集。
        只回 graphicsIndex 有字形的字；sorted，上限 500 并附完整 total。"""
        radical = qs.get("radical", [""])[0]
        struct = qs.get("structure", [""])[0]
        if not radical and not struct:
            raise KeyError("radical/structure 至少提供一个")
        if radical and len(radical) != 1:
            raise KeyError("参数 radical 需为单个字符")
        hub = _state["hub"]
        found = None
        if radical:
            rads = _state["radVariants"].get(radical, {radical})
            found = hub.charsByRadical(sorted(rads))
        if struct:
            byStruct = hub.charsByStructure(struct)
            found = byStruct if found is None else (found & byStruct)
        chars = sorted(found)
        self._json({"chars": "".join(chars[:500]),
                    "count": len(chars[:500]), "total": len(chars)})

    def _apiDict(self, qs):
        """/api/dict?ch=清 — dictionary 完整条目 + hasGlyph/strokeCount（楷体
        笔数=medians 数）。非单字 400；字典无此字 404。"""
        ch = qs.get("ch", [""])[0]
        if len(ch) != 1:
            raise KeyError("参数 ch 需为单个字符")
        entry = _state["hub"].dictEntry(ch)
        if not entry:
            self._json({"error": "字典无此字: " + ch}, 404)
            return
        out = dict(entry)
        g = _state["hub"].geom(ch)
        out["hasGlyph"] = bool(g)
        out["strokeCount"] = len(g["medians"]) if g else 0
        self._json(out)

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            route = parsed.path
            if route == "/api/fonts":
                self._json({"fonts": _listFonts(), "chips": DEFAULT_CHIPS})
            elif route == "/api/library":
                self._jsonBytes(_libraryBody(qs["font"][0]))
            elif route == "/api/decompose":
                self._jsonBytes(_decomposeBody(qs["font"][0], qs["char"][0]))
            elif route == "/api/family":
                comp = qs["comp"][0]
                kind = qs.get("kind", ["phonetic"])[0]
                self._json({"comp": comp, "kind": kind,
                            "chars": _state["hub"].familyChars(comp, kind)})
            elif route == "/api/radicals":
                self._json(_state["radicals"])
            elif route == "/api/search":
                self._apiSearch(qs)
            elif route == "/api/dict":
                self._apiDict(qs)
            elif route == "/" or route == "/index.html":
                self.send_response(302)
                self.send_header("Location", "/viewer/charStrokeLab.html")
                self.end_headers()
            elif route.startswith("/viewer/"):
                self._static(route[len("/viewer/"):])
            else:
                self.send_error(404)
        except KeyError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": repr(e)}, 500)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".",
                    help="数据根目录（含 Fonts/ makemeahanzi-master/ hanzi_chaizi-master/）")
    ap.add_argument("--port", type=int, default=0, help="端口（默认自动选择）")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = ap.parse_args(argv)

    _state["root"] = os.path.abspath(args.root)
    print("加载 makemeahanzi 数据 ...")
    _state["hub"] = DataHub(_state["root"])
    _loadRadicals()
    print("A库 %d 类；字体 %d 个；部首registry %d 条"
          % (len(_state["hub"].libraryA), len(_listFonts()),
             len(_state["radicals"].get("radicals", []))))

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://localhost:%d/viewer/charStrokeLab.html" % srv.server_address[1]
    print("CharStrokeLab: " + url)
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
