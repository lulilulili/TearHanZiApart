import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "viewer" / "js" / "loadingStages.js"


class LoadingStagesTest(unittest.TestCase):
    def run_node(self, source):
        proc = subprocess.run(
            ["node", "-e", source],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def test_font_and_decompose_plans_explain_real_pipeline_stages(self):
        data = self.run_node(
            f"const m=require({json.dumps(str(MODULE))});"
            "console.log(JSON.stringify({font:m.LOADING_STAGE_PLANS.font,"
            "decompose:m.LOADING_STAGE_PLANS.decompose}));"
        )
        self.assertGreaterEqual(len(data["font"]), 4)
        self.assertGreaterEqual(len(data["decompose"]), 5)
        self.assertTrue(any("标准笔画" in s["title"] for s in data["font"]))
        self.assertTrue(any("连通组" in s["detail"] for s in data["decompose"]))
        self.assertTrue(any("布尔" in s["title"] for s in data["decompose"]))

    def test_stage_selection_advances_and_stops_at_last_stage(self):
        data = self.run_node(
            f"const m=require({json.dumps(str(MODULE))});"
            "const p=m.LOADING_STAGE_PLANS.decompose;"
            "console.log(JSON.stringify([0,2200,999999].map(ms=>m.stageAt(p,ms).index)));"
        )
        self.assertEqual(data[0], 0)
        self.assertGreater(data[1], data[0])
        self.assertEqual(data[2], len(self.run_node(
            f"const m=require({json.dumps(str(MODULE))});"
            "console.log(JSON.stringify(m.LOADING_STAGE_PLANS.decompose));"
        )) - 1)

    def test_loading_script_is_loaded_before_core(self):
        html = (ROOT / "viewer" / "charStrokeLab.html").read_text(encoding="utf-8")
        self.assertLess(html.index("js/loadingStages.js"), html.index("js/core.js"))


if __name__ == "__main__":
    unittest.main()
