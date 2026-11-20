# 09 研究摘要（描述性；不調參、不部署）

建置：`2026-11-19T16:42:48.320125+08:00`｜部分日｜profile `paper_bulk_test_v1`

## 樣本
- fills 去重後：125 筆事件；trade_id 數：60
- 完整交易：58；未平倉：2
- 按開倉版本桶：{
  "A_pre_mgmt_dl2000": {
    "n_complete": 52,
    "net_sum": -1905.9625146149754,
    "fee_sum": 520.1335227635914,
    "win_rate": 0.09615384615384616,
    "payoff_ratio_avgWin_over_absAvgLoss": 1.9595101241488744,
    "long_n": 28,
    "short_n": 24
  },
  "B_mgmt_reliability": {
    "n_complete": 2,
    "net_sum": -106.86156144186978,
    "fee_sum": 20.01633185387,
    "win_rate": 0.0,
    "payoff_ratio_avgWin_over_absAvgLoss": null,
    "long_n": 1,
    "short_n": 1
  },
  "C_dl5000_batchside1": {
    "n_complete": 4,
    "net_sum": 76.9714760448048,
    "fee_sum": 40.104677593196996,
    "win_rate": 0.5,
    "payoff_ratio_avgWin_over_absAvgLoss": 1.8899728642186466,
    "long_n": 3,
    "short_n": 1
  },
  "ALL_COMPLETE": {
    "n_complete": 58,
    "net_sum": -1935.8526000120405,
    "fee_sum": 580.2545322106585,
    "win_rate": 0.1206896551724138,
    "payoff_ratio_avgWin_over_absAvgLoss": 1.8637028453080156,
    "long_n": 32,
    "short_n": 26
  }
}

## 同批同向（僅 C 可觀測）
- SCAN_BATCH_RANK 條目數：8
- 含 scan_batch_id 的 SIGNAL_EVAL：5
- SAME_SCAN_BATCH_SIDE_LIMIT 拒絕：0（樣本可能仍不足）
- 空候選批次屬正常（無合格訊號）≠ 限制未部署


## 分布備註
- LONG/SHORT 見各桶 long_n/short_n
- 時段／標的分布請用 `03_trades.csv` 自行分箱；本摘要不外推全日期望
- 同時段同向相關性：樣本少，僅可描述共現，不可作因果

## 執行 vs 策略
- B 起有 `MANAGE_BATCH_COMPLETE`；A 早期若缺事件 → **管理完成間隔不可計算**
- 切換空窗見 `02_version_timeline.csv`（stop→stable_start），**不是** quote→complete 精確空窗
- 失敗交易是否伴隨管理異常：請 join `03_trades.csv` 與 `07_execution_quality.csv`（同時間窗）；本輪不做因果斷言

## K線 MFE/MAE
- **未在本包預先計算路徑依賴的精確 MFE/MAE**（避免 OHLC 捏造進出場先後）
- 若外部用 `06_klines.csv` 估區間極值，必須標記為 K線區間估計，且進出場所在 1m bar 內先後不可知

## 禁止事項（分析約束）
- 跳過候選 ≠ 成交
- 進場後報酬 ≠ 進場特徵
- 不同版本／切換空窗混算需分列
