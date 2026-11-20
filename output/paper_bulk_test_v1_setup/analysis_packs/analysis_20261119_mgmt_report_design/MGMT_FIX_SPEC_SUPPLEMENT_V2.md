# 管理可靠性規格補正 V2（文件 only）

狀態：**設計補正／未實作／未部署**  
承接：`MGMT_RELIABILITY_AND_REPORT_DESIGN.md`、`MGMT_FIX_SPEC_SUPPLEMENT.md`  
保留：案例 1–8、驗收 M1–M7（本文件追加 A–E 與補測，不取代舊案）。  
約束：不改交易碼；不新增定時心跳；無有效行情不承諾成交退出；不繞過 429／Retry-After／418。

數值門檻／退避常數凡新增者一律 **待批准**（下表「建議預設」≠ 已批准）。

---

## A. 排程與超時後行為

### A.1 `next_manage_due` 更新時點（單調時鐘）

| 時點 | 行為 |
|---|---|
| 進入 `manage_due_pass` **開始時** | `last_manage_attempt_started = mono_now`；**立即**設 `next_manage_due = last_manage_attempt_started + MANAGE_DUE_SEC` |
| 管理成功結束 | **不**再把 due 重設為「結束+週期」（避免 manage 耗時>週期時堆積補跑） |
| 管理失敗／受阻 | due 已在開始時推進；另見失敗退避鐘 `next_manage_allowed_at`（§A.4） |
| 時鐘 | 排程與 due 比較一律 `time.monotonic()`（或測試 fake mono）；牆鐘僅用於日誌 |

### A.2 manage 耗時 10s、due 週期 5s

- 開始時已把 due 推到 T0+5；若 manage 跑到 T0+10，結束時 **now > next_manage_due**。
- **禁止**：結束後立刻連續重試把錯過的輪次「補跑」完（不得 while-catch-up）。
- **允許**：結束後回到主迴圈；若 `mono_now >= next_manage_due` **且** `mono_now >= next_manage_allowed_at`，則再跑 **一輪**；否則等待。
- 結果：最密節奏 ≈ max(實際 manage 耗時, MANAGE_DUE_SEC)，而非 0-gap burst。

### A.3 與 429／418／Retry-After

- 插隊 manage 與掃描共用 `GuardedSession` 冷卻／封禁。
- due 到期 **不得** 繞過冷卻或增加 attempts。
- 冷卻中：記 `MANAGE_BLOCKED_COOLDOWN`（或同等），**不**發違規 GET；等下一 allowed 窗。

### A.4 正常節奏 vs 失敗退避

| 鐘 | 含義 |
|---|---|
| `next_manage_due` | 正常管理節奏（到期嘗試權） |
| `next_manage_allowed_at` | 失敗／受阻後最早允許再試（退避） |

實際可跑條件：`now >= next_manage_due AND now >= next_manage_allowed_at AND not in_ban`。

建議（**待批准**）：失敗退避序列例 `0, 1, 2, 5, 10` 秒封頂；成功 NOOP／FILLED 重置退避計數。具體數值未批准前不得寫入生產配置。

### A.5 掃描飢餓與恢復

- **飢餓定義（建議，待批）**：連續 N 次 due 插隊導致掃描切片 0 進度，或距上次完整宇宙刷新 > T_scan。
- **恢復**：在一次 `manage_due_pass` 後，若掃描已 overdue，允許至少 1 個掃描切片（仍受 slice timeout 與下次 due 約束）；不得為恢復掃描而跳過已到期 manage。
- N／T_scan：**待批准政策**，非最小時序修復必備。

---

## B. 掃描切片與取消

### B.1 `wait_for` timeout ≠ 硬性牆鐘上界

量測「呼叫返回」時須計入：

1. 取消傳播與 `CancelledError` 處理  
2. 同步計算（評分、排序）  
3. 檔案 IO（帳戶保存、事件寫入）

因此：**延遲上界適用前提**＝「異步等待段 + 受測環境的取消收尾」；**fake clock 測試不能冒充真實網路 RTT／TLS／DNS 保證**。真實網路只保證「本機排程試圖取消」，不保證對端已停止。

### B.2 CancelledError 傳播檢查清單

