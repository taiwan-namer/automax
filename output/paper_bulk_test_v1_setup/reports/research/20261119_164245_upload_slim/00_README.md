# 策略研究資料包 — paper_bulk_test_v1

## 範圍
- profile: `paper_bulk_test_v1`（不含其他預設 paper 帳戶）
- 時區：Asia/Taipei
- 建置時間（資料截止）：`2026-11-19T16:42:48.320125+08:00`
- 觀測窗（fills）：`2026-11-19T12:47:07+08:00` → `2026-11-19T16:42:11+08:00`
- 權益快照窗：`2026-11-19T12:38:41+08:00` → `2026-11-19T16:42:11+08:00`
- 日界日鍵（帳戶）：`2026-11-19`
- **本日為部分日**（仍在交易中；非午夜收盤完整日）

## 關聯鍵
- `trade_id`：成交／事件／部分退出主鍵
- `run_id`：執行實例
- `signal_key` / `signal_open_time`：訊號去重鍵（存在才填）
- `scan_batch_id`：僅新版（C）掃描批次有紀錄；舊版為空
- `batch_id`（manage）：管理批次

## 檔案用途
| 檔案 | 用途 |
|------|------|
| 00_README.md | 本說明 |
| 00_source_inventory.json | 來源盤點 |
| 01_strategy_spec.md | 策略／風控／管理規格（依 sealed） |
| 02_version_timeline.csv | 版本與切換時間線 |
| 03_trades.csv | 一列一 trade |
| 04_trade_events.csv | 逐筆 OPEN/PARTIAL/CLOSE（可證實） |
| 05_candidates.csv | 決策／候選／拒絕 |
| 06_klines.csv | 1m 研究副本 |
| 06_klines_issues.json | K線衝突／異常 |
| 07_execution_quality.csv | 管理／執行品質 |
| 08_account_reconciliation.md | A/B/C/D 帳戶核對 |
| 09_research_summary.md | 描述性診斷 |
| 10_data_gaps.md | 缺口清單 |
| MANIFEST.json | 雜湊與產生指令 |
| _snapshot/ | 建置時帳戶與 fills 研究副本 |

## 原始觀測 vs 離線計算
- **原始觀測**：fills、decision_trace、manage_events、quote_events、account_state 快照、vault CSV、sealed 原始碼
- **離線計算**：trade 聚合淨損益、initial_risk 估計、version_bucket、摘要統計、K線 OHLC 合理性檢查
- **無法取得**：盤中精確止損先後、未記錄的完整掃描池成員、舊版 manage complete 間隔、未聯網補歷史

## 建議先上傳（精簡）
1. `00_README.md`
2. `01_strategy_spec.md`
3. `09_research_summary.md`
4. `10_data_gaps.md`
5. `03_trades.csv`
6. `05_candidates.csv`（若過大可先抽有 `final_action=OPEN|BLOCKED` 子集；完整版在包內）
7. `02_version_timeline.csv`
8. `08_account_reconciliation.md`

## 安全讀取
- 持續寫入 JSONL：讀取當下檔案長度；**略過未完整尾行**；見 MANIFEST truncated_tails
- 未修改 vault／account／sealed／運行程式
