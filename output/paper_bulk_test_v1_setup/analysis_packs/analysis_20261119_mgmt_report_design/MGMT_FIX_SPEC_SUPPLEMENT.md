# 修復規格補充（持倉管理可靠性）

狀態：**補充設計 only**（不實作、不部署）  
承接：`MGMT_RELIABILITY_AND_REPORT_DESIGN.md`  
約束不變：單 event loop、保留 429 共用冷卻／418 封禁、無有效報價不捏造成交、無網路不承諾退出。

---

## 1. 掃描期間的管理到期保障

### 問題
「先 manage 再 scan」仍可能讓**下一輪** manage 被長時間 K 線（單次 `get_json` 可至 ~45s）阻塞。健康旗標不足以保證到期。

### 單迴圈可驗收排程（建議常數，實作待批）

| 符號 | 建議預設 | 含義 |
|---|---:|---|
| `MANAGE_DUE_SEC` | 5 | 與 `MANAGE_POLL_SEC` 對齊的到期節奏 |
| `SCAN_SLICE_MAX_WAIT_SEC` | 2.0 | **每一個**掃描向 HTTP 呼叫（含單次 kline `get_json`）最大等待；逾時取消該呼叫 |
| `MANAGE_BOOK_*` | 見主設計 | book 專用短預算（例 timeout≤8、attempts≤2、op_budget≤10） |

### 排程規則
1. **唯一截止鐘**：`next_manage_due = last_manage_attempt_started + MANAGE_DUE_SEC`（用 fake／單調時鐘可測）。  
2. **任何掃描工作必須切片**：拉取宇宙 K／評分迴圈中，每次發起請求前與 `await` 返回後檢查 `now >= next_manage_due`。  
3. **到期則插隊**：暫停／放棄當前掃描切片 → 跑一輪 `manage_due_pass`（只做 book 管理預算內的事）→ 更新 `last_manage_attempt_started` → 再決定是否恢復掃描。  
4. **請求取消／超時收尾**：掃描用 `asyncio.wait_for(..., timeout=SCAN_SLICE_MAX_WAIT_SEC)` 或把 kline 的 op_budget 降到 ≤切片；取消後不得使用半套結果開倉；帳戶鎖／寫入保持與現行一致（無成交則無 fill）。  
5. **429／418**：插隊 manage 與掃描**共用**同一 `GuardedSession` 冷卻／封禁表；到期插隊**不得**繞過冷卻或增加違規重試。冷卻中：manage 記「受阻／冷卻中」，等下一 due。  
6. **無網路**：manage 可到期並**嘗試**；失敗只記事件；**不承諾**成功退出。

量測：**本機排程延遲上界**＝`manage_due_pass` 實際開始時間 − `next_manage_due`（不含行情是否成功）。  
**成功取得行情時間**另列，無上界保證。

---

## 2. 兩階段管理的交易語義

### 階段定義

| 階段 | 輸入 | 允許 | 禁止 |
|---|---|---|---|
| **Book 階段** | 本輪新鮮 `_book_usable` book | INITIAL_STOP／有效 stop 觸發平倉；PARTIAL_TP（若淨浮盈達標）；標記 `book_gen`／`fetched_at` | 用 ATR 更新 trail；用**上一輪** book；無有效 book 時成交 |
| **ATR／trail 階段** | 同輪 book 仍有效 **且** 倉仍存在；可選 ATR | `tighten_stop`（只收緊）；若 mark 已越新 stop → 用**當下仍有效** book 成交 | book 已過期仍成交；對已平倉再管；ATR 失敗時清掉既有 `trailing_stop`／`effective_stop` |

### 必須防止的競態
1. **同批重複部分止盈**：`partial_tp_done` 在 Book 階段成功後即持久化；ATR 階段若已 done 則跳過 PARTIAL。  
2. **已平倉再管理**：每倉開始前 `get_position`；無倉則 skip；fill 後不得用舊 pos 快照再 `close_qty`。  
3. **舊 book 等待 K 線後成交**：進入 ATR 階段前重檢 `fetched_at` 年齡；過期 → 只記 `QUOTE_STALE_AFTER_WAIT`，**不成交**；下一 due 用新 book。  
4. **ATR 失敗不廢止損**：K 線／ATR 失敗時保留既有 `effective_stop`／`trailing_stop`；Book 階段仍用 mark vs `effective_stop` 執行。

### 修復前後事件順序（可能改變結果之處）

| 項目 | 修復前 | 修復後 | 結果可能變？ |
|---|---|---|---|
| scan 內順序 | 倉A：K→manage → 倉B：K→manage | 全倉 Book 管理 → 再 ATR | **是**：B 的 stop 更早被看到 |
| POLL 失敗佔時 | book 可卡 ~45s | 短預算快速放棄＋到期插隊 | **是**：恢復嘗試更頻，穿價成交時點可能更早 |
| trail 更新 | 與 stop 綁在同一次 manage（scan 帶 ATR） | ATR 延後且可失敗 | **是**：trail 收緊可能延遲；但既有 stop 仍執行 |
| 參數本身 | — | 不改止損／TP／反手數值 | 參數未變 **≠** 路徑時序行為不變 |

須在驗收中明示：**時序修復會改變部分成交時點／是否超額穿價**，屬預期，不是策略參數調優。

---

## 3. 管理完成態與歷史統計命名

