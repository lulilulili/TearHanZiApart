import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tools.verify_batch import add_root_to_path, choose_jobs, select_chars


class VerifyBatchTests(unittest.TestCase):
    def test_sample_stride_is_deterministic(self):
        chars = "abcdefghij"
        self.assertEqual(select_chars(chars, "sample", 3), "adgj")
        self.assertEqual(select_chars(chars, "full", 3), chars)

    def test_char_file_strips_whitespace_without_deduplicating_order(self):
        self.assertEqual(select_chars("甲\n乙 甲\n", "file", 1), "甲乙甲")

    def test_jobs_are_capped_for_repeatable_windows_runs(self):
        self.assertEqual(choose_jobs(16, 0), 8)
        self.assertEqual(choose_jobs(4, 2), 2)
        self.assertEqual(choose_jobs(1, 0), 1)

    def test_external_root_is_importable(self):
        root = os.path.dirname(os.path.dirname(__file__))
        add_root_to_path(root)
        self.assertEqual(sys.path[0], root)


if __name__ == "__main__":
    unittest.main()
