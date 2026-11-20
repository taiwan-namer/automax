# 持倉管理可靠性修復設計＋報表修正設計

狀態：**設計 only**（本輪不實作、不部署、不停機）  
依據：`runs/20261119T132017_bd1972b2/code_snapshot`（與 launch `package_dir` hash 吻合）  
歷史觀測截止：2026-11-19T14:23:11+08:00（第二階段查證）

---

## 0. 已確認問題（精簡）

1. **bookTicker 連續 timeout + 操作預算耗盡** → 一次 `MANAGE_POLL` 失敗，成功管理空窗約 **56s**（全持倉同時中斷，非單幣）。
2. **單執行緒 asyncio 主迴圈**內：POLL 等 bookTicker、`scan_once` 內逐倉 `await` K 線、宇宙掃描互相阻塞。
3. **quote_events 寫在 `manage_position` 之前**：成功寫入≠管理必然完成；失敗 POLL 不寫 quote。
4. **先前 n=1592 為「每持倉」間隔**，含同批多倉重複計數；全域批次間隔應另報。
5. **review 績效口徑混用**分段 CLOSE 與帳戶 realized（第一／二階段已證實）。

---

## 一、持倉管理可靠性：現況機制與 56 秒如何累積

### 1.1 現行參數（`config.py` L199–204）

| 參數 | 值 | 語義 |
|---|---:|---|
| `HTTP_REQUEST_TIMEOUT_SEC` | 15 | 單次 GET 嘗試上限 |
| `HTTP_MAX_ATTEMPTS` | 5 | 單次 `get_json` 最多嘗試 |
| `HTTP_OP_BUDGET_SEC` | 45 | 單次 `get_json` 總耗時硬帽 |
| `HTTP_BACKOFF_BASE_SEC` | 0.5 | 指數退避基底（×2^attempt） |
| `HTTP_BACKOFF_MAX_SEC` | 30 | 退避上限 |
| `HTTP_BACKOFF_JITTER_FRAC` | 0.25 | 退避抖动 |
| `MANAGE_POLL_SEC` | 5 | 掃描間 sleep 切片（意圖管理節奏） |
| `QUOTE_MAX_AGE_SEC` | 5 | 報價過期判定 |

實作：`market_data.py` `GuardedSession.get_json`（約 L168–413）。

### 1.2 14:05 空窗如何對上預算（已證實路徑）

觀測（`data_quality.jsonl` detail + quote）：

- 14:04:44 成功 `MANAGE_POLL`（全倉有 quote）
- 隨後 sleep ≈5s → 下一輪 `fetch_book_tickers`
- `HTTP_TIMEOUT` bookTicker attempt **1/5**（backoff≈0.54）
- `HTTP_TIMEOUT` attempt **2/5**（backoff≈1.20）
- `HTTP_TIMEOUT` attempt **3/5**（backoff≈2.04）→ 拋出 **`timeout budget for /fapi/v1/ticker/bookTicker`**
- `MANAGE_POLL_ERROR`；**不**進入 per-symbol `manage_position`
- 14:05:40 下一成功 POLL → 相鄰成功批次間隔 **≈56.5s**

**累積示意（有界，非無限重試）：**

```
wall ≈ sleep(≤5) + attempt1(≤15) + backoff1(~0.5) + attempt2(≤15) + backoff2(~1.2)
       + attempt3(吃到剩餘預算)  ≤  5 + ~45(OP_BUDGET)  ≈ 50s 量級
→ 與觀測 56s 同量級（含前次成功到本次啟動的切片誤差）
```

重點：空窗主因是 **單次 bookTicker `get_json` 在 timeout 路徑上吃滿 ~45s 預算**，不是把 `MANAGE_POLL_SEC` 設成 56。  
timeout 退避會寫入**進程內端點冷卻**（`min(backoff, 2.0)`，L355），但本次是預算耗盡先結束，**不是**靠密重試打穿。

429：遵守共用冷卻 + Retry-After，冷卻長於剩餘預算則放棄（L209–230、L245–282）。  
418：端點封禁，停止該 path 密集重試（L237–244、`_ban_endpoint`）。

### 1.3 掃描／K 線／管理如何互相阻塞（已證實）

單一 `while True`（`engine.py` L753+）：

