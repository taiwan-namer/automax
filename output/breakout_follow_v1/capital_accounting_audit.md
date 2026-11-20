# capital_accounting_audit — `20261119T024357_183e09f4`

> 性質：**唯讀稽核**｜不調策略、不解除日損鎖、不重啟、不重設本金、不覆寫歷史、不啟用實盤  
> 時區：`Asia/Taipei`  
> 稽核產出時間：2026-11-19（依帳戶／權益檔當時狀態）  
> 原始 `review.md`：**未改動**；本檔為另存稽核

---

## 0. 結論摘要

| 項 | 判定 |
|---|---|
| 使用者「提高本金／保證金」的**最新明確指示** | **UNKNOWN**（找不到「提高」指令；僅有破局策略首版固定 10000／1000／10x） |
| 執行時資金參數是否被舊 `account_state` 覆蓋 config | **本金欄位**：`initial_capital_u=10000` 與 config 一致；**現金／持倉／已實現**由 `account_state` 恢復，**不會**因 config 重設 |
| `cash` 與 `equity` 零倉差額 251.2471 | **確定問題**：平倉時價格損益未寫入 `cash_u`，僅費用與保證金進出 |
| OPEN5／CLOSE7 | **非 7 筆完整交易**：5 筆本 run 開平成對 + 2 筆跨 run 僅 CLOSE；無超量平倉 |
| 報告 DD 253.86 vs 期末虧 331.22 | **口徑差異**（非兩套帳）：DD 用快照內高峰，期末虧相對初始 10000 |
| PARTIAL_TP／REVERSE 本 run 零事件 | **功能已接通**；證據指向**未達觸發／未出現反向完整訊號**，非漏記路徑斷線 |

---

## 1. 使用者資金指示檢索

### 1.1 最近一次「提高」本金／每筆保證金／名目

| 查詢範圍 | 結果 |
|---|---|
| 本對話 transcript（含 breakout 建置／overnight／匯出／本稽核） | **無**「提高本金／加大保證金／加大名目」類指示 |
| 環境變數（僅列名稱） | 無 `CAPITAL`／`MARGIN`／`NOTIONAL`／`LEVERAGE` 相關覆寫鍵 |
| 舊 FADE／MOMO 指示（100U→500U 等） | **不適用**於 `breakout_follow_v1` 獨立帳戶 |

**結論：`USER_RAISE_CAPITAL_INSTRUCTION = UNKNOWN`。**  
不得把「舊版／他策略」或「程式預設 10000」自動當成使用者剛剛確認要上調後的新值。

### 1.2 最近一次**明確寫死**的 breakout 資金規格（首版，非「提高」）

來源：使用者建立 `breakout_follow_v1` 的規格訊息（transcript `93cb77ec-…jsonl` 約 L237，【二、資金與風險】）：

| 項目 | 使用者要求 |
|---|---|
| 模擬本金 | 10000 U（僅首次建戶初始化） |
| 每筆保證金 | 1000 U |
| 槓桿 | 10x |
| 每筆初始名目 | 10000 U |
| 同時持倉 | 最多 3；同幣 1 |
| 單筆預估損失上限 | 100 U |
| 日損鎖 | 日起始淨權益降 ≥300 U |

後續 overnight／匯出／本稽核訊息**只要求保存與稽核**，未改上述數字。

---

## 2. 要求／設定／執行時對照

run：`20261119T024357_183e09f4`  
config_hash：`89cf2579037ba8ce`（`stable_v1`）  
啟動：`python3 -u -m breakout_follow_v1.paper_main --paper --hours 0.0`（見 `launch.json`；**無**資金 CLI 覆寫）  
機密：未讀取／未輸出 `.env`、API key、secret、token。

