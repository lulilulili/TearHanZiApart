#!/usr/bin/env python3
"""可复用的批量拆字验收运行器。

这个入口只负责编排，不改变 strokelab 的校验规则：

    python -X utf8 tools/verify_batch.py --preset sample
    python -X utf8 tools/verify_batch.py --preset cross
    python -X utf8 tools/verify_batch.py --preset full

每次运行写入独立的 verifyOut/runs/<时间戳>-<preset>/，因此不会把旧
jsonl、不同字体或不同源码版本混进同一份 summary。默认并发上限为 8，
适合 Windows 家用环境；用 --jobs 明确指定时才覆盖它。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path


DEFAULT_FONTS = {
    "sample": "HarmonyOS_Sans_SC.ttf",
    "cross": "simhei.ttf,NotoSansSC-VariableFont_wght.ttf",
    "full": "HarmonyOS_Sans_SC.ttf",
}
DEFAULT_STRIDE = {"sample": 10, "cross": 30, "full": 1}


def choose_jobs(cpu_count: int | None = None, requested: int = 0) -> int:
    """选择稳定的默认 worker 数，避免 Windows 过度复制字体库。"""
    if requested > 0:
        return max(1, requested)
    cpus = cpu_count or (os.cpu_count() or 1)
    return max(1, min(8, cpus - 2 if cpus > 2 else 1))


def select_chars(chars: str, mode: str, stride: int = 10) -> str:
    """按模式选择字集，并保留顺序和重复项（file 模式需要这个语义）。"""
    if mode == "full" or mode == "file":
        return "".join(ch for ch in chars if not ch.isspace())
    if mode == "sample":
        if stride < 1:
            raise ValueError("stride must be >= 1")
        return "".join(chars[::stride])
    raise ValueError(f"unknown char mode: {mode}")


def discover_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def add_root_to_path(root: Path) -> None:
    """允许脚本代码与数据根目录不在同一份 checkout。"""
    values = (str(root), str(Path(__file__).resolve().parents[1]))
    for value in reversed(values):
        if value not in sys.path:
            sys.path.insert(0, value)


def require_data_root(root: Path) -> None:
    required = (
        root / "Fonts",
        root / "makemeahanzi-master" / "graphics.txt",
        root / "makemeahanzi-master" / "dictionary.txt",
    )
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SystemExit(
            "数据目录不完整。请在项目根目录提供 Fonts/、"
            "makemeahanzi-master/，或用 --root 指向家里的项目目录。\n"
            + "缺少:\n  " + "\n  ".join(missing)
        )


def parse_fonts(root: Path, value: str) -> list[str]:
    if value == "all":
        return sorted(p.name for p in (root / "Fonts").iterdir()
                      if p.suffix.lower() in {".ttf", ".otf"})
    fonts = [x.strip() for x in value.split(",") if x.strip()]
    missing = [f for f in fonts if not (root / "Fonts" / f).exists()]
    if missing:
        raise SystemExit("字体不存在: " + ", ".join(missing))
    return fonts


def file_fingerprint(path: Path) -> dict[str, object]:
    st = path.stat()
    return {"path": str(path), "size": st.st_size,
            "mtimeNs": st.st_mtime_ns}


def source_fingerprint(root: Path) -> dict[str, object]:
    files = [root / "strokelab" / n for n in
             ("pipeline.py", "geometry.py", "classify.py", "boolean.py",
              "fonthub.py", "verify.py")]
    h = hashlib.sha256()
    for p in files:
        if p.exists():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return {"algorithmSha256": h.hexdigest(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine()}


def make_chars(root: Path, preset: str, chars_file: str | None,
               chars_text: str | None, stride: int) -> tuple[str, str]:
    from strokelab.datahub import DataHub

    if chars_file:
        raw = Path(chars_file).expanduser().read_text(encoding="utf-8")
        return select_chars(raw, "file", stride), "file"
    if chars_text:
        return select_chars(chars_text, "full", stride), "text"
    hub = DataHub(str(root))
    all_chars = "".join(sorted(hub.graphicsIndex.keys()))
    mode = "full" if preset == "full" else "sample"
    return select_chars(all_chars, mode, stride), mode


def run_verify(root: Path, output: Path, fonts: list[str], chars: str,
               jobs: int, resume: bool) -> dict:
    """调用现有 verify API，并把它的输出目录隔离到本次运行。"""
    import strokelab.verify as verify

    output.mkdir(parents=True, exist_ok=True)
    original_out_dir = verify.outDirOf
    verify.outDirOf = lambda _root: str(output)
    try:
        started = time.perf_counter()
        for font in fonts:
            # Windows 使用 spawn：没有预热时，多个 worker 可能在同一时间
            # 发现 B 库不存在并重复执行昂贵的建库。主进程先完成一次建库，
            # 随后的 worker 只读取同一份算法签名校验过的库数据。
            from strokelab.verify import _initWorker
            _initWorker(str(root), str(root / "Fonts" / font))
            verify.runFont(str(root), font, chars, jobs, resume)
        summary = verify.summarize(str(root))
        elapsed = time.perf_counter() - started
    finally:
        verify.outDirOf = original_out_dir
    return {"summary": summary, "elapsedSeconds": round(elapsed, 3)}


def run_bench(root: Path) -> int:
    # 进程内调用也支持“代码 checkout 与 --root 数据目录分离”；若启用
    # 子进程，Python 会把数据目录当 cwd，而家用数据目录通常没有包代码。
    from strokelab.bench import cmdCheck
    return int(bool(cmdCheck(str(root))))


def write_manifest(output: Path, root: Path, preset: str, fonts: list[str],
                   chars: str, stride: int, jobs: int, result: dict) -> None:
    manifest = {
        "createdAt": datetime.now().astimezone().isoformat(),
        "preset": preset,
        "root": str(root),
        "fonts": [file_fingerprint(root / "Fonts" / f) for f in fonts],
        "charCount": len(chars),
        "charSha256": hashlib.sha256(chars.encode()).hexdigest(),
        "stride": stride,
        "jobs": jobs,
        "source": source_fingerprint(root),
        "elapsedSeconds": result.get("elapsedSeconds"),
        "summary": result.get("summary", {}),
    }
    (output / "chars.txt").write_text(chars, encoding="utf-8")
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", help="项目根目录，默认是脚本所在仓库")
    ap.add_argument("--preset", choices=("sample", "cross", "full"),
                    default="sample")
    ap.add_argument("--fonts", default=None,
                    help="逗号分隔字体文件名；默认按 preset 选择")
    ap.add_argument("--chars-file", help="自定义字集文件")
    ap.add_argument("--chars-text", help="直接提供字集")
    ap.add_argument("--stride", type=int, default=0,
                    help="抽样步长，默认 sample=10/cross=30/full=1")
    ap.add_argument("--jobs", type=int, default=0,
                    help="worker 数；默认 min(8, CPU-2)")
    ap.add_argument("--out", help="本次结果目录；默认 verifyOut/runs/时间戳")
    ap.add_argument("--no-resume", action="store_true",
                    help="忽略已有 jsonl，从头跑")
    ap.add_argument("--bench", action="store_true", help="完成校验后运行 bench")
    a = ap.parse_args(argv)

    if a.chars_file and a.chars_text:
        ap.error("--chars-file 与 --chars-text 不能同时使用")
    root = discover_root(a.root)
    require_data_root(root)
    add_root_to_path(root)
    preset = a.preset
    stride = a.stride or DEFAULT_STRIDE[preset]
    chars, char_mode = make_chars(root, preset, a.chars_file,
                                  a.chars_text, stride)
    fonts = parse_fonts(root, a.fonts or DEFAULT_FONTS[preset])
    jobs = choose_jobs(requested=a.jobs)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = Path(a.out).expanduser().resolve() if a.out else \
        root / "verifyOut" / "runs" / f"{stamp}-{preset}"
    print(f"root={root}")
    print(f"preset={preset} chars={len(chars)} mode={char_mode} "
          f"fonts={','.join(fonts)} jobs={jobs}")
    started = time.perf_counter()
    result = run_verify(root, output, fonts, chars, jobs, not a.no_resume)
    result["elapsedSeconds"] = round(time.perf_counter() - started, 3)
    write_manifest(output, root, preset, fonts, chars, stride, jobs, result)
    print(f"结果: {output}")
    for name, s in result["summary"].items():
        print(f"{name}: 测{s['tested']} 过{s['passed']} "
              f"({s['passRate']:.2f}%)")
    if a.bench:
        print("运行 bench --check ...")
        return run_bench(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
