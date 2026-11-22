# KLINE_ANALYSIS — D

來源：`c:\Users\Admin\MyProjects\automax\output\paper_bcd_local\data_c\data\testnet_c_inv_v1\kline_vault_v2_clean`
限制：D 無專屬 vault；借用同環境 paper C 的 1m last OHLC。非 mark；非 D 當下拉取的15m。

## A. 實際持倉路徑（SHORT，1m OHLC）
- 已平且有路徑樣本：10
- MFE% 中位/P25/P75：0.2167 / 0.1342 / 0.3142
- MAE% 中位/P25/P75：1.3995 / 1.2622 / 1.6674
- 曾浮盈卻虧損出場（gave_back）：10 / 10
- 進出場棒可能含持倉外價格 → `boundary_error`（見 kline_rows.json）。

## B. 固定窗（自進場起，順空方向報酬%）
- 5m：有效 n=11；期末順向報酬中位=-0.1134%（不足截止者已 CENSORED，未補造）
- 15m：有效 n=11；期末順向報酬中位=-0.0564%（不足截止者已 CENSORED，未補造）
- 30m：有效 n=11；期末順向報酬中位=-0.4639%（不足截止者已 CENSORED，未補造）
- 60m：有效 n=10；期末順向報酬中位=-0.6797%（不足截止者已 CENSORED，未補造）

## C. 價格觸及統計（持倉內，順向低點；≠可實現勝率）
分母 n=10（已平有路徑）
- 觸及 0.25%：4/10
- 觸及 0.50%：1/10
- 觸及 0.75%：0/10
- 觸及 1.00%：0/10
- 觸及 1.50%：0/10

## 代表案例
- LINKUSDT `3225002bdc` reason=SL+VOL_SPIKE net=-4.1618 hold=2532.3s MFE%=0.3217 MAE%=1.6919 boundary=True
- XLMUSDT `691d616484` reason=SL net=-3.5650 hold=66.2s MFE%=0.1120 MAE%=1.3116 boundary=True
- BNBUSDT `39934a6a37` reason=SL+VOL_SPIKE net=-3.4773 hold=140.0s MFE%=0.0968 MAE%=1.2458 boundary=True
- 未平 HYPEUSDT entry=2026-11-21T20:15:34.261048+08:00 acct_mfe=0.2293199999999848 acct_mae=-2.706479999999995 path_MFE%=0.1060（估價至窗口結束；**非已實現**）

## 同棒 TP/SL
多數已平持倉極短；若同棒高低同時跨越 TP/SL 價位則標 AMBIGUOUS（見各列 path_ambiguity）。無 tick → 不假設有利先發生。

明細：`kline_rows.json`