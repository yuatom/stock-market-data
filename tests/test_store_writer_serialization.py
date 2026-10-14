from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StoreWriterSerializationTest(unittest.TestCase):
    def test_core_and_dynamic_collectors_share_one_writer_queue(self):
        core = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        dynamic = (ROOT / ".github/workflows/dynamic-candidate-runtime.yml").read_text(encoding="utf-8")
        readiness = (ROOT / ".github/workflows/extended-hours-readiness.yml").read_text(encoding="utf-8")

        expected = "group: market-data-collector"
        self.assertIn(expected, core)
        self.assertIn(expected, dynamic)
        self.assertIn("cancel-in-progress: false", core)
        self.assertIn("cancel-in-progress: false", dynamic)
        for workflow in (core, dynamic, readiness):
            self.assertIn("group: market-data-collector", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
            self.assertIn("queue: max", workflow)


if __name__ == "__main__":
    unittest.main()