```
sleep chunks (MANAGE_POLL_SEC)
  → fetch_book_tickers          # 失敗則整批 POLL 失敗
  → for each position: manage_position (atr=None)
maybe_refresh_universe
scan_once:
  → fetch_book_tickers
  → for each position:          # 串行
        log_quote(MANAGE)
        await fetch_klines_incremental   # 可再吃滿另一個 45s 預算／倉
        manage_position(atr=...)
  → 對宇宙多標的評分／開倉（又一批 klines）
```

阻塞鏈：

1. POLL 的 bookTicker 卡預算 → **所有倉**本輪無法管理。  
2. `scan_once` 內 **先拉該倉 K 線再 manage**（L381–403）→ 前倉 K 線慢，後倉 stop 檢查延後。  
3. 開倉掃描與管理共用同一 session／事件迴圈 → 慢速宇宙 K 線延長「下一輪 POLL」到來時間。

### 1.4 最小修復方案（建議範圍）

**目標**：縮短「book 失敗時佔死迴圈」的時間；管理優先於掃描；無效報價不捏造退出、不開新倉。

#### A. 分端點／分用途有界預算（核心）

| 用途 | 建議 | 說明 |
|---|---|---|
| `MANAGE_BOOK_OP_BUDGET_SEC` | **8–12**（例：10） | 僅 `fetch_book_tickers` 在 POLL／scan 管理階段 |
| `MANAGE_BOOK_MAX_ATTEMPTS` | **2** | timeout 後快速放棄，等下一 5s 切片 |
| `MANAGE_BOOK_REQUEST_TIMEOUT_SEC` | **≤5–8** | 單次嘗試短於現 15s |
| 掃描／宇宙 K 線 | 維持現有 45s/5 次 **或略降** | 不得優於管理搶同一輪 |

失敗時：記 `MANAGE_BOOK_GIVE_UP`，**立即結束本輪 POLL**，不在同輪改打更密請求；下一 sleep chunk 再試（仍走共用 429 冷卻／418 封禁）。

#### B. 管理優先＋掃描降級（同迴圈，不多執行緒）

1. **每輪固定順序**：`bookTicker(管理預算) → 僅用 book 做 stop／部分止盈檢查（全倉）→（可選）有界 ATR K 線 → 若管理健康才做宇宙掃描`。  
2. **`scan_once` 拆兩段**：先 `manage_all_with_books`（不 await klines）；再 `enrich_atr_and_trail`（K 線失敗只影響 trail，不阻擋 INITIAL_STOP）。  
3. **降級觸發**（例）：連續 N 次 book 失敗、或距上次成功管理 > T 秒 → 本分鐘跳過／截斷候選評分與開倉，只保留管理重試。  
4. **不**新開背景執行緒／進程；可用同一 event loop 的順序重排完成。

#### C. 報價不可用時的進出規則

| 情境 | 行為 |
|---|---|
| book 缺失／過期／價差異常 | **禁止**依賴該 book 的新開倉與反手開倉；已有 `STALE_QUOTE`／`_book_usable` 路徑應強制用於開倉前 |
| 持倉需退出但無有效 book | **禁止**用舊 bid/ask、禁止倒填 stop／極值成交（維持現行政策）；記 `EXIT_BLOCKED_NO_VALID_QUOTE`；下輪有界重試 |
| 部分倉有價、部分無價 | 只管理有有效 book 者；無價者標記風險，不捏造 |

明確對外語義：**網路／報價不可用時不保證能完成模擬退出**（驗收禁止反向宣稱）。

#### D. 429／418 政策（不放寬）

- 維持進程內端點共用冷卻與 Retry-After。  
- 418 維持封禁、不密集重試。  
- **禁止**用縮短冷卻或提高 attempts 來「填 56s 空窗」。空窗應靠 **縮短管理專用預算＋更快進入下一切片** 解決。

### 1.5 最小修復 vs 較大重構（取捨）

| | 最小修復 | 較大重構（本輪不採用） |
|---|---|---|
| 內容 | 管理專用短預算；scan 內先 manage 再 kline；掃描降級旗標；DQ／review 欄位 | 獨立 manage task、多 session、跨進程限流、websocket book |
| 優點 | 改動面小；單迴圈可測；不引入執行緒 | 隔離更徹底 |
| 風險 | 仍可能被極慢 DNS／系統卡頓影響 | 複雜度高、競態與帳戶鎖風險 |
| 建議 | **先做最小修復** | 僅當最小修復驗收後仍不足再提案 |

