"""Check extracted centerline parity and document topology API boundaries."""
import importlib.util
import ast
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'source'))
from strokelab import geometry as current
from strokelab import FontEntry
from strokelab.boolean import glyphRegion
from shapely.geometry import Polygon, box
from shapely.affinity import scale

old_file = next(BASE.glob('audit-source-e4c9dfd/strokelab/geometry.py'))
spec = importlib.util.spec_from_file_location('previous_geometry', old_file)
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)

regions = {
    'rectangle': box(0, 0, 500, 60),
    'ring': box(0, 0, 500, 600).difference(box(60, 60, 440, 540)),
    'cross': box(0, 270, 600, 330).union(box(270, 0, 330, 600)),
    'short_true_T': box(0, 400, 600, 460).union(box(270, 200, 330, 430)),
    'disconnected': box(0, 0, 100, 50).union(box(200, 0, 300, 50)),
    'empty': Polygon(),
}
old_tree = ast.parse(old_file.read_text(encoding='utf-8'))
new_tree = ast.parse((BASE / 'source/strokelab/geometry.py').read_text(encoding='utf-8'))
old_fn = next(n for n in old_tree.body if isinstance(n, ast.FunctionDef) and n.name == 'outlineCenterline')
new_fn = next(n for n in new_tree.body if isinstance(n, ast.FunctionDef) and n.name == '_medialAdjacency')
def graph_body(fn):
    start = next(i for i, n in enumerate(fn.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'bnd' for t in n.targets))
    body = []
    for n in fn.body[start:]:
        if isinstance(n, ast.FunctionDef) and n.name == 'farthest':
            break
        if isinstance(n, ast.Return):
            break
        body.append(ast.dump(n, include_attributes=False))
    return body
results = {'graphConstructionAstEqual': graph_body(old_fn) == graph_body(new_fn),
           'parity': [], 'boundaries': []}
for name, region in regions.items():
    polygons = list(region.geoms) if hasattr(region, 'geoms') else [region]
    loops = [list(r.coords) for pg in polygons for r in [pg.exterior] + list(pg.interiors)]
    a, b = old.outlineCenterline(loops), current.outlineCenterline(loops)
    results['parity'].append({'case': name, 'equal': a == b})
    try:
        result = current.medialJunctions(region)
        results['boundaries'].append({'case': name, 'degrees': sorted(v[2] for v in result)})
    except Exception as e:
        results['boundaries'].append({'case': name, 'error': repr(e)})
for factor in (0.5, 1, 2):
    region = scale(regions['short_true_T'], xfact=factor, yfact=factor, origin=(0, 0))
    results['boundaries'].append({'case': 'short_true_T', 'scale': factor,
                                 'degrees': sorted(v[2] for v in current.medialJunctions(region))})
results['fontCalibration'] = []
expected = {'日': [3, 3], '口': [], '田': [3, 3, 3, 3, 4], '中': [4, 4],
            '王': [3, 3, 4], '土': [3, 4]}
for fontname in ('HarmonyOS_Sans_SC.ttf', 'simhei.ttf', 'NotoSansSC-VariableFont_wght.ttf'):
    font = FontEntry(str(BASE / 'source/Fonts' / fontname))
    for ch, degrees in expected.items():
        contours = font.glyphContours(ch)
        current.analyzeContours(contours)
        region = glyphRegion(contours)
        actual = sorted(v[2] for v in current.medialJunctions(region))
        loops = [current.flattenSegs(c['segs'], 6) for c in contours]
        results['fontCalibration'].append({'font': fontname, 'ch': ch,
                                           'expected': degrees, 'actual': actual,
                                           'match': degrees == actual,
                                           'centerlineEqual': old.outlineCenterline(loops) == current.outlineCenterline(loops)})
    font.font.close()
dest = BASE.parents[1] / 'verifyOut/acceptance-f553ce7/audit/topology.json'
dest.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(results, ensure_ascii=False, indent=2))
