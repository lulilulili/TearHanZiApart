#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/run_full8.py — 8 字体全量校验驱动(脱离进程,跨会话存活)。

设计(2026-09-27 用户批次):
- 起跑前等待:轮询至 strokelab/ 工作区干净(形态修复器收官提交完)且
  python 进程安静,两次稳定确认;超时 5h 则带状态起跑(记录 rev);
- 逐字体串行跑 verify_batch --preset full,每字体独立 run 目录;
- 断点续跑:每字体完成写 verifyOut/full8/done-<字体>.json 标记,
  重启驱动自动跳过已完成字体;
- 收尾聚合 verifyOut/full8/summary.json(逐字体通过率+失败码分布+rev)。
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "verifyOut", "full8")
FONTS = [
    "HarmonyOS_Sans_SC.ttf", "msyh.ttc",
    "NotoSansSC-VariableFont_wght.ttf", "simfang.ttf",
    "simhei.ttf", "simkai.ttf", "simsun.ttc", "浪漫雅圆GB.ttf",
]
WAIT_MAX_S = 5 * 3600
POLL_S = 300


def log(msg):
    line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)


def gitRev():
    try:
        return subprocess.run(["git", "log", "-1", "--format=%h"], cwd=ROOT,
                              capture_output=True, text=True,
                              encoding="utf-8").stdout.strip()
    except Exception:
        return "?"


def strokelabDirty():
    try:
        out = subprocess.run(["git", "status", "--porcelain", "strokelab"],
                             cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8").stdout.strip()
        return bool(out)
    except Exception:
        return False


def pythonCount():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="ignore").stdout
        # 除去本驱动自身
        return max(0, out.count("python.exe") - 1)
    except Exception:
        return 0


def waitForQuiet():
    """等修复器收官:strokelab/ 干净 + 进程安静,连续两次(隔5分钟)。"""
    t0 = time.time()
    stable = 0
    while time.time() - t0 < WAIT_MAX_S:
        dirty = strokelabDirty()
        procs = pythonCount()
        if not dirty and procs <= 1:
            stable += 1
            log("安静确认 %d/2 (dirty=%s procs=%d)" % (stable, dirty, procs))
            if stable >= 2:
                return True
        else:
            stable = 0
            log("等待中 dirty=%s procs=%d" % (dirty, procs))
        time.sleep(POLL_S)
    log("等待超时,带当前状态起跑")
    return False


def runFont(font):
    marker = os.path.join(OUT, "done-%s.json" % font.replace(".", "_"))
    if os.path.exists(marker):
        log("跳过(已完成): " + font)
        return json.load(open(marker, encoding="utf-8"))
    log("起跑: " + font)
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join("tools", "verify_batch.py"),
         "--preset", "full", "--fonts", font, "--jobs", "7", "--no-resume"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    elapsed = time.time() - t0
    # 找它写的 run 目录(最新 full run)
    runsDir = os.path.join(ROOT, "verifyOut", "runs")
    cand = sorted((d for d in os.listdir(runsDir) if d.endswith("-full")),
                  key=lambda d: os.path.getmtime(os.path.join(runsDir, d)))
    runDir = cand[-1] if cand else None
    info = {"font": font, "elapsedS": round(elapsed), "rc": r.returncode,
            "runDir": runDir, "tail": (r.stdout or "")[-600:]}
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    log("完成 %s: rc=%d %.0f 分钟 → %s" % (font, r.returncode,
                                            elapsed / 60, runDir))
    return info


def aggregate(results):
    agg = {"rev": gitRev(), "finishedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
           "fonts": {}}
    for info in results:
        runDir = info.get("runDir")
        entry = {"elapsedS": info.get("elapsedS"), "runDir": runDir,
                 "fails": {}, "tested": 0, "passed": 0}
        if runDir:
            full = os.path.join(ROOT, "verifyOut", "runs", runDir)
            for fn in os.listdir(full):
                if not fn.endswith(".jsonl"):
                    continue
                for line in open(os.path.join(full, fn), encoding="utf-8"):
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("skip"):
                        continue
                    entry["tested"] += 1
                    fs = rec.get("fails") or []
                    if not fs:
                        entry["passed"] += 1
                    for fEnt in fs:
                        c = fEnt.get("code", "?")
                        entry["fails"][c] = entry["fails"].get(c, 0) + 1
        agg["fonts"][info["font"]] = entry
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1)
    log("聚合完成 → verifyOut/full8/summary.json")


def main():
    os.makedirs(OUT, exist_ok=True)
    log("驱动启动 rev=%s" % gitRev())
    waitForQuiet()
    log("起跑 rev=%s" % gitRev())
    results = [runFont(f) for f in FONTS]
    aggregate(results)
    log("全部完成")


if __name__ == "__main__":
    main()