| 項目 | 使用者要求 | 設定值（config／snapshot） | 執行時值 | 來源 |
|---|---|---|---|---|
| 初始本金 | 10000（僅首次） | `INITIAL_CAPITAL_USDT=10000` | `account_state.initial_capital_u=10000` | config.py；account_state；run_manifest |
| 每筆保證金 | 1000 | `MARGIN_PER_TRADE_USDT=1000` | OPEN 扣 1000；持倉 `margin_u=1000`（繼承倉亦同） | config；portfolio.try_open；fills 名目≈10000 |
| 槓桿 | 10x | `LEVERAGE=10` | 開倉 `leverage=10`；名目/保證金≈10 | config；portfolio |
| 名目部位 | 10000 | `NOTIONAL_USDT=10000` | 本 run 5 筆 OPEN 名目 ≈9998.98–10000.00 | config；fills `qty*price` |
| 單筆風險上限 | 100 | `MAX_LOSS_PER_TRADE_USDT=100` | 有 `RISK_BUDGET_EXCEEDED` 拒絕（decision_trace） | config；portfolio；review 拒絕表 |
| 日損門檻 | 300 | `DAILY_LOSS_LOCK_USDT=300` | `day_start_equity_u=10000`；約 03:02 起出現 `DAILY_LOSS_LOCK` | account_state；decision_trace；engine |
| 最大同時持倉 | 3 | `MAX_CONCURRENT_POSITIONS=3` | 有 `MAX_POSITIONS` 拒絕 1 次 | config；review |
| 有效「可用現金」 | 保證金不足拒絕 | `free_cash()→cash_u` | 見 §3（**與權益口徑不一致**） | account.py；portfolio.py |

### 2.1 config 修改後是否被舊 account_state 覆蓋？

程式路徑 `account.load_or_create_account()`：

- 若 `ACCOUNT_PATH` 存在 → **整份載入**（含 `cash_u`／`realized_*`／`positions`／`initial_capital_u`）。
- 註解與行為：即使 `initial_capital_u` 與 `config.INITIAL_CAPITAL_USDT` 不同，**也不覆寫**歷史本金。
- 本 run 啟動快照 `account_at_start`：`cash_u≈7979.97`、`realized_net_u≈-72.36`、持倉 `1000PEPEUSDT`+`CROSSUSDT` —— 證明 overnight **沿用舊帳戶**，並非用 config 重開 10000 現金。

**判定：**

- 參數門檻（保證金／名目／槓桿／日損）以**當前 config** 為準（執行時讀模組常數）。
- 帳戶存量（現金、已實現、持倉）以 **account_state 為準**，config 改本金**不會**自動重設帳戶。
- 本 run：config 本金與 `initial_capital_u` **同為 10000**，無「config 已改、帳戶仍舊本金數字」衝突；但有「**現金帳 vs 權益帳**」模型問題（§3）。

---

## 3. 對帳：零持倉時 cash=9920.0293 vs equity=9668.7822

### 3.1 數字恆等式（帳戶檔）

```
initial_capital_u     = 10000.0000
realized_net_u        = -331.2178216420446
realized_fees_u       =   79.97068058028778
realized_funding_u    =    0.0
funding_data_gap      = True
positions             = {}
cash_u                = 9920.029319419711
equity_mark(0)        = initial + realized = 9668.782178357955
cash − equity         = 251.247141061756 ≈ 251.2471
```

另：`cash_u == initial_capital_u − realized_fees_u`（誤差 < 1e-12）。

### 3.2 逐項分解

| 項目 | 金額 (U) | 證據 |
|---|---:|---|
| 初始資金 | +10000.0000 | `initial_capital_u`；使用者首版規格 |
| 入金 | 0 | 無入金事件／欄位 |
| 出金 | 0 | 無出金事件／欄位 |
| 價格損益（毛利合計） | **−251.2471** | 見下「毛利推導」 |
| 費用（累計） | **−79.9707** | `realized_fees_u`；含跨 run 前已付費用 |
| 資金費 | **0（缺口）** | `realized_funding_u=0` 且 `funding_data_gap=True` → **不得**當完整淨收益 |
| 已實現淨損益 | **−331.2178** | `= 價格毛利 − 費用`（資金費未計） |
| 權益（零倉） | **9668.7822** | `10000 + (−331.2178)` |
| 現金（零倉） | **9920.0293** | `10000 − 79.9707`（**未扣價格毛利**） |

**毛利推導（與差額一致）：**

- 定義：`sum_gross := realized_net_u + realized_fees_u − realized_funding_u = −251.2471410617568`
- `cash − equity = −sum_gross = +251.2471`

