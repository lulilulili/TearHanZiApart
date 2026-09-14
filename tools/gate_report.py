#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/gate_report.py — 一键门禁套件:编排 bench/parity/verify_batch/trace_stats
并聚合为主会话可解析的单一 report.json。

用户场景:家里先上代码,公司好电脑跑数据,跑完把 report.json 交回主会话
解析(字段契约见 docs/批量验证运行器.md 的"一键门禁"节)。

    python -X utf8 tools/gate_report.py --suite quick   # bench+parity+sample
    python -X utf8 tools/gate_report.py                 # std: quick+cross+coverage
    python -X utf8 tools/gate_report.py --suite full    # std+全量+trace 胜率表
    python -X utf8 tools/gate_report.py --resume verifyOut/gateReport/<目录>

产物 verifyOut/gateReport/<时间戳>-<git短rev>/:
    report.json   机器可解析结果(每步完成即落盘,中断也有部分结果)
    report.md     人读版
    <step>.log    各步骤原始输出
    sample/ 等    各 verify_batch 步骤的独立 run 目录(jsonl 可断点续跑)

退出码:0=所有硬门 pass/skip;1=任一硬门 fail(含步骤异常);2=参数错误。
--resume 跳过已完成步;未完成的 verify 步复用同一 run 目录,借
verify_batch 既有的 jsonl 续跑机制接着跑。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONTRACT_VERSION = 1

SUITES = {
    "quick": ["bench", "parity", "sample"],
    "std": ["bench", "parity", "sample", "cross", "coverage"],
    "full": ["bench", "parity", "sample", "cross", "coverage", "full", "trace"],
}
VERIFY_STEPS = ("sample", "cross", "coverage", "full")

# 硬编码基线表(2026-09-14 裁定口径:42f54a8 barcap 罚默认开启。归档:
# sample=verifyOut/runs/2b-sample-on-final、cross=2b-cross-on-final、
# coverage=clib-on-coverage)。协议规定升门决策需两侧同意,改基线必须
# 在提交信息显式声明并同步 docs/批量验证运行器.md 的契约表。
BASELINES = {
    "bench": {"broken": 0},
    "parity": {"mismatch": 0},
    "sample": {"HarmonyOS_Sans_SC": {"passed": 932, "tested": 957, "rate": 97.39}},
    "cross": {
        "simhei": {"passed": 289, "tested": 319, "rate": 90.60},
        "NotoSansSC-VariableFont_wght": {"passed": 247, "tested": 320, "rate": 77.19},
        "simsun": {"passed": 278, "tested": 319, "rate": 87.15},
    },
    "coverage": {"HarmonyOS_Sans_SC": {"passed": 380, "tested": 385, "rate": 98.70}},
    # 全量是里程碑软门:95.5 为移交单#5 的外推预期值,不参与退出码
    "full": {"HarmonyOS_Sans_SC": {"expectRate": 95.5}},
}

CROSS_NOISE_CHARS = 2      # 协作验证协议:跨字体门允许 ±2 字噪声
FAIL_LIST_LIMIT = 5000     # failLists 每字体截断长度(全量失败字串防爆)
SOFT_KEYS = ("compQuota", "compOut", "orderX", "reclass")
ENV_FONTS = ("HarmonyOS_Sans_SC.ttf", "simhei.ttf",
             "NotoSansSC-VariableFont_wght.ttf", "simsun.ttc")
PARITY_BASE = Path("verifyOut") / "parity_base.json"

BENCH_LINE = re.compile(r"(\d+) 个用例; 破坏\(verified 回退/错误\): (\d+)")
PARITY_LINE = re.compile(r"(\d+) 例: (\d+) 不一致")


@dataclass
class GateCtx:
    root: Path
    gateDir: Path
    jobs: int
    report: dict


# ---------------------------------------------------------------- 环境与指纹