---

## 二、管理間隔統計口徑（修正）

### 2.1 關於 n=1592

**已證實**：1592＝「持倉生命期內、每標的相鄰成功 MANAGE/MANAGE_POLL quote 的 fetched_at 差」。  
同一次 `fetch_book_tickers` 會為**每個持倉各寫一列**（同 `fetched_at`）→ 多倉時**同一牆鐘間隔被每個倉各計一次**。  
本樣本：`quotes/batches ≈ 2.26`（1613／713）。

| 口徑 | n | P50 | P95 | max | 含義 |
|---|---:|---:|---:|---:|---|
| **全域成功管理批次間隔** | **712** | ≈5.14s | ≈9.88s | ≈59.0s | 相鄰不同 `fetched_at`（purpose∈{MANAGE,MANAGE_POLL}） |
| **每持倉成功管理間隔**（持倉窗內） | **1592** | ≈5.14s | ≈9.87s | ≈59.0s | 同上但按 symbol×trade 生命期 |
| MANAGE_POLL 批次間隔 | 651 | ≈5.14s | ≈10.16s | ≈64.3s | 僅 POLL |
| MANAGE 批次間隔 | 60 | ≈60.0s | ≈89.9s | ≈119s | 接近每分鐘 scan（預期） |

**分組方法（無 batch_id 時）**：`batch_key = (purpose, round(fetched_at, 6))`。  
限制：理論上兩次請求若 fetched_at 撞到同一微秒會誤併（機率極低）；`recorded_at` 僅秒級，**不用**作批次鍵。

### 2.2 失敗嘗試（整份 run `data_quality.jsonl`，detail 解析）

| 事件 | 次數 |
|---|---:|
| `HTTP_TIMEOUT` 且 detail 含 bookTicker | **5** |
| `HTTP_TIMEOUT` 且 detail 含 klines | **4** |
| `MANAGE_POLL_ERROR` | **1** |
| `HIGH_PRIORITY_EXIT_MGMT_RISK`（缺／舊價跳過） | **2** |
| `HTTP_429*`／418（本 run 檔內） | **0**（機制存在，本段未觸發） |

### 2.3 quote 寫入 ≠ 管理完成

現行順序（`engine.py` L367–403 / L785–797）：

1. `_book_usable`  
2. **`log_quote_event`**  
3. （scan 路徑）`fetch_klines_incremental`（失敗只記 DQ，仍會 manage）  
4. `manage_position(...)`  
5. 外層 `except` → `MANAGE_SYMBOL_FAIL_BOUNDED`

因此：

- 有 quote：**通常**已呼叫 `manage_position`，但若其後拋錯，可能未完成平倉。  
- `manage_position` 多以 `return events` 正常返回；`close_qty` 失敗回 `ok:False` 而不必拋錯。  
- **不得**把「有 quote」無條件當成「止損已執行」。完成態應以 **fills／position_events** 或 manage 回傳事件為準。  
- 建議（實作時另批）：`manage_result` 日誌（invoked／events／error），與 quote 分開。

---

## 三、報表修正設計（不改策略參數）

### 3.1 分欄強制分開

| 欄 | 內容 | 禁止 |
|---|---|---|
| **A 帳戶累計** | `realized_net_u`、`realized_fees_u`、`funding_data_gap`、`cash_u`、權益（浮盈須標快照時點）、日損鎖、持倉數 | 用分段勝率解釋帳戶 |
| **B 當次 run 事件** | OPEN／CLOSE／CLOSE_PARTIAL 計數；分段 `net_u` 合計；拒絕原因；本 run 特有事件 | 稱作「完整勝率」 |
| **C 完整 trade_id** | 跨 run 串接；完整平倉數、勝／敗、勝率、總淨利、均盈／均虧、盈虧比、PF、期望 | 把 PARTIAL 當獨立勝負；漏 OPEN 費 |
| **D 未平倉** | 標的／方向／qty／已扣開倉費；不進 C 勝率 | 把未平倉開倉費算進「已平倉費用」或當 PnL |

### 3.2 完整交易淨損益

```
complete_net = sum(OPEN.realized_net_u 或 −OPEN.fee_u)
             + sum(PARTIAL/CLOSE.net_u)   # 已含該腿平倉費
```

