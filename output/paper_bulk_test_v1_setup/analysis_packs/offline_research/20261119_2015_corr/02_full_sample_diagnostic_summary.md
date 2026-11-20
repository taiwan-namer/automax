# 2. 全樣本診斷摘要（106 完整＋未平倉分列）

## 全體完整交易（CLOSE∈結算窗）
- n=106
- sum_net≈-3604.6890｜sum_fees≈1060.1620｜sum_exit_gross≈-2544.5271
- win_rate=0.0943（勝10／負96）
- avg_win≈94.8763｜avg_loss≈-47.4318
- profit_factor≈0.2084
- 未平倉（不入勝率）：2 → FETUSDT LONG, 1000PEPEUSDT SHORT

## 分組（詳見 CSV）

### by_open_version
- A_pre_mgmt_dl2000: n=53 net≈-1945.9976815283915 wr=0.09433962264150944 pf=0.20504927113137628
- B_mgmt_reliability: n=2 net≈-106.86156144186978 wr=0.0 pf=0.0
- C_dl5000_batchside1: n=51 net≈-1551.8297651061264 wr=0.09803921568627451 pf=0.22355841936171883

### by_cross_version
- cross=False: n=103 net≈-3436.349220186075 wr=0.0970873786407767 pf=0.21636009824310592
- cross=True: n=3 net≈-168.33978789031394 wr=0.0 pf=0.0

### by_side
- LONG: n=59 net≈-1618.256631010618 wr=0.1016949152542373 pf=0.27745215296832926
- SHORT: n=47 net≈-1986.4323770657697 wr=0.0851063829787234 pf=0.1414845278756204

### by_exit_reason
- INITIAL_STOP: n=96 net≈-4553.452404644381 wr=0.0 pf=0.0
- TRAILING_STOP: n=10 net≈948.7633965679935 wr=1.0 pf=inf

## 特徵
- 來源：決策當時 SIGNAL_EVAL（vol_x/spread/atr/range）＋進場前已收盤 1m（實體／影線／事前3棒）
- 缺 SIGNAL_EVAL 連結或缺K：見 CSV `feature_missing`／`kline_conflict_or_missing`（n_kline_flag≈0）
- 進場後 1/3/5m 報酬＝**結果標籤 only**
- MFE/MAE：關閉倉無保留輪詢路徑 → **不報告精確路徑 MFE/MAE**

## 檔案
- `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/offline_research/20261119_2015_corr/02_full_sample_trades.csv`
- `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/offline_research/20261119_2015_corr/02_open_positions.csv`
- `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/offline_research/20261119_2015_corr/02_group_summary.csv`