### 歷史口徑更名
- 舊稱「成功管理間隔」若僅來自 `quote_events`（寫在 `manage_position` **前**）→ 改稱：  
  **「有效報價／管理前紀錄批次間隔」**（pre-manage quote batch interval）。  
- 完成態**不得**用該序列單獨宣稱。

### 最小事件化完成紀錄（非心跳）
僅在 manage 路徑結束時寫一筆（事件驅動）：

| `manage_outcome` | 含義 |
|---|---|
| `NOOP` | 有有效 book、呼叫完成、無成交 |
| `FILLED` | 有 fill（附 `trade_id`／`exit_reason`／`fill_price`） |
| `OP_FAIL` | 預期內失敗（close 拒絕、無有效 book、冷卻中等） |
| `ERROR` | 例外；單倉隔離 |

欄位最小集：`ts, symbol, trade_id?, purpose, book_fetched_at?, book_gen?, manage_outcome, detail?`。  
**不**新增定時心跳。

---

## 4. 精確離線驗收（fake clock／session）

### 時鐘與預算（例值，實作鎖定後寫死於測試）

| 項 | 值 |
|---|---|
| `MANAGE_DUE_SEC` | 5.0 |
| `MANAGE_BOOK_REQUEST_TIMEOUT_SEC` | 5.0 |
| `MANAGE_BOOK_MAX_ATTEMPTS` | 2 |
| `MANAGE_BOOK_OP_BUDGET_SEC` | 10.0 |
| `SCAN_SLICE_MAX_WAIT_SEC` | 2.0 |
| 排程延遲容許誤差 | **≤50ms**（fake clock）或 CI **≤200ms**（真實 asyncio） |
| book 連續 timeout 單輪 | attempts **≤2**；牆鐘阻塞 **≤ op_budget(+ε)**；**禁止**第3次同輪重試 |

**管理呼叫延遲上界（本機排程）**：在掃描注入「每 slice 睡 2s、連續 20 次」時，`manage_due_pass` 開始相對 due 的延遲 **≤ SCAN_SLICE_MAX_WAIT_SEC + 容許誤差**（不得累積成 40s）。  
**成功取得行情時間**：不設保證。

### 必測矩陣（補充）

| # | 案例 | 標準 |
|---|---|---|
| M1 | 多倉同批 manage | 同 `book_fetched_at`；每倉一筆完成事件；觸價者各一 fill |
| M2 | 部分止盈一次 | 同輪 Book+ATR 僅一筆 PARTIAL；`partial_tp_done` 後不再 PARTIAL |
| M3 | ATR 更新失敗 | 仍用既有 `effective_stop` 觸價成交；不清除 trail／initial |
| M4 | K 線等待後 book 過期 | 零成交；`QUOTE_STALE_AFTER_WAIT` 或 `OP_FAIL` |
| M5 | 單倉 ERROR | 其他倉續管；帳戶不崩 |
| M6 | 429／418 | 新排程下 attempts／重送次數不大於政策允許；冷卻期無違規 GET |
| M7 | 慢掃描＋due | 排程延遲上界如上；可與「行情失敗」同時發生 |

---

## 5. 政策與報表澄清

### 待批准風控政策（另列，非本最小時序修復必備）
- 健康門檻 `N`（連續 book 失敗次數）、`T`（距上次 **FILLED/NOOP 完成** 過久）。  
- 觸發後**禁止新開倉**／掃描降級。  
→ 屬政策變更，**需另行批准**；與「到期插隊 manage」可分開合入。

### 報表費用
- **A 帳戶**：必須含未平倉 OPEN 已扣費用（及對應 realized 影響）；不得從 A 漏掉。  
- **C 完整平倉績效**：排除未平倉；OPEN 費只計入**已完整結束**的 trade_id。  
- 全帳戶事件費用 ≠ C 平倉費用（兩欄並存）。

### 事件命名
- **管理受阻**（`MANAGE_BLOCKED_*`）：無有效報價／冷卻／預算耗盡，且**尚未判定**必須退出。  
- **`EXIT_BLOCKED_*`**：已判定應退出（觸價／策略要求）但因無有效可成交 book **無法**模擬成交。  
- 未知是否該退出 → 只用管理受阻，不用 EXIT_BLOCKED。

---

## 輸出總表

### 已解決（本補充層：規格層）
- 掃描中 due 插隊＋切片超時，不只靠健康旗標。  
- Book／ATR 兩階段語義與四類防重／防舊價規則。  
- 歷史統計更名；完成態四值事件化（無心跳）。  
- 排程延遲上界 vs 行情成功時間分離。  
- A／C 費用與 BLOCKED 命名澄清。

### 仍待批准
- 實作與部署整包（config／engine／market_data／review／tests）。  
- 具體常數定稿（上表為建議預設）。  
- 健康門檻 N／T 與禁止開倉政策。  
- seal／runtime 切換與回滾。

### 精確驗收表
見 §4（M1–M7 + 預算／誤差數字）；主設計案例 1–8 仍適用，本補充覆蓋「due 插隊」與「兩階段語義」缺口。

---

*本輪仍不實作、不部署。*

---

後續補正見同目錄：`MGMT_FIX_SPEC_SUPPLEMENT_V2.md`（A 排程／超時後行為、B 取消收尾、C portfolio 狀態對照、D batch 完成事件、E 補測 E1–E6）。仍為文件 only。