實作驗收必須證明：

- 切片 `wait_for` 逾時後，`CancelledError` 不在掃描層被裸 `except Exception` 吞掉（或吞掉前已標記取消並 `raise`）。  
- HTTP response／stream 在 cancel 路徑釋放。  
- 帳戶鎖／檔案鎖在 cancel 路徑釋放。  
- 無「取消後仍在背景 task 繼續請求」以假裝符合上限。  
- 半套 K 線結果不得用於開倉。

### B.3 管理優先保障範圍

到期插隊必須能打斷／延後以下阻塞工作（皆切片化）：

- 宇宙更新  
- K 線拉取  
- 評分／排序  
- ATR enrichment  
- 其他掃描期同步重活

### B.4 追加測試設計

| ID | 案例 | 標準 |
|---|---|---|
| B-T1 | 取消收尾緩慢（inject 100–200ms cleanup） | 觀測延遲可 > timeout，但不得啟動新的掃描 HTTP；manage due 仍可在 cleanup 後插隊 |
| B-T2 | 同步評分過長（inject 500ms CPU） | 切片邊界必須在同步段**前**檢查 due；不得宣稱「wait_for=2s 故總阻塞≤2s」 |

---

## C. 兩階段交易語義（對照實際 `portfolio.manage_position`）

來源：sealed runtime `portfolio.py`（paper）：順序為極值更新 → PARTIAL_TP →（成功後）breakeven → ATR trail tighten → effective_stop 觸發平倉。反手在開倉路徑，不在 `manage_position` 內。

### C.1 狀態／動作歸屬

| 行為 | 現行依賴 | 建議 Book 階段 | 建議 ATR 階段 | 備註 |
|---|---|---|---|---|
| initial stop 觸價 | mark vs `effective_stop`（常=initial） | ✓ 成交 | 若仍持倉且 book 仍新：✓ | 不需新 ATR |
| effective stop 觸價 | 同上 | ✓ | ✓（book 未過期） | |
| breakeven 設定 | PARTIAL 成功後 `tighten_stop(be)` | ✓（隨 PARTIAL） | 不重設放寬 | |
| partial TP | 淨浮盈≥門檻且未 done | ✓ | 若 Book 已 done → 禁止再 PARTIAL | |
| 價格推進 trail | `partial_tp_done` + `atr_latest>0` | **不更新 ATR trail** | ✓ 只收緊 | |
| ATR 更新失敗 | — | 不影響既有 stop | 跳過 trail 更新 | **不得清除**既有 effective／trailing／breakeven |
| 反手 | 開倉／訊號路徑 | 不在 manage Book/ATR | 不在 manage | 策略行為變更另批 |

### C.2 關鍵不變量

- **不可**因 Book 階段不更新 ATR，就停止原本不依賴新 ATR 的 trail／保本／initial 觸價。  
  → 已存在的 `effective_stop`／`breakeven_stop`／`trailing_stop` 在 Book 階段仍必須可觸發。  
- book 過期：**不得成交**；記 `QUOTE_STALE`／`OP_FAIL`。  
- ATR 失敗：**不清除**既有 stop。  
- 禁止：重複 PARTIAL；重複全平；用舊 `pos` 快照對已平倉再 `close_qty`。

### C.3 同時多條件時：現行 vs 建議優先序

**現行（單函式順序）**：PARTIAL（含即時 BE 平倉）→ trail 更新 → stop 觸價。

**建議兩階段**：

1. Book：PARTIAL →（BE 設定／即時 BE）→ **stop 觸價（含已有 trail/BE/initial）**  
2. ATR：僅仍持倉且 book 未過期 → trail 更新 → 若 mark 已越新 stop → 用**當下有效 book**成交  

比較：ATR 更新被延後時，**既有** trail／BE 仍在 Book 階段可退出（修復「等 ATR 才管 stop」的飢餓）。  
**可能改變成交結果的時序**（明示）：同批多倉改為先全倉 Book；book 短預算更快放棄；trail 收緊可能延遲一至數個 due。屬時序修復預期，**不是**策略參數調優。新的風控／策略數值另列待批。

---

## D. 管理完成事件（事件化，非心跳）

