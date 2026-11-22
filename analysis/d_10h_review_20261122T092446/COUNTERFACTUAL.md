# COUNTERFACTUAL — D（有限度）

## 範圍
固定進場局部診斷（僅已平10筆）— 不代表改出場後完整策略

| 項 | 值 |
|----|----|
| 實際已平 cycle 淨合計 | -33.5124 U |
| CF0 僅毛損益（去費用） | -30.5087 U |
| 費用拖累（進出場費） | 3.0037 U |

## 候選（最多三個單一變因）

### CF1: 僅放寬 SL 觸發至 5U gross（現行 3U）
- 理由：10/10 已平皆虧；多數 SL 或 SL+VOL；MAE 路徑常剛過 3U
- 可計算性：PARTIAL — 無 tick；用1m高點代理是否觸及5U，延後出場會改後續機會故非完整績效
- 結果：N/A

### CF2: 取消 VOL_SPIKE 出場，僅留 TP/SL
- 理由：多筆標 VOL_SPIKE／SL+VOL_SPIKE，可能在不利波動放大時與 SL 同輪觸發
- 可計算性：PARTIAL — 同棒順序不明；需 paper 對照
- 結果：N/A

### CF3: 時間退出：持倉超過 30 分鐘強制平
- 理由：本批已平持倉多在1–2分鐘內結束，時間退出對本樣本幾乎無影響；留待更長持倉樣本
- 可計算性：False
- 結果：本樣本不适用／效應≈0

## 限制
十小時（實約6.5h）為探索樣本；不可宣稱參數已驗證；不可建議加碼或上實盤

- 無歷史 bid/ask 排队 → 不宣稱可精準重建成交。
- 改出場會改變後續槽位／冷卻／進場機會。

## 逐筆 CF0
- 2026-11-21T1433_040b980f:ADAUSDT:ed9a628238: actual_net=-3.3072 gross_only=-3.0058
- 2026-11-21T1433_040b980f:LINKUSDT:3225002bdc: actual_net=-4.1618 gross_only=-3.8599
- 2026-11-21T1433_040b980f:AVAXUSDT:c987a1c7a4: actual_net=-2.6026 gross_only=-2.3070
- 2026-11-21T1433_040b980f:DOGEUSDT:9667e15363: actual_net=-3.4030 gross_only=-3.1015
- 2026-11-21T1433_040b980f:WLDUSDT:5aea42e042: actual_net=-3.4272 gross_only=-3.1257
- 2026-11-21T1433_040b980f:BNBUSDT:39934a6a37: actual_net=-3.4773 gross_only=-3.1808
- 2026-11-21T1433_040b980f:XLMUSDT:691d616484: actual_net=-3.5650 gross_only=-3.2634
- 2026-11-21T1433_040b980f:XRPUSDT:d03bc3ddb1: actual_net=-3.2687 gross_only=-2.9673
- 2026-11-21T1433_040b980f:1000SHIBUSDT:2e4b743db4: actual_net=-2.8523 gross_only=-2.5510
- 2026-11-21T1433_040b980f:SOLUSDT:b3a67523aa: actual_net=-3.4474 gross_only=-3.1464
