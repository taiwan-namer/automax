# 08 帳戶核對（A/B/C/D）

時點：`2026-11-19T16:42:48.320125+08:00`（**部分日**；非午夜強制結算）  
費率：**paper 模擬** `FEE_RATE_ONE_SIDE`（非交易所實收保證）

## 定義
- **A 帳戶水位**：日初 `day_start_equity_u`、當前 equity／cash／realized／floating／持倉保證金
- **B 視窗事件**：當日時間窗內 OPEN/PARTIAL/CLOSE 事件流（可跨 trade 生命週期）
- **C 完整交易**：有 OPEN+CLOSE，且 **CLOSE 落在當日** 的 complete trade 淨損益合計
- **D 未平倉**：仍持倉；OPEN 費已扣；未實現分離

## A — 水位
- day_key: `2026-11-19`
- day_start_equity_u: `50000.0`
- cash_u: `46054.1474031552`
- realized_net_u: `-1945.8525968447861`
- realized_fees_u: `590.2545290434049`
- used_margin (sum positions.margin_u): `2000.0`
- latest equity snapshot: `48054.14740315521`（ts=2026-11-19T16:42:11+08:00）
- latest floating_u: `0.0`
- day_new_entries_locked: `False`
- 日損失估計（day_start − equity_now）：`1945.8525968447866`
- 持倉數：2 → ['ENAUSDT', 'PENGUUSDT']

**注意**：`cash_delta_u` ≠ 損益；勿把 cash 變動當 realized。

## B — 當日事件（fills 去重後，recorded_at 日起碼為 2026-11-19）
- 事件計數：{'OPEN': 60, 'CLOSE': 58, 'CLOSE_PARTIAL': 7}
- 事件筆數：125

## C — 完整交易且 CLOSE 在當日
- n=58；net_sum=-1935.8526000120405
- 這是完整 trade 結果，不是分段勝率。

## D — 未平倉
- 20261119T132017_bd1972b2:ENAUSDT:cd1f2725f2 ENAUSDT SHORT open=2026-11-19T15:12:10+08:00 fees_in_fills=4.999997800399999
- 20261119T161354_5cb8369a:PENGUUSDT:ff66cbb90f PENGUUSDT LONG open=2026-11-19T16:42:11+08:00 fees_in_fills=4.999999032346499

## 勾稽提醒
- 跨 run 合併依 trade_id；fills 已 dedupe。
- 開倉費計入 complete_net（open 成分）。
- 不重複扣費；不把 B 的局部事件勝率當成 C。
