# 训练分析报告

- 报告周期：{{report_period}}
- 运动范围：{{sport_scope}}
- 数据覆盖：{{data_coverage}}

## 本周结论

- 训练负荷：{{weekly_load_summary}}
- 进步趋势：{{progress_summary}}
- 风险与恢复：{{risk_summary}}
- 下周建议：{{next_week_summary}}

## 训练量与负荷趋势

| 指标 | 本周 | 本月 |
| --- | --- | --- |
| 训练时长 | {{weekly_duration}} | {{monthly_duration}} |
| TSS 合计 | {{weekly_tss}} | {{monthly_tss}} |

CTL、ATL、TSB 为导入的个人负荷指标，不按运动筛选。

{{load_trends}}

## FTP、体重与目标差距

| 指标 | 基线 | 当前 | 变化 |
| --- | --- | --- | --- |
| FTP（W） | {{baseline_ftp}} | {{current_ftp}} | {{ftp_change}} |
| 体重（kg） | {{baseline_weight}} | {{current_weight}} | {{weight_change}} |
| 相对功率（W/kg） | {{baseline_w_per_kg}} | {{current_w_per_kg}} | {{w_per_kg_change}} |

- 训练目标与当前差距：{{goal_gap}}
- 当前值来源：{{metric_dates}}

## 骑行强度分布

分区使用的 FTP（W）：{{zone_ftp}}

{{zone_coverage}}

按当前 FTP 划分，时长占比以有功率样本的秒数为分母。

| 功率区间 | 时长 | 占比 |
| --- | --- | --- |
| Z1（≤55%） | {{z1_duration}} | {{z1_share}} |
| Z2（>55% 至 75%） | {{z2_duration}} | {{z2_share}} |
| Z3（>75% 至 90%） | {{z3_duration}} | {{z3_share}} |
| Z4（>90% 至 105%） | {{z4_duration}} | {{z4_share}} |
| Z5（>105%） | {{z5_duration}} | {{z5_share}} |

## 下周训练建议

{{next_week_recommendations}}
