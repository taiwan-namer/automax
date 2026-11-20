# K 線路徑 + 300U 分批離線研究（2026-11-20）

新目錄，**不覆蓋** `limit0p5_exit_entry_replay_20261120`。

## 重跑

```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/kline_path_and_pyramid300_20261120
python3 run_all.py
```

## 內容

- **Study A**：全成交路徑 MFE/MAE／里程碑 + 事前已收盤 K 特徵 + 至多 3 候選  
- **Study B**：300U 保證金×10 名義三批加倉；R1/R2 × T1/T2/T3；Pstop 取代 SL75；公平單批／原10k 基準

## 輸出

| 檔 | 說明 |
|----|------|
| `MASTER_ANSWERS.md` | 主結論 |
| `out_B/` `out_C/` | 分策略報告與 CSV |
| `A_path_trades.csv` | 逐筆路徑 |
| `A_candidates.json` | 候選 keep/drop 完整 ID |
| `B_pyramid_trades.csv` | 分批逐筆（含樂觀／保守） |
| `B_pyramid_summary.csv` | 分批彙總 |
| `B_baselines.csv` | 原10k／單批3000／名義300 |
| `B_stress_R1_T1.csv` | 費滑壓力 |

唯讀；不改 runtime／帳戶。
