"""Strictly validate complete protocol JSONL runs before comparing pass counts.

Unlike a comparison over common characters, missing/duplicate records and changed
skip states are integrity errors.  Algorithm failure rules remain untouched.
"""
import argparse
from collections import Counter
import json
from pathlib import Path


def load_records(path):
    rows = {}
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        ch = r.get('ch')
        if not isinstance(ch, str) or len(ch) != 1:
            raise ValueError('%s:%d: invalid character' % (path, number))
        if ch in rows:
            raise ValueError('%s:%d: duplicate %s' % (path, number, ch))
        if not r.get('skip') and not isinstance(r.get('fails'), list):
            raise ValueError('%s:%d: missing failure list' % (path, number))
        rows[ch] = r
    if not rows:
        raise ValueError('Empty result: %s' % path)
    return rows


def codes(r):
    return {f['code'] for f in r.get('fails', [])}


def stats(rows):
    tested = [r for r in rows.values() if not r.get('skip')]
    passed = sum(not codes(r) for r in tested)
    by_code = Counter(c for r in tested for c in codes(r))
    soft = {k: sum(r.get('m', {}).get(k, 0) for r in tested)
            for k in ('compQuota', 'compOut', 'orderX', 'reclass')}
    return {'total': len(rows), 'tested': len(tested),
            'skipped': len(rows) - len(tested), 'passed': passed,
            'passRate': round(100 * passed / len(tested), 4) if tested else 0,
            'byCode': dict(sorted(by_code.items())), 'soft': soft}


def compare(base, current, expected=None, min_passed=0, max_net_loss=0):
    errors = []
    if set(base) != set(current):
        errors.append('baseline/current character sets differ')
    if expected is not None and set(current) != set(expected):
        errors.append('result character set differs from expected')
    fixed, broken, migrated, skipped = [], [], [], []
    for ch in sorted(set(base) & set(current)):
        b, c = base[ch], current[ch]
        if bool(b.get('skip')) != bool(c.get('skip')):
            skipped.append(ch)
            continue
        if c.get('skip'):
            continue
        bc, cc = codes(b), codes(c)
        if bc and not cc:
            fixed.append(ch)
        elif not bc and cc:
            broken.append({'ch': ch, 'fails': c['fails']})
        elif bc != cc:
            migrated.append({'ch': ch, 'before': sorted(bc), 'after': sorted(cc),
                             'added': sorted(cc - bc), 'removed': sorted(bc - cc)})
    if skipped:
        errors.append('skip state changed')
    bstats, cstats = stats(base), stats(current)
    if cstats['passed'] < min_passed:
        errors.append('below required pass count')
    if cstats['passed'] < bstats['passed'] - max_net_loss:
        errors.append('net loss exceeds permitted count')
    new_hard = {}
    for code in ('ERROR', 'UNION'):
        new_hard[code] = sorted(ch for ch, r in current.items()
                                if not r.get('skip') and code in codes(r)
                                and code not in codes(base.get(ch, {})))
        if new_hard[code]:
            errors.append('new ' + code)
    if cstats['soft']['compQuota'] != 0:
        errors.append('compQuota is not zero')
    return {'integrityAndCountGatePassed': not errors,
            'note': 'Individual regressions and code migrations still require adversarial review.',
            'errors': errors, 'baseline': bstats, 'current': cstats,
            'fixed': fixed, 'regressions': broken, 'migrations': migrated,
            'skipChanged': skipped,
            'newHardFailures': new_hard,
            'missing': sorted(set(base) - set(current)),
            'extra': sorted(set(current) - set(base)),
            'requirements': {'minPassed': min_passed, 'maxNetLoss': max_net_loss}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('baseline')
    p.add_argument('current')
    p.add_argument('--chars-file')
    p.add_argument('--min-passed', type=int, default=0)
    p.add_argument('--max-net-loss', type=int, default=0)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    expected = None
    if a.chars_file:
        expected = ''.join(Path(a.chars_file).read_text(encoding='utf-8').split())
        if len(expected) != len(set(expected)):
            p.error('expected character list contains duplicates')
    result = compare(load_records(a.baseline), load_records(a.current), expected,
                     a.min_passed, a.max_net_loss)
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['integrityAndCountGatePassed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