### D.1 關聯鍵

- `batch_id`：本輪 `manage_due_pass`  
- `attempt_id`：單次 book／倉嘗試  
- 連結：報價快照、每倉 `manage_outcome`、整批結果

### D.2 整批與每倉

- 整批 book 失敗／冷卻：仍須寫 **batch blocked／failed** 事件（不得沉默）。  
- 每倉：`NOOP`／`FILLED`／`OP_FAIL`／`ERROR`。  
- **部分成功**：例 PARTIAL `FILLED` 後後續 close `OP_FAIL` → 兩筆（或一筆含 `legs[]`）皆可表達；**禁止**只留最終 ERROR 掩蓋 PARTIAL。  

### D.3 計數欄位（batch）

| 欄位 | 含義 |
|---|---|
| `expected_n` | 本批預計管理倉數（開始時持倉數） |
| `checked_n` | 已檢查數 |
| `success_n` | NOOP+FILLED（或政策定義之成功） |
| `blocked_n` | 受阻 |
| `error_n` | ERROR |
| `filled_n` | 有成交 |

- **無持倉**：`expected_n=0`，不得冒充「全倉管理成功」。  
- 歷史 `quote_events` 僅稱「有效報價／管理前紀錄批次」。  
- 完成事件＝路徑結束寫入；**不**新增定時心跳。  
- 完成紀錄寫入失敗：另計 `observability_write_fail`（日誌／DQ）；不得為寫入失敗而重做成交。

---

## E. 精確測試規格（補測）

沿用補充 V1 預算表；下列為追加。容許誤差：fake clock **≤50ms**；真實 asyncio CI **≤200ms**（僅排程，不含網路成功）。

| ID | 案例 | 預算／次數／前提 | 通過標準 |
|---|---|---|---|
| E1 | manage 耗時 > due 週期 | manage stub 睡 10s；DUE=5 | 結束後無緊密補跑；下一輪間隔 ≈≥ manage 耗時；錯過輪次不 while 追趕 |
| E2 | 429／418 期間 | 注入 429+Retry-After／418 ban | 無違規重送；attempts≤政策；due 插隊不繞過 |
| E3 | 掃描取消收尾 | slice timeout=2；cleanup inject | 見 B-T1；無背景繼續請求 |
| E4 | ATR 階段不擋下一 due | ATR stub 睡 > DUE | Book 階段已推 due；下一 due 仍可先 Book；既有 stop 可成交 |
| E5 | 部分成功後失敗事件 | PARTIAL ok + 後續 close fail | 事件同時可見 PARTIAL 成功與後續失敗 |
| E6 | 多倉單倉異常 | 倉B raise | 倉A/C 續管；帳戶不崩；B=ERROR |

**不得承諾**：無有效行情仍可成交退出。  
**不得**：以 fake clock 綠燈宣稱生產網路延遲上界。

---

## 與策略變更分離

| 類型 | 本文件 | 批准狀態 |
|---|---|---|
| 管理時序／取消／完成事件／兩階段語意 | 規格補正 | **實作與部署待批** |
| 健康門檻 N／T、禁止開倉、退避秒數定稿 | 建議列舉 | **待批** |
| 進場成本門檻／同批同向限制／突破濾網 | 見每日報表策略候選 | **待驗證再批**；非本修復包 |

---

## 補正清單（相對 V1 新增／明確化）

1. due 在 **attempt start** 更新；禁止錯過輪次 burst 補跑。  
2. 正常 due 鐘 vs 失敗 `next_manage_allowed_at`。  
3. wait_for 非硬牆鐘；取消傳播／鎖／response 清單；禁止背景續請求。  
4. 管理優先覆蓋宇宙／K／評分／ATR／同步重活；fake clock 前提。  
5. portfolio 對照表：initial／effective／BE／PARTIAL／trail／ATR／反手歸屬。  
6. Book 不更新 ATR ≠ 停用既有 trail／BE／initial。  
7. batch_id／attempt_id；整批失敗事件；部分成功不可掩蓋；無倉不冒充成功。  
8. 補測 E1–E6 + B-T1/T2；明確誤差與「無行情不承諾退出」。

*本輪仍不實作、不部署。*
