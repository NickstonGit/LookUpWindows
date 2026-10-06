import unittest

from change_logic import GridComparator


class GridComparatorTests(unittest.TestCase):
    def test_capture_failure_invalidates_baseline(self):
        comparator = GridComparator()
        hwnd = 100
        self.assertEqual(comparator.compare(hwnd, bytes([30, 30])).status, "baseline")
        self.assertEqual(comparator.compare(hwnd, None).status, "capture_failed")
        # A healthy frame after the failure must re-baseline instead of being
        # compared with the stale pre-failure sample.
        self.assertEqual(comparator.compare(hwnd, bytes([220, 220])).status, "baseline")

    def test_blank_frame_invalidates_baseline(self):
        comparator = GridComparator()
        hwnd = 101
        self.assertEqual(comparator.compare(hwnd, bytes([80, 80])).status, "baseline")
        self.assertEqual(comparator.compare(hwnd, bytes([0, 1])).status, "blank_frame")
        self.assertEqual(comparator.compare(hwnd, bytes([90, 90])).status, "baseline")

    def test_healthy_frames_report_score_and_changed_fraction(self):
        comparator = GridComparator(cell_delta_threshold=24)
        hwnd = 102
        comparator.compare(hwnd, bytes([10, 10, 10, 10]))
        result = comparator.compare(hwnd, bytes([10, 40, 10, 60]))
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.changed_fraction, 0.5)
        self.assertGreater(result.score or 0.0, 0.0)

    def test_forget_is_per_window(self):
        comparator = GridComparator()
        comparator.compare(1, bytes([10]))
        comparator.compare(2, bytes([20]))
        comparator.forget(1)
        self.assertEqual(comparator.compare(1, bytes([30])).status, "baseline")
        self.assertEqual(comparator.compare(2, bytes([20])).status, "ok")


if __name__ == "__main__":
    unittest.main()
