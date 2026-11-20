# 自動策略分派計畫（Active Routing Plan）

> 產生時間：2026-11-19 09:18:09 CST
> 來源股性：`output/coin_personality_summary.csv`
> 總幣數：50｜可交易：17｜觀察：28｜剔除：5

---

## 垃圾桶名單（SKIP｜不盯盤）

| 幣種 | 榜單 | 24h漲跌% | 成交額(萬U) | 量能比 | 剔除原因 |
| --- | --- | --- | --- | --- | --- |
| ICNTUSDT | gainer | 22.37% | 9647.2 | 0.332 | recent_volume_ratio=0.332<0.35（量縮退潮） |
| BULLAUSDT | gainer | 19.43% | 5545.6 | 0.277 | recent_volume_ratio=0.277<0.35（量縮退潮） |
| BLESSUSDT | gainer | 14.49% | 11684.2 | 0.301 | recent_volume_ratio=0.301<0.35（量縮退潮） |
| STABLEUSDT | gainer | 9.44% | 1063.8 | 0.213 | recent_volume_ratio=0.213<0.35（量縮退潮） |
| 龙虾USDT | loser | -5.83% | 2804.2 | 0.272 | recent_volume_ratio=0.272<0.35（量縮退潮） |

---

## 觀察名單（WATCH_ONLY｜不下單）

| 幣種 | 榜單 | 24h漲跌% | 價格位置% | 量能比 | 說明 |
| --- | --- | --- | --- | --- | --- |
| PIPPINUSDT | gainer | 59.96% | 58.9% | 0.563 | 未符任何門派門檻 |
| PIEVERSEUSDT | gainer | 59.94% | 82.6% | 1.001 | 未符任何門派門檻 |
| MMTUSDT | gainer | 58.27% | 56.7% | 0.879 | 未符任何門派門檻 |
| EVAAUSDT | gainer | 32.70% | 93.3% | 0.693 | 未符任何門派門檻 |
| PHAROSUSDT | gainer | 29.31% | 92.9% | 1.151 | 未符任何門派門檻 |
| HUMAUSDT | gainer | 18.04% | 85.3% | 0.987 | 未符任何門派門檻 |
| ZKCUSDT | gainer | 13.61% | 37.5% | 1.811 | 未符任何門派門檻 |
| MAGMAUSDT | gainer | 9.05% | 59.0% | 0.583 | 未符任何門派門檻 |
| TRUSTUSDT | gainer | 8.75% | 79.3% | 1.419 | 未符任何門派門檻 |
| RAVEUSDT | gainer | 8.19% | 78.6% | 0.536 | 未符任何門派門檻 |
| IDOLUSDT | gainer | 7.29% | 74.3% | 0.900 | 未符任何門派門檻 |
| BTWUSDT | gainer | 7.10% | 74.0% | 0.451 | 未符任何門派門檻 |
| BREVUSDT | loser | -25.82% | 5.3% | 0.859 | 未符任何門派門檻 |
| RAREUSDT | loser | -9.59% | 5.3% | 0.509 | 未符任何門派門檻 |
| TLMUSDT | loser | -9.49% | 24.5% | 0.523 | 未符任何門派門檻 |
| SKYAIUSDT | loser | -8.15% | 14.4% | 0.654 | 未符任何門派門檻 |
| 牛来USDT | loser | -8.13% | 19.9% | 1.411 | 未符任何門派門檻 |
| ASTERUSDT | loser | -7.61% | 12.9% | 0.810 | 未符任何門派門檻 |
| BEAMXUSDT | loser | -6.86% | 31.4% | 0.967 | 未符任何門派門檻 |
| MONUSDT | loser | -6.79% | 13.9% | 1.307 | 未符任何門派門檻 |
| MUBARAKUSDT | loser | -6.66% | 18.1% | 0.909 | 未符任何門派門檻 |
| PUMPUSDT | loser | -5.94% | 22.5% | 1.075 | 未符任何門派門檻 |
| RUNEUSDT | loser | -5.67% | 30.0% | 0.754 | 未符任何門派門檻 |
| GRAMUSDT | loser | -5.10% | 22.7% | 0.585 | 未符任何門派門檻 |
| ZECUSDT | loser | -5.08% | 26.4% | 0.731 | 未符任何門派門檻 |
| PHAUSDT | loser | -4.84% | 35.1% | 0.713 | 未符任何門派門檻 |
| RLCUSDT | loser | -4.80% | 10.5% | 0.467 | 未符任何門派門檻 |
| ERAUSDT | loser | -4.45% | 18.7% | 1.099 | 未符任何門派門檻 |

---

## 門派1｜15m 連四反轉派（`MEAN_REV_15M`）

_（本輪無入選）_

---

## 門派2｜1m 爆量騙炮反殺派（`SPIKE_FADE_1M`）

