# 零成交執行鏈路診斷（no_trade_diagnosis）

> 診斷時間：2026-11-18 21:43（CST / Asia/Taipei +0800）  
> 性質：**唯讀檢查**；未改策略參數／成交規則／帳本／歷史  
> 附件報告 20:30:10 **不可**當作最近一小時運行狀態

---

## 根因判定（可並存）

| 代碼 | 判定 | 證據強度 |
|---|---|---|
| **A. 程序未正常掃描** | **成立（現在）** | 交易進程已不在；terminal `last_exit_code=130`（Ctrl+C）；最後掃描 21:29:02，之後無心跳 |
| **B. 紀錄／報告過期或寫錯位置** | **部分成立** | `ai_review_bundle.md` / `execution_audit.md` / `run_manifest.json` 停在 20:24–20:30；但 `decision_trace`／`active_routing_plan`／`live_trades_log` mtime 較新（至 21:29）— **舊 AI 報告 ≠ 掃描已死** |
| **C. 分流導致沒有有效策略被評估** | **不成立為主因** | 可交易池約 17 幣；MMT／PIEVERSE 為 `SPIKE_MOMO_1M` 且有 `SIGNAL_EVAL`；**無人卡在 SWING** |
| **D. 正常評估但沒有訊號** | **成立（停機前 ~1h）** | `decision_trace`：959 次 `SIGNAL_EVAL` 全部 `NO_SIGNAL`；終端每分鐘印「無訊號」 |
| **E. 有訊號但被風控／開倉擋住** | **不成立（該時段）** | OPEN=0；block_reason 僅 `NO_SIGNAL`，無 MAX_POSITIONS／COOLDOWN／OPEN_REJECTED |

**一句話：**  
停機前系統有在每分鐘掃描且呼叫 detect（主門派 FADE/MOMO），但 **全部評估為無訊號**；**約 21:29 使用者 ^C 中斷後，目前沒有交易程序在跑**，故「超過一小時沒開倉」後半段是 **程序已停**，不是市場診斷可涵蓋。

---

## 1. 真正運行中的程序

| 項目 | 結果 |
|---|---|
| 目前時間／時區 | 2026-11-18 21:42:51 CST (+0800) |
| 交易 PID | **無**（`ps` 無 `python3 main.py --live`） |
| 終端紀錄 | `/Users/alan/.cursor/projects/Users-alan-MyProjects-automax/terminals/4.txt` |
| 曾執行命令 | `python3 main.py --live` |
| cwd | `/Users/alan/MyProjects/automax` |
| 歷史 PID | 50566（已結束） |
| last_exit_code | **130**（SIGINT / Ctrl+C） |
| 終端末行 | `^C` →「已中斷。輸出檔案已保留於 output/。」 |
| 是否只有一個交易程序 | 結束前應為單一；**現在為零** |
| 最近 scan_once 開始 | 終端：`📡 訊號掃描 2026-11-18 21:29:02` |
| 最近 scan 完成 | 同輪印完 17 幣「無訊號」+ 監控面板（約 21:29:03） |
| 最近成功行情 | kline vault mtime 至 21:29:02（見 §6） |
| 最近錯誤／traceback | 終端可見段落**無** Python traceback；循環有 `except Exception` 印「循環錯誤」但該時段未見 |
| 掃描心跳 | **21:29 後：無** → 不得因「曾有 PID」認定仍在掃 |

### 吞例外路徑（存在，但非本時段主因）

- `live_paper_trader.run_paper_trader` loop：`except Exception` → 印錯誤、sleep 5 秒重試（**不靜默**）
- `append_decision_trace` / `append_trade_audit`：寫檔失敗 `except: pass`（**可能吞掉稽核寫入錯誤**）
- 單幣 `scan_once` 內層 `except`：印 `✗ 掃描失敗` 並寫 `DATA_INVALID` decision

---

## 2. 輸出是否過期／目錄

工作目錄一致：`/Users/alan/MyProjects/automax/output/`（非另一副本）。

| 檔案 | 絕對路徑 | mtime | 年齡(相對21:43) | 說明 |
|---|---|---:|---:|---|
| ai_review_bundle.md | `.../output/ai_review_bundle.md` | 20:30:10 | ~73 min | **舊快照**（測試／bootstrap 產物） |
| execution_audit.md | 同上目錄 | 20:30:10 | ~73 min | **舊快照** |
| run_manifest.json | 同上 | 20:24:45 | ~78 min | run 建立時凍結 |
| active_routing_plan.json | 同上 | 21:25:09 | ~18 min | 重刷#6 附近仍更新 |
| decision_trace_20261118.jsonl | 同上 | 21:29:03 | ~14 min | **有進 live 掃描** |
| trade_audit_*.jsonl | — | — | — | **0 檔**（無持倉→無 MANAGE_TICK；無開平倉→無 OPEN/CLOSE audit） |
| live_trades_log.csv | 同上 | 21:29:03 | ~14 min | mtime 被 daily_review 觸發更新；**最後成交時間 19:38:02** |
| 主程序日誌 | terminal `4.txt` | 21:29 左右 | — | 非獨立 log 檔 |

