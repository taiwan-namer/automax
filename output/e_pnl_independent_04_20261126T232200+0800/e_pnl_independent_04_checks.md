# e_pnl_independent_04_checks（修正版）

- 對應 review 修正版｜產物目錄 `output/e_pnl_independent_04_20261126T232200+0800/`
- 命令：`python build_independent_04_fix.py`｜另：本機 `mapping_amounts.json` 由 SETTLEMENT 唯一鍵加總

## 測試表

| ID | 測試 | 資料範圍 | 預期 | 實得 | 判定 | 產物 |
|---|---|---|---|---|---|---|
| T1 | income 分頁覆蓋 | 2026-11-22→22:05:57 | 可重跑、有頁數 | pages=54 unique=53550 | **通過** | metrics.boundary |
| T2 | 去重冪等 | 同上連抓兩次 | key 集合相等 | idempotent_rerun=`true` | **通過** | metrics.boundary |
| T3 | 21:42 橋接守恆 | income≤21:42:08 + 快照錢包 | 列出 implied／residual／完整精度 | implied=`5485.43957505` residual=`0.09778263` | **已計算；殘差未解（不標通過）** | bridge_214208 |
| T4 | 22:05 橋接守恆 | income≤22:05:57 + 當時錢包 | 同上 | implied=`5461.14505971` residual=`0.09778263` | **已計算；殘差未解（不標通過）** | bridge_220557 |
| T5 | 未實現不進橋 | — | residual 解釋不含 upnl | 未使用 upnl | **通過** | review |
| T6 | 本金同秒事件 | ±1s @19:01:55 | 列出 | +0.01 與 +5000 | **通過** | same_second_events |
| T7 | 期初 W0 | balance 歷史 | 有證據或條件式 | 無 API；implied_W0=`0.09778263` 條件式 | **資料受限（不標通過）** | conditional |
| T8 | 多 CLOSE 唯一映射 | ≥ERA fills + userTrades 抽樣 | 組數、金額、雙計差 | n=20｜uniq_rp=`-263.74620791`｜delta_rp=`0`｜GUA 75696102 一致 | **計數／金額通過；雙計差通過** | mapping_amounts |
| T9 | OPEN 無 CLOSE 映射 | ≥ERA | 組數、order、持倉分類 | n=48｜有 order=48｜RECONCILE=40｜在倉=10｜userTrades 未命中=5 | **部分：5 筆未完成** | mapping_amounts |
| T10 | 分組加總 | 日／類型 | 完整精度字串 | daily_220557／214208 已輸出 | **通過（加總輸出）** | metrics.daily_* |
| T11 | 歷史起點 | log+archive | 首啟／首 OPEN 時間 | 首啟 11/23 00:07:54；首 OPEN 11/23 02:09:05 BLESS 422609304 | **通過** | origins + archive |

## 本輪新增 vs 沿用

**新增**：T1–T11 重跑數字；雙截止完整精度；多 CLOSE 唯一金額與雙計差；首啟／archive 首單；W0 條件式。

**沿用**：顯示 −440 端點與版本重置敘事；本金後 +274.60999709；帳戶 key 指紋。

## 總判定

- 可交付核對：雙截止橋接數字、歷史起點、20 組多 CLOSE 金額與「非雙計」、income 冪等。
- **不得標整體通過**：殘差 0.09778263 未解；W0 無證據；5 筆 open 明細未完成。
- 不可把「未做」寫成「不可識別」：上表已分 **通過／已計算不通過／資料受限／部分**。
