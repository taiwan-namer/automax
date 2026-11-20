# 小範圍補驗報告（不重新掃參）

- 產製：2026-11-20T09:47:18.846684+08:00
- 範圍：等待期望修正｜C R2_T2 公平對照+壓力｜B TTL5／C3 獨立診斷｜週期風險

## 1. 等待時間：每筆期望（不是總虧損）
- **B**：≤5m n=8 avg=`-2.2032` sum=`-17.6256`；>5m n=27 avg=`-4.7597` sum=`-128.5123`；Δavg(>5−≤5)=`-2.5565` → **gt5_worse_per_trade**
- **C**：≤5m n=14 avg=`-13.2923` sum=`-186.0917`；>5m n=22 avg=`-12.0536` sum=`-265.1786`；Δavg(>5−≤5)=`1.2387` → **gt5_better_or_equal_per_trade**
- **結論修正**：B 的 >5m 每筆較差（-4.76 vs -2.20）；**C 的 >5m 每筆不較差**（-12.05 vs -13.29）。先前用總虧損（C >5m 總虧更多只因筆數 22>14）宣稱兩邊都證實長等待較差，**作廢**。

## 2. C：公平對照（同訊號／同 P0／同 T2 價／同 Pstop／同截止）
- A 單批3000 T2+Pstop：盯市合計 `-39.6055`｜已實現 `77.0728`｜未平盯市 `-116.6783`｜費用 `115.5529`｜均持倉分 `37.6`｜最大名義 `3000.0000`｜最差獨立回撤(峰轉) `-51.2807`｜未平n=11
- B R2_T2 三批3000：盯市合計 `78.4357`｜已實現 `213.8758`｜未平盯市 `-135.4401`｜費用 `196.5757`｜均持倉分 `37.6`｜最大名義 `9000.0000`｜最差獨立回撤 `-105.3056`｜歧義n=0｜未平n=11
- **Δ(B−A) 盯市合計** = `118.0412`
- 原 TP50/SL75 同名義單批（**非**只差加倉對照）：盯市 `-87.9002`

## 3. C R2_T2 成本壓力（不再用 R1_T1 代替）
- base: 盯市 `78.4357`｜已實現 `213.8758`｜未平 `-135.4401`｜費 `196.5757`｜停n=5｜未平n=11
- fee_slip_1p5: 盯市 `-53.0245`｜已實現 `109.4225`｜未平 `-162.4470`｜費 `294.8672`｜停n=5｜未平n=11
- fee_slip_2p0: 盯市 `-184.4878`｜已實現 `4.9668`｜未平 `-189.4546`｜費 `393.1610`｜停n=5｜未平n=11
- touch_nofill: 盯市 `67.2809`｜已實現 `202.7209`｜未平 `-135.4401`｜費 `193.5828`｜停n=5｜未平n=11
- gap_stop_note: pyramid simulator already uses gap_open when bar.open beyond Pstop; touch_nofill suppresses first OHLC extremes to close-only

## 5. 完整交易週期風險（R2_T2 公平 B 臂）
- {"n_full_stop_cycles": 5, "avg_full_stop_net": -64.76392953359755, "max_full_stop_net": -101.25941662632839, "n_success_rebound_cycles_no_stop": 12, "avg_success_rebound_cycle_net": 25.938091163594066, "n_any_positive_rebound_component": 15, "avg_positive_rebound_component": 18.5493029930939, "one_max_stop_eats_n_avg_success_cycles": 3.903888531645424, "one_avg_stop_eats_n_avg_success_cycles": 2.496865676241368, "method": "per_complete_trade_cycle; NOT max_stop/total_rebound_pool", "drawdown_note": "dd_from_peak_indep = independent-trade mark path; NOT full-account drawdown"}
- 回撤欄位為**獨立交易**標記路徑，**不是**完整帳戶回撤。

## 4. B 兩個獨立診斷（不疊加）
### 4a 原規則 vs TTL5m（完整掛單事件）
- queued=62｜實際已平成交=35｜TTL取消原成交=28
- 每訊號淨合計：原 `-146.1379` (avg `-2.3571`) → TTL5 `-17.6256` (avg `-0.2843`)
- 被 TTL 拿掉的成交：n=28 sum_net=`-128.5123` avg=`-4.5897`
### 4b 原規則 vs C3（固定條件＋後續新訊號）
- 主樣本 closed=35｜C3 drop=27 keep=8｜keep淨額 `28.0745` / 全樣 `-146.1379`
- against_accel=27｜trend15_against=35
- OOS n=1（開倉>截止，**不參與選參**）：
  - `20261120T042025_ab76e26a:ETCUSDT:0f4c404d85` drop=True net=None exit=None against=True trend15=AGAINST

## C 模板矛盾修正
C 主樣本 pre_fill_accel_against=0 且 trend_15m AGAINST=0；不得套用 B 的「大虧與逆向加速重疊」模板句。
- 實測 C：against_accel=0｜trend15_against=0｜both=0

## 樣本清單
- C primary n=44｜open_at_cutoff=8｜oos=['20261120T042025_b9dc1424:TAOUSDT:4bf541c990']
- B primary n=36｜open=1｜oos=['20261120T042025_ab76e26a:ETCUSDT:0f4c404d85']
- 逐筆：`2_C_fair_R2T2_vs_single.csv` `3_C_R2T2_stress_trades.csv` `4_B_ttl5_queue_events.csv`