本 run `fills.jsonl` 內 CLOSE 列 `gross_u` 合計 = **−198.9178**（僅本檔 CLOSE）；其餘毛利／費用來自**啟動前已存在的已實現**與跨 run 倉的進場費（帳戶累計費用 79.97 − 本 run fills 費用 59.94 ≈ **20.03** 為啟動前費用存量）。

### 3.3 現金為何不跟權益走？（確定問題）

`portfolio.try_open` / `close_qty` 實際行為：

1. **OPEN**：`cash_u -= margin`；`cash_u -= entry_fee`；`realized_net_u -= entry_fee`
2. **CLOSE（含全平）**：`cash_u -= exit_fee`；`realized_net_u += gross − exit_fee`；若平坦則 `cash_u += margin`（**全額退回保證金**）
3. **從未執行** `cash_u += gross`（或等價結算）

因此零倉時：

- `equity` = 經濟損益後淨值（含價格盈虧）
- `cash_u` = 初始本金 − 累計手續費（價格虧損留在 realized，**不減現金**）

用本 run fills 自 `account_initial` 重放：`sim cash`／`sim realized` 與帳戶檔 **完全一致** → 非隨機損壞，屬**帳務模型設計結果**。

### 3.4 費用是否漏記／重複？

| 檢查 | 結果 |
|---|---|
| OPEN 費用 | 計入 `realized_fees_u` 與 `realized_net_u`（−fee），並扣現金 |
| CLOSE 費用 | `net = gross − exit_fee`；exit_fee 再計 `realized_fees_u` 並扣現金 |
| CLOSE 是否重扣進場費 | **否**（`net` 不含進場費；進場費已在 OPEN 扣過） |
| 本 run fills 費用加總 | 59.9445；與帳戶增量一致 |
| 重複 CLOSE／超量 qty | **未發現**（見 §4） |

### 3.5 `cash_u` 是否參與可用資金與風控？

| 用途 | 使用欄位 | 程式 |
|---|---|---|
| 開倉保證金是否足夠 | **`cash_u`（`free_cash()`）** | `portfolio.try_open` → `INSUFFICIENT_MARGIN` |
| 日損鎖定 | **`equity_mark(floating)`** vs `day_start_equity_u` | `engine` 分鐘權益 |
| 報表權益 | `initial + realized + floating` | `account.equity_mark`；`review.py` |

**影響（確定）：** 大額已實現虧損後，`cash_u` **高估**可開倉保證金（本例多出 ≈251 U）。日損鎖仍依權益，故本 run 仍被 `DAILY_LOSS_LOCK` 擋住新倉；但若日損未鎖，可用現金會偏樂觀。

---

## 4. OPEN／CLOSE 對應（不得把 7 次 CLOSE 當 7 筆完整交易）

### 4.1 ID 現況

- 串接鍵：主要為 **`trade_id`**（`run_id:symbol:hex`）
- **`fill_id` / `position_id`：不存在**（fills 無此欄）→ 列為待確認／建議修復（觀測缺口，非本 run 重複成交證據）

### 4.2 生命周期表（本 run `fills.jsonl`）

| # | trade_id | OPEN | CLOSE | qty 對帳 | 類型 |
|---|---|---|---|---|---|
| 1 | `…183e09f4:WLDUSDT:244cefb3dd` | 1 | 1 | 38087=38087 | 本 run 完整 |
| 2 | `…183e09f4:HUMAUSDT:9243c0c5e4` | 1 | 1 | 1126281=1126281 | 本 run 完整 |
| 3 | `…183e09f4:龙虾USDT:739df0324b` | 1 | 1 | 680136=680136 | 本 run 完整 |
| 4 | `…183e09f4:DOGEUSDT:4646ef9bcd` | 1 | 1 | 168999=168999 | 本 run 完整 |
| 5 | `…183e09f4:QNTUSDT:63e08fd4f8` | 1 | 1 | 66=66 | 本 run 完整 |
| 6 | `…0232_a8c35e7a:CROSSUSDT:a3f50b92d7` | 0（本檔） | 1 | 僅 CLOSE | **跨 run 恢復後平倉** |
| 7 | `…0232_a8c35e7a:1000PEPEUSDT:f1435ca4a6` | 0（本檔） | 1 | 僅 CLOSE | **跨 run 恢復後平倉** |

