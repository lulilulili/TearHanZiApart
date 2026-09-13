"""Check completed evidence, without running or changing the algorithm."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent / 'source'
OUT = ROOT / 'verifyOut/acceptance-f553ce7'
sys.path.insert(0, str(ROOT))
from tools.check_protocol_results import load_records, compare

identity = json.loads((OUT / 'identity.json').read_text(encoding='utf-8'))
changed = []
for key, prefix in [('sourceSha256', SOURCE), ('fontSha256', SOURCE / 'Fonts'),
                    ('dataSha256', SOURCE)]:
    for rel, expected in identity[key].items():
        if hashlib.sha256((prefix / rel).read_bytes()).hexdigest() != expected:
            changed.append(rel)
for mode, expected in identity['charSha256'].items():
    if hashlib.sha256((OUT / (mode + '-chars.txt')).read_bytes()).hexdigest() != expected:
        changed.append(mode + ' chars')

status = json.loads((OUT / 'status.json').read_text(encoding='utf-8'))
deferred = status.get('crossDeferredByUser', False)
assert status['status'] == ('completed_reduced_scope' if deferred else 'completed'), 'Run is not complete'
assert set(status['phases']) == ({'sample', 'bench'} if deferred else {'sample', 'bench', 'cross'})
assert all(p['exitCode'] == 0 for p in status['phases'].values())
assert not changed, changed
checks = {}
for mode, font, baseline_dir, minimum, loss in [
    ('sample', 'HarmonyOS_Sans_SC', '20260913-001125-sample', 920, 0),
    ('cross', 'simhei', '20260912-234423-cross', 281, 2),
    ('cross', 'NotoSansSC-VariableFont_wght', '20260912-234423-cross', 237, 2),
]:
    if mode == 'cross' and deferred:
        continue
    base = load_records(ROOT / 'verifyOut/runs' / baseline_dir / (font + '.jsonl'))
    current = load_records(OUT / mode / (font + '.jsonl'))
    chars = ''.join((OUT / (mode + '-chars.txt')).read_text(encoding='utf-8').split())
    assert len(chars) == len(set(chars))
    result = compare(base, current, chars, minimum, loss)
    name = 'sample-comparison' if mode == 'sample' else font + '-comparison'
    (OUT / (name + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    checks[font] = {
        'countGate': result['integrityAndCountGatePassed'],
        'stats': result['current'],
        'changedFailureDetails': [ch for ch in sorted(current) if base.get(ch, {}).get('fails') != current[ch].get('fails')],
        'changedMetrics': [ch for ch in sorted(current) if base.get(ch, {}).get('m') != current[ch].get('m')],
        'regressions': result['regressions'], 'migrations': result['migrations'],
    }

bench_text = (OUT / 'bench.log').read_text(encoding='utf-8')
bench_match = re.search(r'(\d+) 个用例; 破坏\(verified 回退/错误\): (\d+)', bench_text)
assert bench_match and bench_match.groups() == ('65', '0'), 'Bench gate did not pass'
golden = [json.loads(p.read_text(encoding='utf-8')) for p in (SOURCE / 'bench/golden').glob('*.json')]
report = {'snapshotUnchanged': not changed, 'requestedScopeCompleted': True,
          'crossDeferredByUser': deferred, 'checks': checks,
          'bench': {'cases': 65, 'broken': 0, 'verifiedGoldenCount': sum(g.get('status') == 'verified' for g in golden)}}
(OUT / 'final-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
assert all(c['countGate'] for c in checks.values()), 'A numeric/integrity gate failed'
