"""Build a complete, traceable repair inventory from an existing verification run.

This is analysis only: it neither runs nor changes the decomposition pipeline.
Failure indices in verify output are zero based; the inventory also gives the
one-based stroke number used when reviewing a character.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strokelab import DataHub


REPAIR_TRACKS = {
    'ERROR': '检查异常路径、非法几何与缺失输入；不得跳过后计通过',
    'COUNT': '追踪空笔所属组及救济走廊；恢复有效墨区并检查被让出区域的邻笔',
    'SPLIT': '区分字体设计分离与同一墨区内断裂；检查连接桥及碎片归属',
    'UNION': '检查轮廓绕向、裁剪和残差回填；独立并集验证不得退化',
    'TYPE': '检查组认领、模板段完整性与直笔端部抢占；按参照笔方向复核',
    'ORDER': '检查同型笔的相对位置及终态中轴采样；区分身份互换与采样偏移',
    'OVERLAP': '定位不应相交的笔对；受连通性和覆盖约束重新分配共享墨区',
    'AREA': '定位过小/过大笔及邻笔；检查走廊定位、抢占和救济后的面积分配',
}


def indices(failure):
    detail = failure['detail']
    code = failure['code']
    if code == 'COUNT':
        m = re.search(r'\[([^]]*)\]', detail)
        return [int(i) for i in re.findall(r'\d+', m.group(1))] if m else []
    if code in ('ORDER', 'OVERLAP'):
        return sorted({int(i) for pair in re.findall(r'(\d+)-(\d+)', detail)
                       for i in pair})
    if code in ('TYPE', 'AREA', 'SPLIT'):
        return [int(i) for i in re.findall(r'(?:^|\s)(\d+)(?=[:(])', detail)]
    return []


def component_path(kai, idx):
    matches = kai.get('matches', [])
    path = matches[idx] if idx < len(matches) else None
    tree = kai.get('structure') or {}
    result = []
    for child_idx in path or []:
        children = tree.get('children') or []
        if child_idx >= len(children):
            break
        tree = children[child_idx]
        if tree.get('char'):
            result.append(tree['char'])
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default='.')
    ap.add_argument('--baseline', required=True)
    ap.add_argument('--commit', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    baseline = Path(args.baseline)
    raw = baseline.read_bytes()
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    if len(rows) != len({r['ch'] for r in rows}):
        raise ValueError('Baseline contains duplicate characters')
    hub = DataHub(str(Path(args.root).resolve()))
    cases = []
    code_chars = defaultdict(set)
    families = defaultdict(set)
    combos = Counter()
    for r in sorted(rows, key=lambda r: r['ch']):
        if not r.get('fails'):
            continue
        kai = hub.kai(r['ch'])
        problems = []
        for f in r['fails']:
            code_chars[f['code']].add(r['ch'])
            affected = []
            for idx in indices(f):
                types = kai['strokeTypes']
                kind = types[idx] if idx < len(types) else '?'
                path = component_path(kai, idx)
                affected.append({'index': idx, 'strokeNumber': idx + 1,
                                 'type': kind, 'componentPath': path})
                component = path[-1] if path else '(whole glyph)'
                families[(f['code'], component, kind)].add(r['ch'])
            problems.append({'code': f['code'], 'detail': f['detail'],
                             'affectedStrokes': affected,
                             'repairTrack': REPAIR_TRACKS[f['code']]})
        codes = sorted({f['code'] for f in r['fails']})
        combos['+'.join(codes)] += 1
        cases.append({'ch': r['ch'], 'codepoint': 'U+%04X' % ord(r['ch']),
                      'strokeCount': len(kai['medians']),
                      'decomposition': kai.get('decomposition'),
                      'codes': codes, 'problems': problems,
                      'status': 'baseline_failure_requires_recheck'})
    tested = [r for r in rows if not r.get('skip')]
    result = {'baselineCommit': args.commit,
              'baselineSha256': hashlib.sha256(raw).hexdigest(),
              'font': 'HarmonyOS_Sans_SC.ttf',
              'note': 'Family grouping is a diagnostic hypothesis, not a confirmed root cause. '
                      'TYPE/AREA/ORDER/OVERLAP details may list only the first 8 affected items.',
              'total': len(rows), 'tested': len(tested), 'failed': len(cases),
              'passed': len(tested) - len(cases),
              'byCode': {c: {'count': len(cs), 'chars': ''.join(sorted(cs))}
                         for c, cs in sorted(code_chars.items())},
              'combinations': dict(combos.most_common()),
              'families': [{'code': key[0], 'component': key[1], 'type': key[2],
                            'count': len(cs), 'chars': ''.join(sorted(cs))}
                           for key, cs in sorted(families.items(),
                                                 key=lambda kv: (-len(kv[1]), kv[0]))],
              'cases': cases}
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('total', 'tested', 'passed', 'failed')}, ensure_ascii=False))
    for family in result['families'][:15]:
        print(json.dumps(family, ensure_ascii=False))


if __name__ == '__main__':
    main()
