from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StoreWriterSerializationTest(unittest.TestCase):
    def test_core_and_dynamic_collectors_share_one_writer_queue(self):
        core = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        dynamic = (ROOT / ".github/workflows/dynamic-candidate-runtime.yml").read_text(encoding="utf-8")

        expected = "group: market-data-collector"
        self.assertIn(expected, core)
        self.assertIn(expected, dynamic)
        self.assertIn("cancel-in-progress: false", core)
        self.assertIn("cancel-in-progress: false", dynamic)


if __name__ == "__main__":
    unittest.main()
