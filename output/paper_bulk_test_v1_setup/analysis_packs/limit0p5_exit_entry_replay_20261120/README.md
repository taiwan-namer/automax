# 限價 0.5% B／C 離線回放（2026-11-20）

唯讀離線分析。不修改 runtime／配置／帳戶／原始 fills／金庫。

## 重跑

```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/limit0p5_exit_entry_replay_20261120
python3 run_replay.py
```

依賴：Python 3.9+、pandas（本機已有）。

## 輸出

| 路徑 | 內容 |
|------|------|
| `out_B/REPORT.md` | B 主報告 |
| `out_C/REPORT.md` | C 主報告 |
| `MASTER_ANSWERS.md` | 五題直接回答 |
| `out_*/matrix_summary.csv` | 16 組×保守／樂觀 |
| `out_*/matrix_trades.csv` | 逐筆反事實 |
| `out_*/conversion.csv` | 原贏變輸／原輸改善 |
| `out_*/milestones.csv` | 反彈／延伸路徑 |
| `out_*/entry_*.csv` | 進場距離實驗 |
| `out_*/meta.json` | 假設與基準核對 |

## 主截止

`2026-11-20T08:16:25+08:00`（與日報 asof 對齊）。  
僅納入 limit0p5 runs：B `040855`+`042025`；C `040855`+`042025`。

## 模型要點

- 第一階段：固定實際成交進場，改 TP／SL 後用 **1m OHLC** 重走退出。
- 同根雙觸：保守=SL、樂觀=TP。
- 費率 5bps／邊；出場不利滑價 2bps（套在障礙價）。
- 限價進場實驗：觸價≠保證全量；同根觸價+作廢→保守作廢；**非 maker 費**。
- 此為獨立交易敏感度，不是資金約束完整帳戶回測。