統計：

- OPEN 事件 **5**；CLOSE 事件 **7**；`CLOSE_PARTIAL` **0**
- **唯一 trade_id 數 = 7**
- **完整開平成對數 = 5**；**跨 run 收尾 = 2**
- 無同一 `trade_id` 重複全平、無 close_qty > open_qty

跨 run OPEN 證據（不在本 run fills，在 root／pre_overnight 歸檔）：

- `1000PEPEUSDT`／`CROSSUSDT` OPEN 於 `2026-11-19T02:41–02:42`，`trade_id` 前綴 `2026-11-19T0232_a8c35e7a`
- 本 run `account_at_start.positions` 已載入二者

`review.md` 將 `wins=0 losses=7` 建在 **7 次 CLOSE 事件的 net_u** 上，易被誤讀成「7 筆完整交易」——**報表口徑問題**，非多平倉。

---

## 5. 最大回撤 vs 期末虧損

### 5.1 原始報告（保留）

`review.md`：權益曲線含浮盈最大回撤 **−253.8625 U**；已實現淨損益 **−331.2178 U**。

計算方式（`review.py`）：只遍歷 `equity_snapshots.jsonl`，  
`dd = min(eq − peak)`，**未**把「初始本金 10000」或「啟動瞬間無快照點」強制插入為第一點。

實測同算法：

- 高峰：`2026-11-19T02:49:05` equity≈**9922.6446**（恰逢 WLD OPEN 快照）
- 低谷：`2026-11-19T03:19:03` equity≈**9668.7822**
- DD = 9668.7822 − 9922.6446 = **−253.8625**（與報告一致）

### 5.2 稽核重算（含初始權益）

| 序列定義 | 高峰 | 低谷 | 最大回撤 |
|---|---|---|---|
| A. 僅快照（原報告） | 9922.6446 | 9668.7822 | **−253.8625** |
| B. 前置 `initial_capital=10000` + 快照 | 10000 | 9668.7822 | **−331.2178** |
| C. 前置啟動標記權益 `10000+realized_at_start≈9927.6445` + 快照 | 9927.6445 | 9668.7822 | **−258.8624** |

期末虧損 **331.2178** = `10000 − 9668.7822` = `|realized_net_u|`（零倉、無浮盈）。

**差異說明（非矛盾帳）：**

- 253.86 = **觀測窗內**自「已低於 10000 的局部高峰」再往下掉的幅度  
- 331.22 = **相對初始本金**的總權益損失  
- 二者差 ≈ **77.36** ≈ 到達局部高峰前已發生的虧損（含啟動前 −72.36 及早盤浮／實現波動）

日損：`day_start_equity_u` 固定 **10000**；equity≤9700 觸發鎖。首筆 `DAILY_LOSS_LOCK` 決策約 **03:02:04**（其時 equity≈9689）。

---

## 6. PARTIAL_TP 與 REVERSE

### 6.1 PARTIAL_TP

| 問題 | 證據與判定 |
|---|---|
| 功能是否接通？ | **是**。`portfolio.manage_position`：`net_u >= PARTIAL_TP_NET_USDT(100)` → `close_qty(..., reason="PARTIAL_TP")`；事件名在非全平時為 `CLOSE_PARTIAL` |
| 本 run 是否有事件？ | fills／trades：**無** `PARTIAL_TP`／`CLOSE_PARTIAL` |
| 是否曾達門檻？ | `equity_snapshots` 最大 `floating_u` ≈ **+32.31** ≪ 100；無 `floating_u≥100` 快照 |
| 風控拒絕？ | 無「PARTIAL 被拒」類碼；未進觸發分支 |
| 漏記？ | 單元／歸檔曾出現 PARTIAL（pre_overnight 測試倉 `AAAUSDT`）；生產路徑寫入與 OPEN／CLOSE 相同 `_append_fill`。本 run **較符合未達條件**，非路徑未接 |

**判定：未達觸發條件（淨浮盈門檻 100U）。**

### 6.2 REVERSE

