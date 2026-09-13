"""Independent audit cases; production code is imported from a frozen revision."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
DATA = BASE / 'source'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--revision', default='f553ce7')
    ap.add_argument('--chars', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--probe', action='store_true')
    ap.add_argument('--disable-ladder', action='store_true')
    ap.add_argument('--resume', action='store_true')
    a = ap.parse_args()
    rev = subprocess.check_output(['git', 'rev-parse', a.revision], cwd=ROOT, text=True).strip()
    source = BASE / ('audit-source-' + rev[:7])
    if not source.exists():
        source.mkdir()
        raw = subprocess.check_output(['git', 'archive', '--format=zip', rev, 'strokelab'], cwd=ROOT)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            z.extractall(source)
    sys.path.insert(0, str(source))
    from strokelab import DataHub, FontEntry
    from strokelab import pipeline, verify
    cache = source / '.blibCache'
    cache.mkdir(exist_ok=True)
    FontEntry._libCachePath = lambda self, hub: str(cache / (Path(self.key).stem + '.json'))
    hub = DataHub(str(DATA))
    font = FontEntry(str(DATA / 'Fonts' / 'HarmonyOS_Sans_SC.ttf'))
    font.buildLibraryB(hub)
    font.completeLibraryB(hub)
    if a.disable_ladder:
        pipeline.LADDER_ACT = False
    pipeline.LADDER_PROBE = a.probe
    out = ROOT / 'verifyOut' / 'acceptance-f553ce7' / 'audit'
    out.mkdir(exist_ok=True)
    dest = out / (a.label + '.jsonl')
    done = set()
    if dest.exists() and a.resume:
        prior = [json.loads(line) for line in dest.read_text(encoding='utf-8').splitlines() if line.strip()]
        done = {r['ch'] for r in prior}
        if len(done) != len(prior) or not done <= set(a.chars):
            raise RuntimeError('Invalid partial audit output')
        if any(r['audit']['revision'] != rev or r['audit']['ladderDisabled'] != a.disable_ladder for r in prior):
            raise RuntimeError('Audit revision/options mismatch')
    elif dest.exists():
        raise RuntimeError('Refusing to overwrite audit output')
    with dest.open('a' if a.resume else 'w', encoding='utf-8') as stream:
        for ch in dict.fromkeys(a.chars):
            if ch in done:
                continue
            start = time.monotonic()
            r = pipeline.runPipeline(hub, font, ch)
            original = pipeline.runPipeline
            try:
                pipeline.runPipeline = lambda *args, **kw: r
                v = verify.verifyChar(hub, font, ch)
            finally:
                pipeline.runPipeline = original
            v['audit'] = {'revision': rev, 'ladderDisabled': a.disable_ladder,
                          'ladderRealign': r.get('ladderRealign', []),
                          'ladderProbe': r.get('ladderProbe', []),
                          'pass': r.get('pass'),
                          'pathSha256': hashlib.sha256(json.dumps(
                              [(s['index'], s['path']) for s in r.get('strokes', [])],
                              ensure_ascii=False).encode()).hexdigest(),
                          'seconds': round(time.monotonic() - start, 2)}
            stream.write(json.dumps(v, ensure_ascii=False) + '\n')
            stream.flush()
            print(ch, [f['code'] for f in v['fails']], v['audit']['ladderRealign'], flush=True)
            rawdir = BASE / ('raw-' + a.label)
            rawdir.mkdir(exist_ok=True)
            (rawdir / ('U%04X.json' % ord(ch))).write_text(json.dumps({'result': r, 'verify': v}, ensure_ascii=False), encoding='utf-8')
    font.font.close()


if __name__ == '__main__':
    main()
