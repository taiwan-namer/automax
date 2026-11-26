# h_price_action_v2 交付報告

日期：2026-11-26（Asia/Taipei）  
狀態：**已完成離線實作與測試；未部署、未重啟、未啟動 Paper／Testnet、未下單、未撤單。**  
未修改 A／E；未覆蓋既有 `paper_h_surge_trend_v1`／案例研究目錄內容（新 profile：`paper_h_price_action_v2`）。

---

## 1. 修改檔案與摘要

| 檔案 | 變更 |
|---|---|
| `config.py` | `STRATEGY_ID/VERSION`→`h_price_action_v2`；新 profile；`ENABLE_CONTINUATION_ARM=False`；B／C／CHOP／冷卻閾值；`TRADE_STALE_SEC=2.0` |
| `price_series.py` | **新增**稀疏 1 秒價序列（缺秒不補價） |
| `path_abc.py` | **新增** A／B／C 與 CHOP |
| `entry_gate.py` | **新增**冷卻／消費／180s 次數閘 |
| `signals.py` | 重寫為 v2 `evaluate_entry`（三路徑並行；`NO_SURGE` 不擋 B／C） |
| `signals_v1_legacy.py` | **新增**原 v1＋cont_arm 評估（回退／測試） |
| `trade_feed.py` | tape 加 `sparse`；dense grid 仍供 Path A |
| `engine.py` | 每完整秒評估、診斷欄位、v2 gate；cont_arm 預設不武裝 |
| `portfolio.py` | `entry_gates`；取消同根 3m 首筆硬鎖；出場確認寫入冷卻 |
| `tests/test_price_action_v2_offline.py` | **新增**驗收 1–12＋七案例冒煙 |
| `tests/test_surge_continuation_offline.py` | 改呼叫 `evaluate_entry_v1` |

程式根目錄：`output/testnet_abcd_setup/code/h_surge_trend_v1/`

---

## 2. 三路徑實際設定（研究起始值，未搜參）

### 共同
- 評估秒 = `floor(decision_ts)-1`（未完成當秒不參與）
- 覆蓋率 ≥ 80%；最新成交年齡 ≤ `min(TRADE_STALE_SEC, V2_MAX_TRADE_AGE_SEC)=2s`
- 缺端點／覆蓋不足 → `DATA_INSUFFICIENT`（欄位用 `null`，不寫 0）
- 排名／新鮮度／曝險／可交易性仍為共同外層限制（engine）

### A SURGE（原爆量）
- 前 20 根已完成 3m 成交額中位數；盤中累積／elapsed → RVOL
- `RVOL_THRESHOLD=3`、`RVOL_MIN_ELAPSED_SEC=30`、`ABS≥0.5×baseline`
- 全域 dense ER₆₀≥0.35、range≥0.30%、ret₆₀／CLV 原趨勢門檻
- **A 失敗不 return 擋 B／C**

### B CONTINUATION
- `ret60≥0.35%`、`ret15≥0.10%`、`ER30≥0.35`
- 多 CLV60≥0.75／空≤0.25；距 60s 極值回撤≤0.20%
- 連續 3 個完整秒；**不要求 RVOL／30s elapsed／突破前一根 3m**
- 換 3m 棒不重置 sparse 視窗

### C REVERSAL
- 120s 極值、τ∈[5,60]s、前段≥0.40%、收復≥0.25% 且比例≥50%
- 突破前 10s 極值 ≥1 tick → 武裝；再 2 秒維持同側（共 3 秒確認）
- 確認秒**不要求再次創新高／新低**；失效則解除武裝可重找

### CHOP（僅 B／C）
- 舊窗中線 ±0.10%；60s 內側別切換≥4 → 擋
- 例外：最近 3 秒離開 [t-60,t-10) 箱體 ≥1 tick

### 去重／持倉／閘
- 同向多路徑合併 tags；多空同時 → `DIRECTION_CONFLICT`
- 同向持倉只記錄；反向 → `OPPOSITE_POSITION`（不平倉）
- 退出後冷卻 15s；候選消費後須先失效再重立；180s 最多 2 次首筆嘗試；同時一筆首筆在途
- `ENABLE_CONTINUATION_ARM=False`（程式保留，非第四路徑）

---

## 3. 測試結果