- 不重複扣費；`cash_delta`≠PnL；保證金返還≠獲利。  
- `funding_data_gap=true` 時標「資金費未完整」。  
- **費用兩欄**：① 全帳戶事件 `fee_u` 合計（含未平倉 OPEN）；② 僅 C 已完整平倉交易之費用合計。

### 3.3 建議改檔（報表，實作另批）

- `review.py`：輸出 A/B/C/D 區塊與對照一行。  
- 可選：`export_snapshot` 增加 `trades_complete.csv`。  
- **不**改 `PARTIAL_TP_*`、止損、倉位上限、反手、日損鎖。

---

## 四、離線驗收計畫（fake session／fixture only）

原則：不連交易所；不製造真 429／418；不宣稱「無網仍保證退出」。

| # | 案例 | 預期行為 | 量測 | 驗收標準 |
|---|---|---|---|---|
| 1 | 正常報價 | POLL／scan 正常 manage；可 INITIAL_STOP／PARTIAL | 批次間隔、fills | 間隔中位近 `MANAGE_POLL_SEC`；成交價=mark 公式 |
| 2 | bookTicker 連續 timeout | 短預算內 give up；記 DQ；下一切片再試 | 單輪阻塞時長、空窗 | 單輪 book 等待 **≪45s**（對齊新預算）；不密重試 |
| 3 | 慢速 K 線掃描 | 先完成全倉 book 止損檢查，再拉 K | stop 檢查相對 K 線順序 | stop 不因前倉 K 線 timeout 延後逾 SLA（設計指定，如 ≤2s+book） |
| 4 | 429 共用冷卻 | 退避／Retry-After；預算不足則放棄 | cooldown 狀態、attempts | 冷卻期無提前重送；與管理短預算共存 |
| 5 | 418 封禁 | 端點 banned；停止該 path | ban 旗標 | 無密集重試；有明確 ALERT／DQ |
| 6 | 無效／過期報價 | 不開倉；持倉不捏造退出 | STALE／EXIT_BLOCKED | 無 fill；有品質事件 |
| 7 | 多倉同時觸止損 | 同一有效 book 批次內逐倉退出 | fills 數、時間 | N 倉 N 筆 CLOSE；價皆來自該批 book+滑價 |
| 8 | 跨 run 完整交易＋部分止盈 | C 欄合併 OPEN+PARTIAL+CLOSE | complete_net、費用兩欄 | 與手工 fixture 帳一致；勝率不含未平倉 |

Fixture：`GuardedSession` 替換為可腳本化的 fake（依呼叫次序回 timeout／429／418／慢 JSON）。帳戶用臨時目錄，**不**碰 fixed_data_root 真帳戶。

---

## 五、交付總表

### 已確認問題
- bookTicker timeout 路徑吃滿 ~45s 操作預算 → ~56s 成功管理空窗。  
- 管理與 K 線／掃描串行阻塞。  
- 間隔統計曾用每倉列數放大；quote≠管理完成。  
- review 口徑混用。

### 建議修改檔案與範圍（實作須另批批准）
- `config.py`：管理專用 timeout／attempts／op_budget 常數。  
- `market_data.py`：`fetch_book_tickers` 可傳 budget 覆寫；保留 429／418。  
- `engine.py`：POLL／scan 順序（先全倉 book 管理，再 K 線／掃描降級）。  
- `run_store.py`／`review.py`：DQ 碼與 A/B/C/D 報表。  
- 測試：`tests/` 下 fake session 案例 1–8。

### 不會改動的政策
- 本金、倉位上限、日損鎖、止損／ATR／部分止盈／反手參數。  
- 成交：有效 book + 不利滑價；**禁止**舊價／止損價倒填。  
- 429 共用冷卻、418 封禁；不靠密重試填空窗。  
- 不多執行緒；不新增心跳；不碰運行中 runtime／真帳戶／金庫。

### 驗收方法
- 僅離線 fake／fixture（上表）。  
- 回歸現有單元測試 + 新案例。  
- 對照指標：管理專用 book 失敗輪阻塞時長、成功批次間隔 P95、EXIT_BLOCKED 計數、報表 C 與 fixture 帳差=0。

### 部署與回滾（需另行批准）
- 新 seal／新 runtime 目錄、哈希核對、paper 短時觀察。  
- 回滾：切回前一 seal；帳戶檔不遷移破壞。  
- **本輪明確不做**：實作、部署、重啟、停機、調參。

---

*文件結束。*
