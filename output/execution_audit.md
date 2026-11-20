# 成交與帳務可信度稽核（execution_audit）

> 產生時間：2026-11-19T03:26:53+08:00（Asia/Taipei）
> strategy_version：`fade_long_only_v2`｜config_hash：`257ef0a2d47262d8`｜git：`UNKNOWN`

判定等級：`PASS` / `FAIL` / `UNKNOWN`
問題類型：`程式缺陷` / `模擬限制` / `缺少紀錄`

---

## A. 多空保本與移動停損

| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |
|---|---|---|---|---|
| LONG 保利停損上移 | PASS | `live_paper_trader.py` / `check_exits_on_bar` | `be_price = entry*(1+lock%)`，且僅在 `be_price > original_sl` 時取代 | — |
| SHORT 保利停損下移 | PASS | 同上 | `be_price = entry*(1-lock%)`，且僅在 `be_price < original_sl` 時取代 | — |
| 停損只能收緊 | PASS | `effective_sl_prices` / `check_exits_on_bar` | LONG 有效SL=max(原SL,保本)；SHORT=min(原SL,保本)；`lock_offset` 只增不減 | — |
| 區分原始/有效/保本/追蹤 | PASS（紀錄層新增） | `audit_observability.effective_sl_prices` | 原模擬只保存最終出場價；新 trade_audit 分開記錄。歷史舊單為 UNKNOWN | 缺少紀錄（歷史） |
| TP 角色 | PASS | `check_exits_on_bar` | TP 為硬出場目標（觸及即 `TP_HIT`）；追蹤鎖利另以 MFE 回吐用 **close** 觸發 `TRAIL_PROFIT`，不是把 TP 當追蹤門檻 | — |
| BREAKEVEN_WIN 標籤依據 | FAIL（標籤語意） | `check_exits_on_bar` L1515-1524 | 觸發機制是「有效保本SL被碰到」；但標籤額外要求 `gross>0` 才叫 BREAKEVEN_WIN，否則仍記 SL_HIT。**不可**只因 gross>0 就當成追蹤停利；目前也**沒有**把 TRAIL 與 BE 混稱。建議：以 `exit_trigger_type=BREAKEVEN_SL` 為準，gross 僅作附註。最小修補：新增 trigger 欄，不改成交價。 | 程式缺陷（標籤） |

## B. 時間與 K 線邊界

| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |
|---|---|---|---|---|
| 訊號只用已收盤K | PASS | `_closed_klines` + 各 `detect_*` | `open_time + interval_ms <= now` 才納入 | — |
| 進場前 high/low 是否算入持倉路徑 | PASS（同輪） | `scan_once` | 先 `check_exits` 再 `open_position`；新倉同輪不再用該根 OHLC 做出場。下一根才開始 MFE/出場 | — |
| 出場後同根波動是否算入 MFE | PASS | `close_position` → del position | 平倉後移除持倉，後續 bar 不再更新該倉 MFE | — |
| 分鐘粒度路徑精確性 | UNKNOWN | `check_exits_on_bar` | 僅有 OHLC，無法還原持倉內真實價格路徑；標示為模擬假設 | 模擬限制 |

## C. 同根 K 價格先後順序

| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |
|---|---|---|---|---|
| 同根同碰 TP+SL 規則 | PASS（規則明確）／模擬限制 | `check_exits_on_bar` | 若兩者皆觸發，**強制 SL 勝出**（`hit_tp=False, hit_sl=True`）。無開高低收路徑推論。標記建議：`AMBIGUOUS_INTRABAR` | 模擬限制 |
| 同根先用高低更新保本再觸發 | FAIL（可信度風險） | `check_exits_on_bar` | 同一函式內先 `_update_mfe_mae(high,low)` 並可能上移 lock，再立刻用同根另一端觸發有效SL。若真實路徑是先碰到低點再高點，結果可能不同。應標記 `AMBIGUOUS_INTRABAR_ARM_AND_STOP`；**不建議未確認就改規則** | 模擬限制／潛在程式缺陷 |
| 樂觀/保守邊界 | UNKNOWN（歷史）／PASS（新單可記） | `classify_intrabar_exit` | 歷史成交未存邊界；新 trade_audit 可寫 optimistic/conservative。不覆寫歷史模擬結果 | 缺少紀錄（歷史） |
| 模擬成交價 vs 觀測價 | PASS（需分稱） | close / tp_price / sl_price | TP/SL 觸發用公式價；TRAIL/TIME/MOMO_STALL 多用 **bar close**。皆為模擬假設成交，非交易所實際成交 | 模擬限制 |

## D. 帳務

| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |
|---|---|---|---|---|
| entry/exit 費用各扣一次 | PASS（模型） | `_calc_pnl` / `close_position` | 平倉 143 筆；exit_reason 合計 143；fee_u 總和 14.3000；OPEN 不寫 fee；_calc_pnl 以 notional×0.001 **一次**計雙邊費。無分離 entry_fee/exit_fee 欄位；等同一次記 round-trip。浮動權益也預扣 round-trip，但平倉後浮動消失→**不雙記入已實現** | — |
| balance_after_u 語意 | PASS（需釐清） | `calc_equity` + close log | 寫入值為 `INITIAL + realized_net + floating`（權益）。開倉當下 floating 含預估費故略低於現金感。不是純現金帳戶 | 缺少紀錄（未標欄位語意於舊CSV） |
| 100→500 本金調整 | UNKNOWN | config / live_trades_log | balance_after_u 範圍 97.1961~501.3788；closed net 加總 -25.1369；目前 INITIAL_CAPITAL=500.0。早期餘額貼近 100 而後跳至 ~500，符合本金調升，**無獨立資金調整流水**。不可把跳升當獲利；標記 **未對帳差額／資金調整未入帳** | 缺少紀錄 |
| 出場原因加總=平倉數 | PASS | live_trades_log | 分布={'SL_HIT': 68, 'TIME_EXIT': 43, 'TP_HIT': 29, 'TRAIL_PROFIT': 3}；closed=143 | — |
| SL類 vs 虧損筆數分離 | PASS（統計應分離） | review stats | 含SL字樣出場 68（淨利>0: 1，淨損: 67）；全部淨損筆數 92。保利停損可為正收益，不可等同虧損單 | — |

### OPEN/CLOSE 配對抽樣
- OPEN=145 CLOSE=143（差額可能為未平倉或中斷）

---

## 總結：影響成交可信度的 FAIL／風險

1. **同根K先更新MFE/保本再觸發有效SL**（`AMBIGUOUS_INTRABAR_ARM_AND_STOP`）— 模擬限制／潛在缺陷。
   - 最小修補方案（待確認）：同根內若 lock 剛因該根 high/low 才啟動，則該根禁止用另一端觸發新有效SL；或標記歧義並提供樂觀/保守邊界，**不改歷史成交價**。
2. **BREAKEVEN_WIN 依賴 gross>0** — 標籤語意缺陷；最小修補：以 trigger type 為準。
3. **本金 100→500 無調整流水** — 缺紀錄；勿回填成獲利。

以上 FAIL **尚未改成交規則、未改歷史**。待使用者確認後再修。

