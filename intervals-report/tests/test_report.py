import math
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1]), *sys.path]):
    import report


class ReportCalculationsTests(unittest.TestCase):
    def test_report_cutoff_and_week_month_boundaries(self):
        for cutoff, expected in (
            (date(2023, 1, 1), ("2022-12-26", "2023-01-01", "2023-01-01")),
            (date(2024, 1, 31), ("2024-01-29", "2024-01-01", "2024-01-31")),
            (date(2024, 3, 1), ("2024-02-26", "2024-03-01", "2024-03-01")),
            (date(2024, 3, 4), ("2024-03-04", "2024-03-01", "2024-03-04")),
        ):
            with self.subTest(cutoff=cutoff):
                self.assertEqual(report._report_bounds(cutoff), expected)

    def test_tss_total_and_coverage_count_zero_but_exclude_missing(self):
        total = report._period_total([4.25, None, 5.75, 0.0], report._metric, math.fsum)
        self.assertEqual(total, "10.0（3/4 条有数据）")
        self.assertEqual(report._period_total([None], report._metric, math.fsum), "无数据")
        self.assertEqual(report._period_total([0.0, None], report._metric, math.fsum), "0.0（1/2 条有数据）")

    def test_duration_total_and_coverage_use_seconds(self):
        total = report._period_total([900, None, 3600], report._hours)
        self.assertEqual(total, "1.25 小时（2/3 条有数据）")
        self.assertEqual(report._period_total([], report._hours), "无数据")
        self.assertEqual(report._period_total([None, 0], report._hours), "0.00 小时（1/2 条有数据）")

    def test_power_zone_edges_and_time_weighted_percentages(self):
        # Independent watt/second inputs for the zone calculation, with each threshold and its next watt.
        watts = (0, 110, 111, 150, 151, 180, 181, 210, 211)
        seconds = (10, 10, 15, 5, 8, 2, 12, 3, 35)
        zones = report._zone_seconds(zip(watts, seconds, strict=True), 200)
        self.assertEqual(zones, [20, 20, 10, 15, 35])
        fields = report._zone_metrics(zones)
        self.assertEqual(
            [fields[f"z{i}_share"] for i in range(1, 6)], ["20.0%", "20.0%", "10.0%", "15.0%", "35.0%"]
        )
        self.assertEqual(fields["z5_duration"], "35 秒")

    def test_zone_with_zero_seconds_differs_from_no_samples(self):
        fields = report._zone_metrics([1, 0, 2, 0, 0])
        self.assertEqual(fields["z1_share"], "33.3%")
        self.assertEqual(fields["z2_share"], "0.0%")
        self.assertEqual(fields["z2_duration"], "0 秒")
        self.assertEqual(set(report._zone_metrics([0] * 5).values()), {"无数据"})

    def test_ftp_baseline_relative_power_and_target_gap(self):
        args = SimpleNamespace(baseline_ftp_w=180, baseline_weight_kg=75, target_ftp_w=225)
        fields = report._fitness_metrics(210, 70, args)
        self.assertEqual(fields["ftp_change"], "+30.0")
        self.assertEqual(fields["weight_change"], "-5.0")
        self.assertEqual(fields["baseline_w_per_kg"], "2.40")
        self.assertEqual(fields["current_w_per_kg"], "3.00")
        self.assertEqual(fields["w_per_kg_change"], "+0.60")
        self.assertEqual(fields["goal_gap"], "目标 225.0 W，当前减目标 -15.0 W")
        self.assertEqual(
            report._fitness_metrics(230, 70, args)["goal_gap"], "目标 225.0 W，当前减目标 +5.0 W"
        )

    def test_missing_baseline_target_and_current_values(self):
        args = SimpleNamespace(baseline_ftp_w=180, baseline_weight_kg=None, target_ftp_w=None)
        fields = report._fitness_metrics(210, 70, args)
        self.assertEqual(fields["baseline_w_per_kg"], "未提供")
        self.assertEqual(fields["w_per_kg_change"], "未提供完整基线")
        self.assertEqual(fields["goal_gap"], "未提供 FTP 目标")
        fields = report._fitness_metrics(210, None, args)
        self.assertEqual(fields["current_weight"], "无数据")
        self.assertEqual(fields["current_w_per_kg"], "无数据")
        args.target_ftp_w = 225
        fields = report._fitness_metrics(None, 70, args)
        self.assertEqual(fields["ftp_change"], "无数据")
        self.assertEqual(fields["goal_gap"], "目标 225.0 W，缺少当前 FTP，无法计算差距")
