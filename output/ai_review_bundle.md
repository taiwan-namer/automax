# AI 復盤精簡報告（ai_review_bundle）

> 產生時間：2026-11-19T03:26:53+08:00
> run_id：`20261118T221030_4178e9e3`｜strategy_version：`fade_long_only_v2`｜config_hash：`257ef0a2d47262d8`

> 不同時間／訊號環境的版本績效**不是**嚴格 A/B 測試。

## A. 資料品質

- 詳見 `output/execution_audit.md`
- 成交模擬：1m OHLC；同根 TP+SL → SL 優先；TRAIL 用 close
- 成本：round-trip fee=0.001（平倉一次計入）
- 滑價模型：NONE

## B. 績效（依門派／方向）

- 平倉筆數：143（版本標記：歷史皆 LEGACY_UNKNOWN）
- 勝/負：51/92｜勝率 35.7%
- 毛利 -10.8369｜費用 14.3000｜淨損益 -25.1369｜每筆期望 -0.1758
- 平均獲利 +0.7702｜平均虧損 -0.7002｜PF 0.6097716826174944
- 已實現累積淨利曲線最大回撤（僅 cum net，**未還原100→500本金**）：-32.0765 U
- 含浮盈回撤：UNKNOWN（歷史無每分鐘權益快照檔）
- 平均持倉：6.96 m
- 出場原因：{'SL_HIT': 68, 'TIME_EXIT': 43, 'TP_HIT': 29, 'TRAIL_PROFIT': 3}｜加總=143 vs 平倉=143
- 幣種數：59
- 最大單筆獲利 +3.6261｜占總獲利比 9.2%
- 排除最佳一筆後淨損益：-28.7630

### 分組：門派 × 方向

| school | side | n | win | net |
|---|---|---:|---:|---:|
| MEAN_REV_15M | LONG | 1 | 0 | -1.4377 |
| SPIKE_FADE_1M | LONG | 28 | 9 | -6.8674 |
| SPIKE_FADE_1M | SHORT | 25 | 13 | +3.1799 |
| SPIKE_MOMO_1M | LONG | 24 | 9 | +0.0489 |
| SPIKE_MOMO_1M | SHORT | 28 | 9 | -8.7852 |
| SWING_REV_15M | LONG | 15 | 5 | -3.6500 |
| SWING_REV_15M | SHORT | 19 | 5 | -6.4000 |
| TREND_FOLLOW_15M | LONG | 2 | 0 | -2.6000 |
| TREND_FOLLOW_15M | SHORT | 1 | 1 | +1.3746 |

## C. 逐筆診斷（摘要）

- 完整逐筆請見 `output/live_trades_log.csv` 與新 `trade_audit_*.jsonl`（新單才有完整進場快照）。
- 歷史單缺少訊號K open_time／量倍分母／分流快照 → 欄位為 UNKNOWN，禁止回填。
- 不可僅憑 MFE/MAE 宣稱換參數一定能賺。

## D. 錯失機會與分流（含 MMTUSDT）

| symbol | 24h% | primary | status | 曾交易 | 決策可追溯 |
|---|---:|---|---|---|---|
| MMTUSDT | 74.768 | SPIKE_MOMO_1M | ROUTED | Y | 歷史缺 decision_trace → 無法完整追溯 |
| PIPPINUSDT | 86.096 | SPIKE_MOMO_1M | ROUTED | Y | 歷史缺 decision_trace → 無法完整追溯 |
| PIEVERSEUSDT | 59.657 | WATCH_ONLY | WATCH_ONLY | N | 歷史缺 decision_trace → 無法完整追溯 |
| ICNTUSDT | 40.533 | WATCH_ONLY | WATCH_ONLY | Y | 歷史缺 decision_trace → 無法完整追溯 |
| ZKCUSDT | 25.276 | SPIKE_MOMO_1M | ROUTED | Y | 歷史缺 decision_trace → 無法完整追溯 |
| PHAROSUSDT | 24.95 | SPIKE_MOMO_1M | ROUTED | Y | 歷史缺 decision_trace → 無法完整追溯 |
| SOONUSDT | 20.725 | SPIKE_MOMO_1M | ROUTED | Y | 歷史缺 decision_trace → 無法完整追溯 |
| HUMAUSDT | 19.837 | SPIKE_MOMO_1M | ROUTED | N | 歷史缺 decision_trace → 無法完整追溯 |

- 行情漲幅 ≠ 當時可交易收益；未使用未來資料選入場點。
- 決策追蹤自本版起寫入 `decision_trace_YYYYMMDD.jsonl`。

## E. 附件索引

- `[OK]` `output/execution_audit.md`
- `[OK]` `output/run_manifest.json`
- `[OK]` `output/live_trades_log.csv`
- `[OK]` `output/daily_review_for_ai.md`
- `[OK]` `output/kline_slices_for_ai.md`
- `[OK]` `output/active_routing_plan.json`
- `[OK]` `output/decision_trace_20261119.jsonl`
- `[OK]` `output/trade_audit_20261119.jsonl`
- `[OK]` `output/kline_archives/`

