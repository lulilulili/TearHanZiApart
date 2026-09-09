# -*- coding: utf-8 -*-
"""strokelab.server — 本地算法服务 + 可视化前端静态托管。

    python -m strokelab.server --root <数据根目录> [--port 8632] [--open]

API：
    GET /api/fonts                     → 字体清单
    GET /api/library?font=<file>       → A库 + B库（含来源/骨架）
    GET /api/decompose?font=<f>&char=<c> → 完整拆解结果（含楷体数据/结构/校验）
前端：/ → viewer/charStrokeLab.html
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .datahub import DataHub, DEFAULT_CHIPS
from .fonthub import FontEntry
from .pipeline import runPipeline

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.normpath(os.path.join(PKG_DIR, "..", "viewer"))

_state = {"hub": None, "root": ".", "fonts": {}, "results": {}, "lock": threading.Lock()}


def _fontsDir():
    return os.path.join(_state["root"], "Fonts")


def _listFonts():
    d = _fontsDir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith((".ttf", ".otf", ".ttc")))


def _getFont(name):
    if name not in _listFonts():
        raise KeyError("未知字体: " + name)
    with _state["lock"]:
        if name not in _state["fonts"]:
            fe = FontEntry(os.path.join(_fontsDir(), name))
            fe.buildLibraryB(_state["hub"])
            added = fe.completeLibraryB(_state["hub"])
            if added:
                print("  %s 自举补全 %d 类: %s" % (name, len(added), " ".join(added)))
            _state["fonts"][name] = fe
    return _state["fonts"][name]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            route = parsed.path
            if route == "/api/fonts":
                self._json({"fonts": _listFonts(), "chips": DEFAULT_CHIPS})
            elif route == "/api/library":
                fe = _getFont(qs["font"][0])
                self._json({"A": _state["hub"].libraryA, "B": fe.libraryBAll,
                            "Btypes": sorted(fe.libraryB.keys())})
            elif route == "/api/decompose":
                font = qs["font"][0]
                ch = qs["char"][0]
                key = (font, ch)
                if key not in _state["results"]:
                    tL = time.perf_counter()
                    fe = _getFont(font)
                    tP = time.perf_counter()
                    r = runPipeline(_state["hub"], fe, ch)
                    r["serverTimings"] = {
                        "库准备": round((tP - tL) * 1000),
                        "拆解": round((time.perf_counter() - tP) * 1000)}
                    _state["results"][key] = r
                self._json(_state["results"][key])
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
    print("A库 %d 类；字体 %d 个" % (len(_state["hub"].libraryA), len(_listFonts())))

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