| 幣種 | 主/備援 | 榜單 | 入選條件 | TP% | SL% | 最長持倉(分) |
| --- | --- | --- | --- | --- | --- | --- |
| SOONUSDT | 主門派 | gainer | rev_rate_15m=83.3%≥65.0% 且 (pulse=0.189≥0.3 或 rev_rate_5m=58.3%≥54.0%) | 0.85% | 0.65% | 12 |
| SOPHUSDT | 主門派 | gainer | rev_rate_15m=74.1%≥65.0% 且 (pulse=0.253≥0.3 或 rev_rate_5m=55.6%≥54.0%) | 0.85% | 0.80% | 12 |
| LABUSDT | 主門派 | gainer | rev_rate_15m=79.0%≥65.0% 且 (pulse=0.187≥0.3 或 rev_rate_5m=57.9%≥54.0%) | 0.85% | 0.65% | 12 |
| CFGUSDT | 主門派 | loser | rev_rate_15m=77.8%≥65.0% 且 (pulse=0.270≥0.3 或 rev_rate_5m=57.4%≥54.0%) | 0.85% | 0.65% | 12 |
| INJUSDT | 主門派 | loser | rev_rate_15m=71.4%≥65.0% 且 (pulse=0.181≥0.3 或 rev_rate_5m=57.1%≥54.0%) | 0.85% | 0.65% | 12 |
| LITUSDT | 主門派 | loser | rev_rate_15m=83.3%≥65.0% 且 (pulse=0.201≥0.3 或 rev_rate_5m=70.0%≥54.0%) | 0.85% | 0.65% | 12 |
| FILUSDT | 主門派 | loser | rev_rate_15m=68.4%≥65.0% 且 (pulse=0.194≥0.3 或 rev_rate_5m=57.9%≥54.0%) | 0.85% | 0.65% | 12 |
| RENDERUSDT | 主門派 | loser | rev_rate_15m=86.4%≥65.0% 且 (pulse=0.173≥0.3 或 rev_rate_5m=54.5%≥54.0%) | 0.85% | 0.65% | 12 |
| SUSDT | 主門派 | loser | rev_rate_15m=80.0%≥65.0% 且 (pulse=0.197≥0.3 或 rev_rate_5m=64.0%≥54.0%) | 0.85% | 0.65% | 12 |

### 觸發條件摘要

- T-1 爆量紅 K +（上影線≥35% 或 T 收黑）→ 於 T 收盤 **做空**
- T-1 爆量黑 K +（下影線≥35% 或 T 收紅）→ 於 T 收盤 **做多**

---

## 門派3｜1m 爆量動能直追派（`SPIKE_MOMO_1M`）

| 幣種 | 主/備援 | 榜單 | 入選條件 | TP% | SL% | 最長持倉(分) |
| --- | --- | --- | --- | --- | --- | --- |
| IRYSUSDT | 主門派 | gainer | 強制順勢：volume_ratio=2.654≥1.5 且 price_position=99.9%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |
| VELVETUSDT | 主門派 | gainer | 強制順勢：volume_ratio=4.930≥1.5 且 price_position=74.2%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |
| SENTUSDT | 主門派 | gainer | 強制順勢：volume_ratio=1.844≥1.5 且 price_position=93.4%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |
| CROSSUSDT | 主門派 | gainer | 強制順勢：volume_ratio=1.970≥1.5 且 price_position=73.9%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |
| MEGAUSDT | 主門派 | gainer | 強制順勢：volume_ratio=1.645≥1.5 且 price_position=97.5%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |
| 1000LUNCUSDT | 主門派 | gainer | 強制順勢：volume_ratio=2.572≥1.5 且 price_position=94.4%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |
| CAPUSDT | 主門派 | loser | 強制順勢：volume_ratio=1.698≥1.5 且 price_position=1.4%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |
| BARDUSDT | 主門派 | loser | 強制順勢：volume_ratio=1.684≥1.5 且 price_position=9.1%（≥70.0% 或 ≤30.0%） | 1.80% | 0.85% | 20 |

### 觸發條件摘要

- 1m 爆量且實體佔比 ≥ 60%：紅→**做多**，黑→**做空**

---

## 門派4｜15m 順勢追擊派（`TREND_FOLLOW_15M`）

| 幣種 | 主/備援 | 榜單 | 入選條件 | TP% | SL% | 最長持倉(分) |
| --- | --- | --- | --- | --- | --- | --- |
| IRYSUSDT | 備援 | gainer | 強制順勢：volume_ratio=2.654≥1.5 且 price_position=99.9%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |
| VELVETUSDT | 備援 | gainer | 強制順勢：volume_ratio=4.930≥1.5 且 price_position=74.2%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |
| SENTUSDT | 備援 | gainer | 強制順勢：volume_ratio=1.844≥1.5 且 price_position=93.4%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |
| CROSSUSDT | 備援 | gainer | 強制順勢：volume_ratio=1.970≥1.5 且 price_position=73.9%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |
| MEGAUSDT | 備援 | gainer | 強制順勢：volume_ratio=1.645≥1.5 且 price_position=97.5%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |
| 1000LUNCUSDT | 備援 | gainer | 強制順勢：volume_ratio=2.572≥1.5 且 price_position=94.4%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |
| CAPUSDT | 備援 | loser | 強制順勢：volume_ratio=1.698≥1.5 且 price_position=1.4%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |
| BARDUSDT | 備援 | loser | 強制順勢：volume_ratio=1.684≥1.5 且 price_position=9.1%（≥70.0% 或 ≤30.0%）｜副門派 TREND | 2.80% | 1.40% | 60 |

### 觸發條件摘要

- 15m 連 3 紅且量 > 20 均量 → **做多**
- 15m 連 3 黑且量 > 20 均量 → **做空**

---

## 門派5｜15m 延遲畫門波段派（右側結構轉折）（`SWING_REV_15M`）

_（本輪無入選）_

---

## 可交易監控池（以主門派為準）

| 幣種 | 主門派 | TP% | SL% | 持倉上限(分) | 其他備援門派 |
| --- | --- | --- | --- | --- | --- |
| IRYSUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| VELVETUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| SOONUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
| SENTUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| SOPHUSDT | SPIKE_FADE_1M | 0.85% | 0.80% | 12 | - |
| CROSSUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| MEGAUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| 1000LUNCUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| LABUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
| CAPUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| CFGUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
| BARDUSDT | SPIKE_MOMO_1M | 1.80% | 0.85% | 20 | TREND_FOLLOW_15M |
| INJUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
| LITUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
| FILUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
| RENDERUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
| SUSDT | SPIKE_FADE_1M | 0.85% | 0.65% | 12 | - |
