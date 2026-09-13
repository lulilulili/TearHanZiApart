import json
from pathlib import Path
import tempfile
import unittest

from tools.check_protocol_results import compare, load_records


def row(ch, failure=None, skip=False):
    return {'ch': ch, 'skip': True} if skip else {
        'ch': ch, 'fails': [{'code': failure, 'detail': 'test'}] if failure else [],
        'm': {'compQuota': 0}}


class ProtocolResultTests(unittest.TestCase):
    def test_missing_character_does_not_disappear_in_intersection(self):
        result = compare({'甲': row('甲'), '乙': row('乙')}, {'甲': row('甲')})
        self.assertFalse(result['integrityAndCountGatePassed'])
        self.assertEqual(result['missing'], ['乙'])

    def test_skip_change_cannot_improve_the_score(self):
        result = compare({'甲': row('甲', 'TYPE')}, {'甲': row('甲', skip=True)})
        self.assertFalse(result['integrityAndCountGatePassed'])

    def test_net_improvement_still_lists_regression(self):
        base = {c: row(c, 'TYPE' if c != '丙' else None) for c in '甲乙丙'}
        cur = {c: row(c, 'AREA' if c == '丙' else None) for c in '甲乙丙'}
        result = compare(base, cur)
        self.assertTrue(result['integrityAndCountGatePassed'])
        self.assertEqual(result['regressions'][0]['ch'], '丙')

    def test_duplicate_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rows.jsonl'
            p.write_text('\n'.join(json.dumps(row('甲')) for _ in range(2)), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                load_records(p)

    def test_new_union_failure_blocks_gate_even_with_net_gain(self):
        base = {c: row(c, 'TYPE') for c in '甲乙'}
        cur = {'甲': row('甲'), '乙': row('乙', 'UNION')}
        self.assertFalse(compare(base, cur)['integrityAndCountGatePassed'])

    def test_cross_two_character_noise_is_explicit(self):
        base = {c: row(c) for c in '甲乙丙'}
        cur = {c: row(c, 'TYPE' if c != '甲' else None) for c in '甲乙丙'}
        self.assertTrue(compare(base, cur, max_net_loss=2)['integrityAndCountGatePassed'])
        self.assertFalse(compare(base, cur, max_net_loss=1)['integrityAndCountGatePassed'])

    def test_hard_failure_moving_to_another_character_is_new(self):
        for code in ('ERROR', 'UNION'):
            with self.subTest(code=code):
                base = {'甲': row('甲', code), '乙': row('乙')}
                cur = {'甲': row('甲'), '乙': row('乙', code)}
                result = compare(base, cur)
                self.assertFalse(result['integrityAndCountGatePassed'])
                self.assertEqual(result['newHardFailures'][code], ['乙'])


if __name__ == '__main__':
    unittest.main()
