"""Run protocol #3 against an immutable Git snapshot and isolated font cache."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'source'
OUT = ROOT / 'verifyOut' / 'acceptance-f553ce7'
COMMIT = 'f553ce730d4f69d9553c02e32e7c79ac8075cfb0'
FONTS = ['HarmonyOS_Sans_SC.ttf', 'NotoSansSC-VariableFont_wght.ttf',
         'simhei.ttf', 'ZCOOLXiaoWei-Regular.ttf', 'ZCOOLKuaiLe-Regular.ttf']


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def prepare():
    if SOURCE.exists():
        raise RuntimeError('Refusing to overwrite an existing snapshot')
    OUT.mkdir(parents=True, exist_ok=True)
    archive = subprocess.check_output(['git', 'archive', '--format=zip', COMMIT,
                                       'strokelab', 'tools', 'bench'], cwd=ROOT)
    SOURCE.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for member in z.infolist():
            target = (SOURCE / member.filename).resolve()
            if SOURCE.resolve() not in target.parents and target != SOURCE.resolve():
                raise RuntimeError('Unsafe archive path')
        z.extractall(SOURCE)
    data = ['makemeahanzi-master/graphics.txt', 'makemeahanzi-master/dictionary.txt',
            'hanzi_chaizi-master/raw_data/chaizi-jt.txt',
            'hanzi_chaizi-master/raw_data/chaizi-ft.txt']
    for rel in data + ['Fonts/' + f for f in FONTS]:
        dest = SOURCE / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    # Reader independently checks the algorithm/font signature before reuse.
    (SOURCE / '.blibCache').mkdir()
    for f in FONTS:
        cache = ROOT / '.blibCache' / (Path(f).stem + '.json')
        if cache.is_file():
            shutil.copy2(cache, SOURCE / '.blibCache' / cache.name)
    for mode, run in [('sample', '20260913-001125-sample'), ('cross', '20260912-234423-cross')]:
        shutil.copy2(ROOT / 'verifyOut' / 'runs' / run / 'chars.txt', OUT / (mode + '-chars.txt'))
    manifest = {'commit': COMMIT, 'preparedAt': datetime.now().astimezone().isoformat(),
                'sourceSha256': {str(p.relative_to(SOURCE)).replace('\\', '/'): digest(p)
                                 for p in sorted((SOURCE / 'strokelab').iterdir()) if p.is_file()},
                'fontSha256': {f: digest(SOURCE / 'Fonts' / f) for f in FONTS},
                'dataSha256': {f: digest(SOURCE / f) for f in data},
                'charSha256': {mode: digest(OUT / (mode + '-chars.txt')) for mode in ('sample', 'cross')},
                'python': sys.version, 'jobs': 6}
    save('identity.json', manifest)
    print('Prepared', SOURCE, flush=True)


def run():
    resume = '--resume' in sys.argv
    identity = json.loads((OUT / 'identity.json').read_text(encoding='utf-8'))
    for rel, expected in identity['sourceSha256'].items():
        if digest(SOURCE / rel) != expected:
            raise RuntimeError('Snapshot changed: ' + rel)
    for name, expected in identity['fontSha256'].items():
        if digest(SOURCE / 'Fonts' / name) != expected:
            raise RuntimeError('Font changed: ' + name)
    for name, expected in identity['dataSha256'].items():
        if digest(SOURCE / name) != expected:
            raise RuntimeError('Data changed: ' + name)
    for mode, expected in identity['charSha256'].items():
        if digest(OUT / (mode + '-chars.txt')) != expected:
            raise RuntimeError('Character set changed: ' + mode)
    env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', PYTHONUTF8='1')
    phases = [
        ('sample', [sys.executable, '-X', 'utf8', '-u', 'tools/verify_batch.py', '--root', str(SOURCE),
                    '--preset', 'sample', '--chars-file', str(OUT / 'sample-chars.txt'),
                    '--jobs', '6', '--out', str(OUT / 'sample'), '--no-resume']),
        ('bench', [sys.executable, '-X', 'utf8', '-u', '-m', 'strokelab.bench', '--root', str(SOURCE), '--check']),
        ('cross', [sys.executable, '-X', 'utf8', '-u', 'tools/verify_batch.py', '--root', str(SOURCE),
                   '--preset', 'cross', '--chars-file', str(OUT / 'cross-chars.txt'),
                   '--jobs', '6', '--out', str(OUT / 'cross'), '--no-resume']),
    ]
    status = {'commit': COMMIT, 'status': 'running', 'startedAt': datetime.now().astimezone().isoformat(),
              'phases': {}}
    if resume and (OUT / 'status.json').exists():
        status = json.loads((OUT / 'status.json').read_text(encoding='utf-8'))
        status['status'] = 'running'
        status.setdefault('resumedAt', []).append(datetime.now().astimezone().isoformat())
    for name, command in phases:
        if resume and status['phases'].get(name, {}).get('exitCode') == 0:
            continue
        if not resume and (OUT / (name + '.log')).exists():
            raise RuntimeError('Refusing to overwrite existing phase: ' + name)
        if resume and name in ('sample', 'cross'):
            expected_chars = set((OUT / (name + '-chars.txt')).read_text(encoding='utf-8').strip())
            for path in (OUT / name).glob('*.jsonl'):
                rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
                chars = [r['ch'] for r in rows]
                if len(chars) != len(set(chars)) or not set(chars) <= expected_chars:
                    raise RuntimeError('Invalid partial result: ' + str(path))
            command.remove('--no-resume')
        status['phase'] = name
        save('status.json', status)
        print('START', name, datetime.now().astimezone().isoformat(), flush=True)
        start = time.monotonic()
        with (OUT / (name + '.log')).open('a' if resume else 'w', encoding='utf-8') as log:
            proc = subprocess.run(command, cwd=SOURCE, env=env, stdout=log, stderr=subprocess.STDOUT)
        status['phases'][name] = {'exitCode': proc.returncode, 'seconds': round(time.monotonic() - start, 2),
                                 'command': command}
        save('status.json', status)
        print('DONE', name, proc.returncode, flush=True)
    status['status'] = 'completed' if all(p['exitCode'] == 0 for p in status['phases'].values()) else 'check_failed'
    status['finishedAt'] = datetime.now().astimezone().isoformat()
    save('status.json', status)


if __name__ == '__main__':
    {'prepare': prepare, 'run': run}[sys.argv[1]]()