| 問題 | 證據與判定 |
|---|---|
| 功能是否接通？ | **是**。`engine` 掃描：`pos and sig and sig["side"] != pos.side` → `close_qty(..., reason="REVERSE_SIGNAL")` → 記 `REVERSE_CLOSE`／可能 `REVERSE_OPEN_*`；`note_reverse` |
| 本 run 是否有事件？ | decision_trace：**零** `REVERSE_*`；fills exit_reason 僅 `TRAILING_STOP`／`INITIAL_STOP`；`account_state.reverse_events={}` |
| 風控拒絕反手開倉？ | 無 `REVERSE_OPEN_REJECTED`／`REVERSE_OPEN_BLOCKED` 列 |
| 漏記？ | 若曾反手，應同時有 `REVERSE_SIGNAL` 平倉 reason 與 decision 列；三者皆空 → **本 run 未觸發反向完整訊號分支** |

**判定：持倉期間未出現「完整反向爆量突破」觸發反手；非功能未接通、非本 run 漏記已發生的反手。**

補充：pre_overnight 歸檔有測試向 `REVERSE_SIGNAL` CLOSE，證明寫入路徑存在；與本 overnight 實盤樣本分離。

---

## 7. 證據索引

| 檔案 | 用途 |
|---|---|
| `run_manifest.json` / `config_snapshot.json` / `launch.json` | 版本、hash、啟動命令、啟動帳戶 |
| `account_initial.json` / `account_state.json` | 起訖帳務 |
| `fills.jsonl` / `position_events.jsonl` / `trades.csv` | 成交與持倉事件 |
| `equity_snapshots.jsonl` | 權益／浮盈／回撤 |
| `decision_trace.jsonl` | 拒絕碼、OPEN、日損鎖 |
| `stop_events.jsonl` | 停損收緊（本 run 多為 `TRAILING_STOP` 更新） |
| `review.md` | 原始報告（未改） |
| `breakout_follow_v1/{config,account,portfolio,engine,review}.py` | 行為依據 |
| transcript L237 使用者規格 | 10000／1000／10x 明文 |

---

## 8. 確定問題

1. **`cash_u` 不平結價格損益** → 零倉 cash＞equity；`free_cash()` 高估可開保證金。  
2. **`review` 以 CLOSE 事件數當損失筆數** → OPEN5／CLOSE7 易被讀成 7 筆完整交易。  
3. **回撤與期末虧損口徑不同** → 253.86（窗內高峰回撤）vs 331.22（相對初始本金）；報告未並陳。  
4. **`fill_id`／`position_id` 缺失** → 僅能靠 `trade_id` 串接。  
5. **`funding_data_gap=True`** → 淨收益不完整。

---

## 9. 待確認（需使用者拍板，本輪不修）

1. 是否要在後續 run **提高**本金／保證金／名目？（目前 raise 指示 = **UNKNOWN**）  
2. 現金模型修復偏好：平倉時 `cash += gross`，或可用資金改看 `equity − used_margin`？  
3. 日損鎖目前仍為 True：是否維持至日切，或另案解除（本輪禁止動）？  
4. 跨 run 倉的 OPEN 是否要在新 run fills **回填引用**（只讀連結，不改歷史）？  
5. PARTIAL 門檻 100U 在 1000U 保證金／短持倉下是否過高（屬參數決策，非本輪）？

---

## 10. 建議修復（等待確認後再做）

1. **帳務**：全平／部分平倉時將 `gross`（或 `net`）結算進 `cash_u`，並加不變式測試：`flat ⇒ abs(cash − equity) < ε`。  
2. **風控**：`INSUFFICIENT_MARGIN` 改以 `equity_mark(0) − used_margin`（或修復後 cash）判斷。  
3. **報表**：區分 `n_close_events`／`n_round_trips`／`n_cross_run_exits`；回撤同時列「相對初始」與「窗內高峰」。  
4. **觀測**：為每筆 fill 寫 `fill_id`；POSITION 事件寫 `position_id=trade_id`。  
5. **資金費**：補結算或持續標 gap，禁止用 realized 冒充含資費淨利。

---

## 11. 本輪未執行

- 未改 config／參數／策略  
- 未解除 `day_new_entries_locked`  
- 未重啟／停止交易進程  
- 未重設本金、未改寫 fills／account 歷史  
- 未啟用實盤  
- 未覆寫 `review.md`