### decision_trace 為何存在（反證「沒進 live」）

- 路徑：`scan_once` → 每幣 `append_decision_trace`（無訊號也寫 `NO_SIGNAL`）
- `ROUTING_REFRESH` 在 `refresh_routing` 末尾寫入（共 6 次）
- **不是**只在有成交才寫
- 21:29 後無新行 = 掃描停止，不是寫錯目錄

### trade_audit 為何沒有

- `MANAGE_TICK` 僅在 `check_exits_on_bar`（有持倉）  
- 該時段「目前無持倉」→ 不會產生 trade_audit  
- **不是**掃描沒跑的證據

---

## 3. config_hash 不一致

| 來源 | config_hash | 產生時間 |
|---|---|---|
| run_manifest.json | `9f20b63fa8200136` | 2026-11-18T20:24:45+08:00（**凍結寫入，之後不刷新**） |
| execution_audit / ai_review_bundle | `599ec73616a36618` | 20:30:10（`write_*` **當場** `config_hash()`） |
| 診斷當下重算 | `ccc94455c9800f9d` | 21:43 |

**機制（程式路徑）：**

- `init_or_load_run_manifest(force_new=False)`：若檔已存在 → **沿用舊 hash**，不重算  
- `write_execution_audit_md` / `write_ai_review_bundle`：每次呼叫 `config_hash()` 寫入當下值  
- `config_hash()`：`dir(config)` 全部 UPPER 欄位 → `json.dumps(..., default=str)` → sha256[:16]

**結論：**  
不一致主要是 **「manifest 凍結」vs「報告即時重算」**，外加時點間設定／程式狀態可能已變。  
**禁止**為了一致而覆寫 hash。  
無法還原 20:24 與 20:30 進程內完整 `config` 物件 → 欄位級 diff 標 **UNKNOWN**（僅知現在有 129 個 UPPER 鍵；含 `SWING_REV_ENABLED=False` 等）。

---

## 4. 實際分流（active_routing_plan.json @ 21:25:09）

總覽：ROUTED=17，WATCH_ONLY=26，SKIP=7；**primary 無任何 `SWING_REV_15M`**。

### 焦點幣

| symbol | status | tradeable | primary | 實際評估？ | 說明 |
|---|---|---|---|---|---|
| **MMTUSDT** | ROUTED | True | **SPIKE_MOMO_1M** | 是（37× SIGNAL_EVAL，皆 NO_SIGNAL） | 強制順勢：vol_ratio=1.756≥1.5 且 pos=88.4%≥70；副門派 TREND **未評估**（primary_only） |
| **PIEVERSEUSDT** | ROUTED | True | **SPIKE_MOMO_1M** | 是（59×，皆 NO_SIGNAL） | 同上；pos=95.2% |
| **PIPPINUSDT** | **WATCH_ONLY** | **False** | WATCH_ONLY | 較早窗口曾以 MOMO 評估；**當前計畫不在可交易池** | pos=**69.6%**（&lt;70）→ **未觸發**強制 MOMO；schools=[] |

### primary_only_scan=true 行為

- `scan_once` **只**對 `primary_school` 呼叫 `detect_signal_for_school`  
- TREND 副門派：`other_schools=NOT_EVALUATED`（紀錄如此）  
- **沒有**「SWING 停用則自動改評估副門派」的替代執行邏輯（只查，未新增）

### ROUTED 語意

- `status=ROUTED` 且 `tradeable=True` → 進入 watchlist／可被掃描  
- **不是**「保證有訊號」；訊號仍須過 detect_*  

### SWING

- `config.SWING_REV_ENABLED=False`  
- 分流結果無 SWING primary  
- `detect_signal_for_school(SWING)` 若被叫會直接 `None`（本時段未成為評估主門派）

---

## 5. 最近 60 分鐘掃描漏斗

### 時間錨點

- 最後決策事件：`2026-11-18 21:29:03`  
- 現在：~21:43 → **此後 ~14 分鐘無掃描**（程序已停）  
- 下列「從最後事件往回 60 分」覆蓋 **有在跑的時段**（約 20:30–21:29）