### v2 離線（`test_price_action_v2_offline.py`）
全部 PASS：01 RVOL 不足 B／C 可算；02 爆量不退化；03 多空對稱；04 反轉時序；05 未來不改過去；06 換棒不重置；07 缺資料／過期；08 CHOP／離開箱體；09 反轉解除武裝；10 多路徑不重複；11 冷卻／消費；12 風控常數與加倉步長；13 七案例冒煙。

### 遺留 cont_arm（`evaluate_entry_v1`）
`test_surge_continuation_offline.py`：**failed=0/14**

---

## 4. 七案例冒煙（固定參數；A 用占位 baseline，**不代表實盤 RVOL**）

| ID | 商品 | 錨點 | 有候選？ | 最早（錨點相對） | 路徑 | 備註 |
|---|---|---|---|---|---|---|
| M1 | MANTRAUSDT | 23:39 | 是 | −57s → 23:38:03 | CONTINUATION LONG | 錨點前已有延續 |
| M2 | MANTRAUSDT | 23:48 | 是 | +21s → 23:48:21 | CONTINUATION **SHORT** | 與「追多」意圖方向不一致 |
| M3 | MANTRAUSDT | 23:54 | **否** | — | — | 視窗內無 B／C pass |
| B1 | BIGTIMEUSDT | 23:48 | 是 | +57s → 23:48:57 | CONTINUATION LONG | |
| B2 | BIGTIMEUSDT | 23:54 | 是 | −18s → 23:53:42 | CONTINUATION LONG | 與「追空」意圖方向不一致 |
| S1 | SSVUSDT | 23:48 | **否** | — | — | 視窗內無 pass（未調參） |
| S2 | SSVUSDT | 23:57 | 是 | +28s → 23:57:28 | CONTINUATION SHORT | |

未完整模擬委託／滑價／費用 → **不宣稱策略獲利**。不要求七案皆觸發。

---

## 5. Order type 與限價要求

| 項目 | 實際值 |
|---|---|
| `ENTRY_ORDER_TYPE` | **`MARKET`**（config 現值；本次**未改**市價／限價） |
| `ENTRY_TTL_SEC` | 5.0（僅 LIMIT 模式使用） |
| `ENTRY_POST_ONLY` | False |
| Paper 成交模型 | MARKET → ask/bid 立即全成，taker |
| 出場 | 仍為 reduce-only **LIMIT**（未改） |

與使用者早先「限價進場」要求：**不一致（目前為市價）**。本次依指示不自行切換；若要改回 LIMIT，需另開任務明確指定。

---

## 6. 風控／加倉／曝險（核對＝保持）

| 項目 | 值 | 本任務 |
|---|---|---|
| Top10／5min、Top15 維持 | 10／300s／15 | 未改 |
| 排除清單 | 原 frozenset | 未改 |
| 30U×10×、最多 4 腿、0.5% 加倉 | 30／10／4／0.5% | 未改 |
| 最多 20 slot | 20 | 未改 |
| 浮盈 2% 啟動追蹤、回撤 1% | 0.02／0.01 | 未改 |
| 費率／保護單管理 | 原模型 | 未改 |
| 加倉不放寬停損 | 仍用 trail_stop | 未改 |

與「保持」清單**無發現需另報的程式不一致項**（order type 見上節，屬進場單類型而非風控數值）。

---

## 7. 尚未解決／限制

1. 案例冒煙未重放真實 20 根 baseline／排名資格 → Path A 實盤行為需 Paper 重放驗證。  
2. Dense grid（Path A）仍可在 gap≤3s 內借價；B／C 已改稀疏。  
3. `ENTRY_ORDER_TYPE=MARKET` 與早先限價要求未對齊。  
4. 診斷寫入 `signal_rejects.jsonl` 含多路徑 detail；長跑磁碟量需再觀察節流。  
5. 未做完整委託／滑價／費用模擬 → 無績效結論。  
6. cont_arm 預設關閉；若要回退需顯式 `ENABLE_CONTINUATION_ARM=True` 並改走 legacy 評估銜接（非本任務啟用）。

---

## 8. 明確確認

- **未部署**  
- **未重啟**  
- **未啟動 Paper／Testnet**  
- **未下單、未撤單**  
- **未修改 A／E**  
- **未覆蓋歷史 ledger／案例研究輸出**（新 profile 名稱已隔離）

`config_hash`（寫入當下）：以 `config.config_hash()` 為準（測試輸出曾見 `6d635cd4120640ae…`）。
