import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluate import markdown, summarize, wilson  # noqa: E402


def run(incident, repeat, correct, fault="crash_loop", difficulty="L1", stop="submitted"):
    return {"incident": incident, "repeat": repeat, "difficulty": difficulty, "stop_reason": stop,
            "diagnosis": {"component": "inventory", "fault": fault} if stop == "submitted" else None,
            "ground_truth": {"component": "inventory", "fault": fault},
            "score": {"correct": correct, "component_ok": correct, "fault_ok": correct},
            "tool_calls": [{}] * 4, "prompt_tokens": 1000, "completion_tokens": 100, "seconds": 10.0}


class EvalTest(unittest.TestCase):
    def test_wilson_known_values(self):
        lo, hi = wilson(8, 10)
        self.assertAlmostEqual(lo, 0.490, places=2)
        self.assertAlmostEqual(hi, 0.943, places=2)
        self.assertEqual(wilson(0, 0), (0.0, 0.0))

    def test_summary_groups_and_variance(self):
        runs = [run("A", 1, True), run("B", 1, True, difficulty="L2"),
                run("A", 2, True), run("B", 2, False, difficulty="L2", stop="max_steps")]
        s = summarize(runs)
        self.assertEqual((s["runs"], s["incidents"], s["accuracy"]), (4, 2, 0.75))
        self.assertEqual(s["by_difficulty"]["L2"], {"correct": 1, "runs": 2, "accuracy": 0.5})
        self.assertEqual(s["accuracy_std_across_repeats"], 0.25)
        self.assertEqual(s["no_diagnosis"], 1)
        self.assertIn("- B r2: said -/-, truth inventory/crash_loop (max_steps)", markdown(s, "m", runs))


if __name__ == "__main__":
    unittest.main()