### 漏斗（decision_trace 實計）

| 指標 | 數值 | 來源 |
|---|---:|---|
| ROUTING_REFRESH 輪數 | 6 | decision_trace |
| SIGNAL_EVAL 次數 | 959 | decision_trace |
| 不重複評估幣數 | 27 | decision_trace |
| final_action=NO_ENTRY | 959 | 全部評估 |
| final_action=OPEN | **0** | |
| block_reason=NO_SIGNAL | **959** | |
| 風控擋單（額滿／冷卻／同幣倉） | **0** | 無此 block_reason |
| 依門派評估 | FADE 568 / MOMO 391 | evaluated_school |
| open_position 成功／失敗 | 0 / 0（無訊號故未呼叫成功開倉） | 推自 OPEN=0；呼叫次數未單獨埋點 → 部分 UNKNOWN |
| K線不足／API失敗次數 | **UNKNOWN**（未獨立計數）；終端該時段未見大量「掃描失敗」 | |
| 掃描開始／完成輪數 | 終端可見每分鐘一輪至 21:29；**無獨立 heartbeat 欄位** → 精確「失敗輪數」UNKNOWN | |
| 停用門派跳過數 | 0（無人 primary=SWING） | |
| WATCH_ONLY 跳過 | 有寫 `NOT_TRADEABLE_OR_WATCH_SKIP` 之路徑；本漏斗主量在可交易池 NO_SIGNAL | |

### 終端旁證（非偽造）

21:28–21:29 連續輪次：17 幣皆 `｜無訊號`，含 `MMTUSDT｜SPIKE_MOMO_1M｜無訊號`、`PIEVERSEUSDT｜SPIKE_MOMO_1M｜無訊號`。

### 成交

- `live_trades_log` 最後事件時間：**2026-11-18 19:38:02**  
- 相對最後掃描小時內：**0 筆 OPEN/CLOSE**

### 紀錄缺口（誠實）

- `detect_*` **內部哪一條閾值失敗**未展開（只記 `NO_SIGNAL` + short-circuit note）  
- 無每輪 `scan_start`/`scan_end` 專用心跳 ID  
- `open_position` 呼叫計數未獨立埋點  

---

## 6. 資料與新 K／去重

| 幣 | 週期 | 最後 open_time（本地） | vault mtime | 相對診斷時刻 close 年齡 |
|---|---|---|---|---:|
| MMTUSDT | 1m | 21:28:00 | 21:29:02 | ~15 min（因程序已停，屬預期停滯） |
| MMTUSDT | 15m | 21:15:00 | 21:25:03 | ~14+ min |
| PIEVERSEUSDT | 1m | 21:29:00 | 21:29:02 | ~14 min |
| PIPPINUSDT | 1m | 21:25:00 | 21:25:03 | ~18 min（較早掉出活躍掃描） |

- 停機前 vault 與掃描時間對齊 → **不像**「last_processed 超前導致永久無新K」  
- 去重狀態是否在訊號前誤更新：**本診斷無直接計數器** → **UNKNOWN**；停機前仍有每分鐘「無訊號」輸出，顯示有跑到 detect，而非整輪卡在「無新K」早退  
- API 限流卡住整輪：該時段終端**未見**「循環錯誤」刷屏 → 低嫌疑，但無 metrics → 部分 UNKNOWN  

---

## 最小觀測補丁（待確認後再套用；不改決策）

1. 每輪 `scan_once` 開頭／結尾各寫一筆 `SCAN_HEARTBEAT`（start/end、symbol_count、duration_ms、error=null）  
2. `NO_SIGNAL` 時由 detect 回傳可選 `reject_code`（只紀錄，不改 True/False）  
3. `open_position` 進出各計數（called/success/reject_reason）寫 decision_trace  
4. `config_hash`：manifest 增加 `config_hash_at_write` 與報告分開；或以穩定序列化（frozenset→sorted list）避免跨時點誤解  
5. **不要**為了「看起來有交易」放寬門檻或強制開單  

---

## 使用者下一步

1. **確認是否要重新啟動** `python3 main.py --live`（目前無進程；重啟不會丟已實現帳本，但記憶體持倉本就為 0）。  
2. 上傳／保留：本檔、`decision_trace_20261118.jsonl`、terminal `4.txt` 片段、`active_routing_plan.json`。  
3. 若需查「為何 MOMO/FADE 全 NO_SIGNAL」→ 另開 **訊號子條件觀測**（仍非調參），展開 vol_x／影線／殭屍盤等 **NOT_EVALUATED** 明細。  

**不要**把 20:30 的 `ai_review_bundle.md` 當成 21:00 後狀態。