def runGit(root: Path, args: list) -> str:
    try:
        out = subprocess.check_output(["git"] + args, cwd=str(root),
                                      stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def gitInfo(root: Path) -> dict:
    return {
        "rev": runGit(root, ["rev-parse", "--short", "HEAD"]) or "unknown",
        "revFull": runGit(root, ["rev-parse", "HEAD"]) or "unknown",
        "dirty": bool(runGit(root, ["status", "--porcelain",
                                    "--untracked-files=no"])),
    }


def fontFingerprint(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtimeNs": st.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def buildEnv(root: Path) -> dict:
    # 复用 verify_batch 的算法源码指纹口径,两台机器可据此确认同版
    from tools.verify_batch import source_fingerprint
    env = source_fingerprint(root)
    env["fonts"] = {name: fontFingerprint(root / "Fonts" / name)
                    for name in ENV_FONTS if (root / "Fonts" / name).exists()}
    return env


# ---------------------------------------------------------------- 报告读写

def newReport(root: Path, gateDir: Path, suite: str) -> dict:
    info = gitInfo(root)
    return {
        "contractVersion": CONTRACT_VERSION,
        "rev": info["rev"], "revFull": info["revFull"], "dirty": info["dirty"],
        "createdAt": datetime.now().astimezone().isoformat(),
        "suite": suite,
        "gateDir": str(gateDir),
        "env": buildEnv(root),
        "steps": {},
        "gates": {},
        "baselines": dict(BASELINES, crossNoiseChars=CROSS_NOISE_CHARS),
        "verdicts": {},
        "failLists": {},
        "softMetrics": {},
    }


def saveReport(ctx: GateCtx) -> None:
    text = json.dumps(ctx.report, ensure_ascii=False, indent=1)
    (ctx.gateDir / "report.json").write_text(text, encoding="utf-8")


def setStep(ctx: GateCtx, name: str, status: str, info: dict = None) -> None:
    entry = dict(info or {})
    entry["status"] = status
    ctx.report["steps"][name] = entry


# ---------------------------------------------------------------- 步骤执行

def runLoggedCommand(cmd: list, logPath: Path, cwd: Path) -> int:
    """子进程执行,输出同时进控制台与 <step>.log(utf-8)。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    with open(logPath, "w", encoding="utf-8", errors="replace") as logFile:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        for rawLine in proc.stdout:
            line = rawLine.decode("utf-8", errors="replace")
            sys.stdout.write(line)
            sys.stdout.flush()
            logFile.write(line)
        proc.wait()
    return proc.returncode


def parseLogLast(logPath: Path, pattern) -> tuple:
    """取日志中最后一次匹配(bench/parity 的汇总行都在末尾)。"""
    text = logPath.read_text(encoding="utf-8", errors="replace")
    match = None
    for match in pattern.finditer(text):
        pass
    return match.groups() if match else None


def stepBench(ctx: GateCtx) -> None:
    logPath = ctx.gateDir / "bench.log"
    cmd = [sys.executable, "-X", "utf8", "-m", "strokelab.bench",
           "--root", str(ctx.root), "--check"]
    started = time.perf_counter()
    code = runLoggedCommand(cmd, logPath, ctx.root)
    groups = parseLogLast(logPath, BENCH_LINE)
    status = "error"
    if groups:
        ctx.report["gates"]["bench"] = {"cases": int(groups[0]),
                                        "broken": int(groups[1])}
        status = "done"          # broken>0 属 fail 判定,不是步骤错误
    setStep(ctx, "bench", status,
            {"exitCode": code, "log": "bench.log",
             "elapsedSeconds": round(time.perf_counter() - started, 1)})


def stepParity(ctx: GateCtx) -> None:
    basePath = ctx.root / PARITY_BASE
    if not basePath.exists():
        setStep(ctx, "parity", "skipped",
                {"note": "基线不存在: " + str(PARITY_BASE)})
        return
    logPath = ctx.gateDir / "parity.log"
    cmd = [sys.executable, "-X", "utf8",
           str(ctx.root / "tools" / "parity_check.py"),
           "compare", str(basePath)]
    started = time.perf_counter()
    code = runLoggedCommand(cmd, logPath, ctx.root)
    groups = parseLogLast(logPath, PARITY_LINE)
    status = "error"
    if groups:
        ctx.report["gates"]["parity"] = {"cases": int(groups[0]),
                                         "mismatch": int(groups[1])}
        status = "done"          # mismatch>0 属 fail 判定,不是步骤错误
    setStep(ctx, "parity", status,
            {"exitCode": code, "log": "parity.log",
             "elapsedSeconds": round(time.perf_counter() - started, 1)})


def stepVerify(ctx: GateCtx, name: str) -> None:
    """sample/cross/coverage/full 四步共用:preset 名=步骤名。"""
    runDir = ctx.gateDir / name
    logPath = ctx.gateDir / (name + ".log")
    cmd = [sys.executable, "-X", "utf8",
           str(ctx.root / "tools" / "verify_batch.py"),
           "--preset", name, "--out", str(runDir)]
    if ctx.jobs:
        cmd += ["--jobs", str(ctx.jobs)]
    started = time.perf_counter()
    code = runLoggedCommand(cmd, logPath, ctx.root)
    ok = code == 0 and (runDir / "manifest.json").exists()
    setStep(ctx, name, "done" if ok else "error",
            {"exitCode": code, "log": logPath.name, "runDir": str(runDir),
             "elapsedSeconds": round(time.perf_counter() - started, 1)})
    if ok:
        collectVerifyGate(ctx, name)


def stepTrace(ctx: GateCtx) -> None:
    runDir = ctx.gateDir / "trace"
    logPath = ctx.gateDir / "trace.log"
    cmd = [sys.executable, "-X", "utf8",
           str(ctx.root / "tools" / "trace_stats.py"), "--out", str(runDir)]
    if ctx.jobs:
        cmd += ["--jobs", str(ctx.jobs)]
    started = time.perf_counter()
    code = runLoggedCommand(cmd, logPath, ctx.root)
    ok = code == 0 and (runDir / "traceStats.md").exists()
    setStep(ctx, "trace", "done" if ok else "error",
            {"exitCode": code, "log": "trace.log", "runDir": str(runDir),
             "elapsedSeconds": round(time.perf_counter() - started, 1)})


def runStep(ctx: GateCtx, name: str) -> None:
    if name == "bench":
        stepBench(ctx)
    elif name == "parity":
        stepParity(ctx)
    elif name == "trace":
        stepTrace(ctx)
    else:
        stepVerify(ctx, name)


def stepDone(ctx: GateCtx, name: str) -> bool:
    """resume 判据:report 标记 done 且磁盘工件仍在(防半截目录)。"""
    entry = ctx.report["steps"].get(name) or {}
    if entry.get("status") == "skipped":
        return True
    if entry.get("status") != "done":
        return False
    if name in VERIFY_STEPS:
        runDir = ctx.gateDir / name
        return (runDir / "manifest.json").exists() and \
               (runDir / "summary.json").exists()
    if name == "trace":
        return (ctx.gateDir / "trace" / "traceStats.md").exists()
    return name in ctx.report["gates"]


# ---------------------------------------------------------------- 结果聚合

def collectVerifyGate(ctx: GateCtx, name: str) -> None:
    """从 run 目录的 summary.json/jsonl 聚合 gates/failLists/softMetrics。"""
    runDir = ctx.gateDir / name
    summary = json.loads((runDir / "summary.json").read_text(encoding="utf-8"))
    gate, failLists = {}, {}
    for fontName, s in summary.items():
        gate[fontName] = {"tested": s["tested"], "passed": s["passed"],
                          "rate": s["passRate"]}
        chars = "".join(sorted(s.get("failChars", {})))
        if len(chars) > FAIL_LIST_LIMIT:
            chars = chars[:FAIL_LIST_LIMIT] + "…(截断,共%d字)" % \
                len(s["failChars"])
        failLists[fontName] = chars
    ctx.report["gates"][name] = gate
    ctx.report["failLists"][name] = failLists
    ctx.report["softMetrics"][name] = aggregateSoftMetrics(runDir)


def aggregateSoftMetrics(runDir: Path) -> dict:
    """逐字体累加 verify m 字段的协议软指标(compQuota 应恒 0)。"""
    out = {}
    for jsonlPath in sorted(runDir.glob("*.jsonl")):
        agg = {k: 0 for k in SOFT_KEYS}
        tested = 0
        with open(jsonlPath, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("skip"):
                    continue
                tested += 1
                m = rec.get("m") or {}
                for k in SOFT_KEYS:
                    agg[k] += int(m.get(k, 0) or 0)
        agg["tested"] = tested
        out[jsonlPath.stem] = agg
    return out


# ---------------------------------------------------------------- 门判定

def evalCounterGate(report: dict, name: str, key: str) -> dict:
    step = report["steps"].get(name)
    if step is None:
        return {"verdict": "skip", "hard": True, "note": "套件未包含该步"}
    if step.get("status") == "skipped":
        return {"verdict": "skip", "hard": True, "note": step.get("note", "")}
    value = (report["gates"].get(name) or {}).get(key)
    if step.get("status") != "done" or value is None:
        return {"verdict": "fail", "hard": True,
                "note": "步骤未完成或输出不可解析"}
    return {"verdict": "pass" if value == 0 else "fail", "hard": True,
            "delta": value, "note": "%s=%d(基线 0)" % (key, value)}


def evalFontEntry(cur: dict, base: dict, noiseChars: int) -> dict:
    if not cur:
        return {"verdict": "fail", "note": "结果缺失"}
    entry = {"tested": cur["tested"], "passed": cur["passed"],
             "rate": cur["rate"], "baseline": base,
             "deltaPp": round(cur["rate"] - base["rate"], 2),
             "deltaPassed": None}
    if cur["tested"] == base["tested"]:
        entry["deltaPassed"] = cur["passed"] - base["passed"]
        ok = entry["deltaPassed"] >= -noiseChars
    else:
        # 字集/字体版本漂移时退化为通过率对比,字数噪声换算成百分点
        tol = noiseChars / cur["tested"] * 100 if cur["tested"] else 0.0
        ok = cur["rate"] >= base["rate"] - tol - 1e-9
        entry["note"] = "tested 与基线不一致(%d vs %d),按通过率判" % (
            cur["tested"], base["tested"])
    entry["verdict"] = "pass" if ok else "fail"
    return entry


def evalFontGate(report: dict, name: str, noiseChars: int) -> dict:
    step = report["steps"].get(name)
    if step is None:
        return {"verdict": "skip", "hard": True, "note": "套件未包含该步"}
    if step.get("status") != "done":
        return {"verdict": "fail", "hard": True, "note": "步骤未完成或异常退出"}
    data = report["gates"].get(name) or {}
    fonts, verdict = {}, "pass"
    for fontName, base in BASELINES[name].items():
        fonts[fontName] = evalFontEntry(data.get(fontName), base, noiseChars)
        if fonts[fontName]["verdict"] == "fail":
            verdict = "fail"
    return {"verdict": verdict, "hard": True, "fonts": fonts}


def evalFullGate(report: dict) -> dict:
    step = report["steps"].get("full")
    if step is None:
        return {"verdict": "skip", "hard": False, "note": "套件未包含全量"}
    if step.get("status") != "done":
        return {"verdict": "fail", "hard": False, "note": "步骤未完成或异常退出"}
    data = report["gates"].get("full") or {}
    fonts, verdict = {}, "pass"
    for fontName, base in BASELINES["full"].items():
        cur = data.get(fontName)
        if not cur:
            fonts[fontName] = {"verdict": "fail", "note": "结果缺失"}
            verdict = "fail"
            continue
        ok = cur["rate"] >= base["expectRate"] - 1e-9
        fonts[fontName] = {"tested": cur["tested"], "passed": cur["passed"],
                           "rate": cur["rate"],
                           "expectRate": base["expectRate"],
                           "deltaPp": round(cur["rate"] - base["expectRate"], 2),
                           "verdict": "pass" if ok else "fail"}
        if not ok:
            verdict = "fail"
    return {"verdict": verdict, "hard": False, "fonts": fonts,
            "note": "软门:预期为移交单#5外推值,不影响退出码"}


def evalAll(report: dict) -> int:
    verdicts = {
        "bench": evalCounterGate(report, "bench", "broken"),
        "parity": evalCounterGate(report, "parity", "mismatch"),
        "sample": evalFontGate(report, "sample", 0),
        "cross": evalFontGate(report, "cross", CROSS_NOISE_CHARS),
        "coverage": evalFontGate(report, "coverage", 0),
        "full": evalFullGate(report),
    }
    hardFail = any(v["hard"] and v["verdict"] == "fail"
                   for v in verdicts.values())
    verdicts["overall"] = "fail" if hardFail else "pass"
    report["verdicts"] = verdicts
    return 1 if hardFail else 0


# ---------------------------------------------------------------- 人读版

def fmtDelta(entry: dict) -> str:
    parts = []
    if entry.get("deltaPp") is not None:
        parts.append("%+.2fpp" % entry["deltaPp"])
    if entry.get("deltaPassed") is not None:
        parts.append("%+d字" % entry["deltaPassed"])
    return " ".join(parts) or "—"


def gateTableRows(report: dict) -> list:
    rows = []
    for name in ("bench", "parity"):
        v = report["verdicts"][name]
        gate = report["gates"].get(name) or {}
        num = "—" if not gate else "%d/%d例" % (
            gate.get("broken", gate.get("mismatch", 0)), gate.get("cases", 0))
        rows.append("| %s | 是 | %s | %s | 0 | %s |" % (
            name, v["verdict"], num, v.get("note", "")))
    for name in ("sample", "cross", "coverage", "full"):
        v = report["verdicts"][name]
        hard = "是" if v["hard"] else "否(软门)"
        if "fonts" not in v:
            rows.append("| %s | %s | %s | — | — | %s |" % (
                name, hard, v["verdict"], v.get("note", "")))
            continue
        for fontName, e in v["fonts"].items():
            base = e.get("baseline") or {}
            baseText = base.get("rate", e.get("expectRate", "—"))
            num = "—" if e.get("rate") is None else "%d/%d=%.2f%%" % (
                e["passed"], e["tested"], e["rate"])
            rows.append("| %s:%s | %s | %s | %s | %s | %s |" % (
                name, fontName, hard, e["verdict"], num, baseText,
                fmtDelta(e) + (" " + e["note"] if e.get("note") else "")))
    return rows


def renderMarkdown(report: dict) -> str:
    lines = ["# 门禁报告 %s · %s · **%s**" % (
        report["rev"], report["suite"], report["verdicts"]["overall"]), ""]
    lines.append("生成 %s · Python %s · %s · dirty=%s" % (
        report["createdAt"], report["env"].get("python"),
        report["env"].get("platform"), report["dirty"]))
    lines += ["", "| 门 | 硬门 | 判定 | 本次 | 基线 | 差值/备注 |",
              "|---|---|---|---|---|---|"]
    lines += gateTableRows(report)
    lines += ["", "## 软指标(协议趋势项,compQuota 应恒 0)", "",
              "| 步 | 字体 | compQuota | compOut | orderX | reclass |",
              "|---|---|---|---|---|---|"]
    for stepName, byFont in report["softMetrics"].items():
        for fontName, agg in byFont.items():
            lines.append("| %s | %s | %d | %d | %d | %d |" % (
                stepName, fontName, agg["compQuota"], agg["compOut"],
                agg["orderX"], agg["reclass"]))
    lines += ["", "## 失败字(完整串见 report.json failLists)", ""]
    for stepName, byFont in report["failLists"].items():
        for fontName, chars in byFont.items():
            shown = chars if len(chars) <= 120 else chars[:120] + "…"
            lines.append("- %s/%s(%d): %s" % (
                stepName, fontName, len(chars), shown or "无"))
    lines += ["", "## 步骤", "",
              "| 步 | 状态 | 耗时(s) | 退出码 | run 目录 |",
              "|---|---|---|---|---|"]
    for stepName, entry in report["steps"].items():
        lines.append("| %s | %s | %s | %s | %s |" % (
            stepName, entry.get("status"), entry.get("elapsedSeconds", "—"),
            entry.get("exitCode", "—"), entry.get("runDir", "—")))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主流程

def prepareCtx(args) -> GateCtx:
    if args.resume:
        gateDir = Path(args.resume).expanduser().resolve()
        reportPath = gateDir / "report.json"
        if not reportPath.exists():
            raise SystemExit("resume 目录缺 report.json: %s" % gateDir)
        report = json.loads(reportPath.read_text(encoding="utf-8"))
        report["suite"] = args.suite or report.get("suite", "std")
        report["resumedAt"] = datetime.now().astimezone().isoformat()
    else:
        suite = args.suite or "std"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        gateDir = ROOT / "verifyOut" / "gateReport" / (
            "%s-%s" % (stamp, gitInfo(ROOT)["rev"]))
        gateDir.mkdir(parents=True, exist_ok=True)
        report = newReport(ROOT, gateDir, suite)
    return GateCtx(root=ROOT, gateDir=gateDir, jobs=args.jobs, report=report)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一键门禁套件(说明见模块头)")
    ap.add_argument("--suite", choices=tuple(SUITES), default=None,
                    help="quick=bench+parity+sample; std(默认)=+cross+coverage;"
                         " full=+全量+trace")
    ap.add_argument("--resume", help="已有 gateReport 目录,跳过已完成步")
    ap.add_argument("--jobs", type=int, default=0,
                    help="传给 verify_batch/trace_stats 的 worker 数")
    args = ap.parse_args(argv)

    ctx = prepareCtx(args)
    steps = SUITES[ctx.report["suite"]]
    for name in steps:
        ctx.report["steps"].setdefault(name, {"status": "pending"})
    saveReport(ctx)
    for name in steps:
        if stepDone(ctx, name):
            print("[gate] %s: 已完成,跳过" % name, flush=True)
            if name in VERIFY_STEPS:      # 重读磁盘,保证聚合与工件一致
                collectVerifyGate(ctx, name)
            continue
        print("[gate] 运行 %s ..." % name, flush=True)
        runStep(ctx, name)
        saveReport(ctx)
    exitCode = evalAll(ctx.report)
    (ctx.gateDir / "report.md").write_text(
        renderMarkdown(ctx.report), encoding="utf-8")
    saveReport(ctx)
    (ROOT / "verifyOut" / "gateReport" / "latest.txt").write_text(
        str(ctx.gateDir), encoding="utf-8")
    print("门禁报告:", ctx.gateDir / "report.json")
    print("总判定: %s (退出码 %d)" % (
        ctx.report["verdicts"]["overall"], exitCode))
    return exitCode


if __name__ == "__main__":
    raise SystemExit(main())
