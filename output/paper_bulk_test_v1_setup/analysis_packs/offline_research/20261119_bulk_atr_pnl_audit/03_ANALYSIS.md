# 舊 paper_bulk_test_v1｜約3600U虧損逐筆歸因（只讀）

禁止混入 `paper_reverse_fixed_exit_v1`。未改策略／參數／帳戶。

## 一、虧損範圍確認
- 帳戶路徑：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1`
- 「約3600」來源：日報 C 指標 `sum_net_u≈-3604.689`（`analysis_packs/daily_reports/2026-11-19`）
- 口徑：**完整 trade_id 且最終 CLOSE 落在報告窗**的已實現淨損益合計（含OPEN費組件＋PARTIAL/CLOSE net，不重扣）
- 時區：Asia/Taipei；報告窗：`2026-11-18T20:15:00+08:00` ≤ t < `2026-11-19T20:15:00+08:00`（交易日標籤 2026-11-19）
- 實際覆蓋：成交自約 12:38 起，**非**自 11-18 20:15 起的完整窗
- 本次核對 C：n=106 sum_net=-3604.689008；與報告差=0.000000e+00
- 帳戶 `realized_net_u`=-3614.688987623117；相對 C 差≈-10.0000（約等於未平倉開倉費合計 10.0000）
- 權益／現金變化含未實現與保證金占用，**≠** 純已實現 -3604
- 涵蓋 run_id：['20261119T124504_79a79b4d', '20261119T131629_436e6cae', '20261119T132017_bd1972b2', '20261119T155834_00889cc1', '20261119T161354_5cb8369a']

## 舊策略出場設定（不得套用新反向1ATR TP）
- 初始止損：約 **1.5×入場ATR**（fills/initial_stop 核對）
- **無固定止盈**；路徑含 PARTIAL_TP → BE → TRAILING_STOP，或直接 INITIAL_STOP
- 初始TP欄位：一律 NA（來源缺漏／策略未設）

## 三、彙總（C窗完整平倉）
### C_window_all
- n=106 贏=10 輸=96 平=0 勝率=0.09433962264150944
- sum_gross=-2544.5270536207227 sum_fees=1060.1620 sum_net=-3604.6890 PF=0.2083613294387977
- avg/med win=94.87633965679935/90.18876716803872；avg/med loss=-47.431795881712304/-37.094318291331
- max win=183.77466775181978 max loss=-184.84502738326847
- 樣本損益兩平勝率≈0.33330347349590733（非未來保證）
### all_complete
- n=106 贏=10 輸=96 平=0 勝率=0.09433962264150944
- sum_gross=-2544.5270536207227 sum_fees=1060.1620 sum_net=-3604.6890 PF=0.2083613294387977
- avg/med win=94.87633965679935/90.18876716803872；avg/med loss=-47.431795881712304/-37.094318291331
- max win=183.77466775181978 max loss=-184.84502738326847
- 樣本損益兩平勝率≈0.33330347349590733（非未來保證）
### C_window_ver_A
- n=53 贏=5 輸=48 平=0 勝率=0.09433962264150944
- sum_gross=-1415.8491530833835 sum_fees=530.1485 sum_net=-1945.9977 PF=0.20504927113137628
- avg/med win=100.38997178822365/87.92186972903363；avg/med loss=-50.99890709311478/-42.910530486352236
- max win=183.77466775181978 max loss=-184.84502738326847
- 樣本損益兩平勝率≈0.33687353701250883（非未來保證）
### C_window_ver_B
- n=2 贏=0 輸=2 平=0 勝率=0.0
- sum_gross=-86.84522958799977 sum_fees=20.0163 sum_net=-106.8616 PF=0.0
- avg/med win=None/None；avg/med loss=-53.43078072093489/-53.43078072093489
- max win=None max loss=-72.51935990557052
- 樣本損益兩平勝率≈None（非未來保證）
### C_window_ver_C
- n=51 贏=5 輸=46 平=0 勝率=0.09803921568627451
- sum_gross=-1041.83267094934 sum_fees=509.9971 sum_net=-1551.8298 PF=0.22355841936171883
- avg/med win=89.36270752537504/92.45566460704381；avg/med loss=-43.44876745071743/-33.319769336614414
- max win=106.46320032145974 max loss=-159.34165022631637
- 樣本損益兩平勝率≈0.3271461856630889（非未來保證）

### 前10大虧損（占全部虧損金額）
- 全部虧損合計=-4553.4524；前10合計=-1083.7739；占比=0.2380114769588853
- PIPPINUSDT SHORT INITIAL_STOP net=-184.85 atr%=0.5395766976684879 planned_sl_u=80.93646912560622
- JCTUSDT SHORT INITIAL_STOP net=-159.34 atr%=0.3202083947642132 planned_sl_u=48.03125620809326
- EVAAUSDT LONG INITIAL_STOP net=-101.04 atr%=0.5602166487754832 planned_sl_u=84.03137147656935
- EVAAUSDT LONG INITIAL_STOP net=-92.94 atr%=0.5300958047909212 planned_sl_u=79.51288525282385
- 1000LUNCUSDT LONG INITIAL_STOP net=-92.91 atr%=0.5171772078730422 planned_sl_u=77.57642017797738
- PIPPINUSDT LONG INITIAL_STOP net=-92.48 atr%=0.47519999244465766 planned_sl_u=71.27990618473794
- EVAAUSDT SHORT INITIAL_STOP net=-91.68 atr%=0.5132190656949734 planned_sl_u=76.98107821079002
- ICNTUSDT SHORT INITIAL_STOP net=-91.59 atr%=0.5436023276383876 planned_sl_u=81.54004750615421
- EVAAUSDT LONG INITIAL_STOP net=-89.12 atr%=0.4468320796892699 planned_sl_u=67.02209650391305
- KAVAUSDT SHORT INITIAL_STOP net=-87.83 atr%=0.4582631557404054 planned_sl_u=68.73945574610687

## 四、ATR／波動分組（入場 ATR/價 % 三分位）
- 界線：low≤0.124219% ＜ mid ≤0.256864% ＜ high（僅描述）
### low (<= 0.1242%)
- n=36 avg_notional=9998.129634567455 WR=0.027777777777777776 avg_win=92.45566460704381 avg_loss=-25.443335179893275
- sum_fees=359.90 sum_net=-798.06 avg_planned_sl_u=13.286093885093454
- exits={'INITIAL_STOP': 35, 'TRAILING_STOP': 1}
### mid ((0.1242%, 0.2569%])
- n=35 avg_notional=9999.67084022056 WR=0.11428571428571428 avg_win=117.83685149985648 avg_loss=-40.344691978708276
- sum_fees=349.98 sum_net=-779.34 avg_planned_sl_u=26.89530871980679
- exits={'INITIAL_STOP': 31, 'TRAILING_STOP': 4}
### high (> 0.2569%)
- n=35 avg_notional=9999.925327789171 WR=0.14285714285714285 avg_win=76.99206519230474 avg_loss=-80.40834073360533
- sum_fees=350.28 sum_net=-2027.29 avg_planned_sl_u=58.377248104639456
- exits={'INITIAL_STOP': 30, 'TRAILING_STOP': 5}

### 贏家 vs 輸家
- 贏家 n=10 avg_atr%=0.26885666633782007 avg_notional=9999.9011561044 avg_planned_sl_u=40.32806413804755
- 輸家 n=96 avg_atr%=0.2124765498951134 avg_notional=9999.161678788802 avg_planned_sl_u=31.870398201442786
- 「大波動多止損、小波動多止盈」：見各組 INITIAL_STOP vs TRAILING 計數；不得單憑分組虧損斷言ATR是唯一原因。

### 動態／類止盈路徑後仍虧或近打平（近打平門檻 |net|≤5.0U）
- tp_like n=10；淨虧 n=0；近打平 n=0

## 假設判定
- **A_sl_vs_tp_distance**: 支持（就舊策略結構） — 舊策略無固定TP；出場 INITIAL_STOP=96 / TRAILING_STOP=10；多數以初始止損結束，非對稱風險／報酬在入場時未設固定TP。
- **B_tp_space_vs_fees**: 資料不足／部分支持 — 無固定TP；trail/partial 路徑 n=10，其中淨虧 n=0，|net|≤5.0 n=0。無法用1ATR TP口徑評估舊單。
- **C_fixed_notional_inconsistent_risk**: 支持 — 名義≈1萬固定；planned_sl_gross_u min=7.58 max=84.03 median=25.64（同1.5ATR但金額差很大）
- **D_signal_winrate_insufficient**: 支持 — C窗勝率=9.434%；樣本損益兩平勝率≈0.33330347349590733
- **E_other_exec**: 資料不足 — 有部分止盈/移動止損路徑；滑價在成交價內。未另做執行異常審計。

## 未平倉（獨立）
- FETUSDT LONG tid=`20261119T161354_5cb8369a:FETUSDT:1bd4e0c0cc` open_fee=4.9999805961599995 atr%=0.13746067473341292 — **非已完成虧損單**
- 1000PEPEUSDT SHORT tid=`20261119T161354_5cb8369a:1000PEPEUSDT:f30831260d` open_fee=4.99999895056992 atr%=0.3649317103073433 — **非已完成虧損單**

## 檔案
- 逐筆完整：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/offline_research/20261119_bulk_atr_pnl_audit/01_complete_trades.csv`
- 未平倉：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/offline_research/20261119_bulk_atr_pnl_audit/02_open_positions.csv`
- 本報告：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/offline_research/20261119_bulk_atr_pnl_audit/03_ANALYSIS.md`
