import unittest
from datetime import datetime, timedelta
from pathlib import Path

from jason_checks.intraday_sparkline import SEOUL, build_fallback_sparkline, build_today_sparkline


class IntradaySparklineTests(unittest.TestCase):
    def _minute_rows(self):
        start = datetime(2026, 6, 9, 9, 0, tzinfo=SEOUL)
        end = datetime(2026, 6, 9, 14, 50, tzinfo=SEOUL)
        rows = []
        i = 0
        cur = start
        while cur <= end:
            rows.append({
                "stck_bsop_date": cur.strftime("%Y%m%d"),
                "stck_cntg_hour": cur.strftime("%H%M%S"),
                "stck_prpr": str(10000 + i),
            })
            i += 1
            cur += timedelta(minutes=1)
        return rows

    def test_today_intraday_one_minute_rows_build_five_minute_sparkline(self):
        chart = build_today_sparkline(
            self._minute_rows(),
            prev_close=9900,
            interval_minutes=5,
            max_points=80,
            now=datetime(2026, 6, 9, 14, 50, tzinfo=SEOUL),
        )
        self.assertEqual(chart["status"], "OK")
        self.assertEqual(chart["sparkline_range"], "today")
        self.assertEqual(chart["sparkline_tf"], "5m")
        self.assertEqual(chart["sparkline_source"], "kis_intraday")
        self.assertGreaterEqual(chart["sparkline_point_count"], 60)
        self.assertLessEqual(chart["sparkline_point_count"], 80)
        self.assertEqual(chart["points"][0]["time"], "2026-06-09T09:04:00+09:00")
        self.assertEqual(chart["points"][-1]["time"], "2026-06-09T14:50:00+09:00")
        self.assertIn("pct_from_open", chart["points"][-1])
        self.assertIn("pct_from_prev_close", chart["points"][-1])

    def test_downsample_caps_point_count(self):
        chart = build_today_sparkline(
            self._minute_rows(),
            interval_minutes=1,
            max_points=70,
            now=datetime(2026, 6, 9, 14, 50, tzinfo=SEOUL),
        )
        self.assertEqual(chart["sparkline_range"], "today")
        self.assertEqual(chart["sparkline_point_count"], 70)
        self.assertEqual(chart["points"][0]["time"], "2026-06-09T09:00:00+09:00")
        self.assertEqual(chart["points"][-1]["time"], "2026-06-09T14:50:00+09:00")

    def test_fallback_is_marked_when_intraday_missing(self):
        fallback = build_fallback_sparkline(
            {
                1780976400000: 10000,
                1780976700000: 10010,
                1780977000000: 10020,
            },
            price=10020,
            change_pct=1.2,
        )
        self.assertTrue(fallback["sparkline_is_fallback"])
        self.assertEqual(fallback["sparkline_range"], "recent")
        self.assertEqual(fallback["sparkline_tf"], "recent")

    def test_fixed_one_minute_button_label_is_removed(self):
        template = Path("src/jason_checks/web/templates/index.html").read_text(encoding="utf-8")
        self.assertNotIn(">1m</button>", template)
        self.assertIn("Today 5m", template)


if __name__ == "__main__":
    unittest.main()
