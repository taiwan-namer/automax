# D_OBSERVATION_PLAN

**狀態：尚未啟動，等待確認。**  
本輪只完成準備與隔離測試；**不自行啟動**觀測 runner。  
不掃 TP／SL；不改交易規則（訊號／RANGE／BAR／限價偏移／撤單條件沿用現行 config）。

---

## 1. 新 runtime_root 與模式

| 項目 | 值 |
|------|-----|
| 建議 runtime_root | `c:\Users\Admin\MyProjects\automax\output\paper_d_observe_local\obs_<RUN_ID>` |
| 建議 run_id | `obs_20261122_confirm1`（可改；須唯一） |
| 模式 | **`--observe-only`** → `OBSERVATION_ONLY` |
| 舊 root | **禁止**：`output\paper_bcd_local\data_d`（程式硬拒） |
| 舊 HYPE | paper 殘倉**維持原狀**；不補造出場；不經實盤／testnet 處理 |
| 成交 | **不產生** paper OPEN／CLOSE；**不掛**交易所單 |
| 影子觸價 | `observational_touch`（獨立 `shadow_lifecycle.jsonl`／`observation_events.jsonl`）；**不稱成交、不計盈利** |

### 為何不能直接用現有 `--scan-only`
現行 scan-only **不會**在「無持倉／無掛單」時持續寫完整 `native_quote_vol`（該檔先前僅在 manage 對 positions／pending 取樣）。  
且 scan-only 仍走帳本／pending 路徑語意。  
→ **不能**用 scan-only 冒稱已完成觀測接線。本輪新增 **`--observe-only`**。

---

## 2. 設定及欄位

### 新設定標籤（非歷史重現）
- `settings_tag` / 文件標示：**`NEW_OBSERVATION_SETTINGS`**
- 現行 config：**RANGE_AMP_MAX=0.015**、**BAR_AMP_MAX=0.0075**
- 歷史口徑不變：BAR 僅能說主時段上限 ≥約 0.00952、與 0.01 相容；**不得**把本觀測設定回填成歷史已證明 BAR=0.01；RANGE 歷史有效值仍 **UNKNOWN**

### 啟動時持久化
| 檔案 | 內容 |
|------|------|
| `SETTINGS_SNAPSHOT.json` | 完整設定＋`mode_flag`＋`run_id`＋`git_commit`＋`written_at` |
| `LAUNCH_META.json` | `mode=OBSERVATION_ONLY`、`settings`、路徑、`observe_only=true`、`no_open_close=true` |
| `OBSERVATION_META.json` | run_id／版本／RANGE／BAR／commit |
| `native_quote_vol.jsonl` | 原生 bid/ask/mid、時間戳、VOL 滾動、data_valid |
| `observation_events.jsonl` | 訊號再檢查／觸價／缺口告警 |
| `shadow_lifecycle.jsonl` | 影子開／撤／observational_touch |
| `signals.jsonl` | 訊號（標 `OBSERVATION_ONLY`） |
| `observation_health.json` | 心跳健康 |

### 每訊號／影子生命週期欄位（已接線）
- signal_id、symbol、訊號時間、P、影子限價  
- 引用 15m `ref_15m_open_time`／`ref_15m_close_time` 與 range/bar  
- **分欄**：`scan_time_bucket`（掃描牆鐘桶）≠ 引用 K 線時間桶  
- 訊號年齡、最後重新檢查時間、行情／資料年齡  
- 決策時已收盤 1m 報酬／振幅（缺失標 MISSING）  
- 原生 bid/ask/mid、fetched_at、received_at  
- VOL amp、roll 覆蓋樣本數、T  
- quiet／depth／PRICE_DROP 等 checks  
- 觸價：`touch_price_kind=bid`、`touch_compare=bid_ge_shadow_limit_px`  
- 執行順序：**`revalidate_then_touch`**（先重新驗證，再判斷觸價）  
- BTC／ETH 同時段 mid（不可得 → MISSING）  

只記錄、**不新增過濾門檻**。未來分析不得用觸價後資料反推當時應否接受。

---

## 3. 測試結果

```text
cd c:\Users\Admin\MyProjects\automax\output\testnet_abcd_setup\code
$env:PYTHONPATH = "c:\Users\Admin\MyProjects\automax;c:\Users\Admin\MyProjects\automax\output\testnet_abcd_setup\code"
python -m unittest d_quiet_short_v1.test_observe_only -v
# Ran 5 tests — OK
```

| 測試 | 結果 |
|------|------|
| 拒舊 `paper_bcd_local/data_d` | OK |
| 影子觸價不寫 OPEN/CLOSE／標 NOT a fill | OK |
| SETTINGS_SNAPSHOT／LAUNCH／quote_vol 欄位 | OK |
| 過期行情 → DATA_INVALID（非無訊號） | OK |
| observe 入口存在、不產生 fills | OK |

### Diff 摘要（僅 D）
- 新增 `observe.py`（影子／observational_touch／隔離 root 檢查）  
- 新增 `run_observe_loop`／`observe_scan_once`／`observe_manage_shadows`（`engine.py`）  
- `paper_main.py`：`--observe-only`／`--run-id`／`--max-events`／`--disk-limit-mb`；拒與 `--allow-fills`／`--testnet` 併用  
- `market.py`：`fetch_last_closed_1m_stats`  
- `config.py`：觀測路徑＋NEW_OBSERVATION_SETTINGS 註記  
- `test_observe_only.py`  

**未改** A／B／C；**未改** TP／SL／進場規則數值。

---

## 4. 精確啟動／停止命令（供確認；尚未執行）

### 啟動前請先確認
1. 舊 `paper_bcd_local\data_d` **不要**當 `--runtime-root`。  
2. 不要加 `--allow-fills`、不要加 `--testnet`。  
3. 確認磁碟空間（預設上限 512MB 觀測日誌合計）。  

### 建議環境變數與啟動（PowerShell）

```powershell
$RUN_ID = "obs_20261122_confirm1"
$ROOT = "c:\Users\Admin\MyProjects\automax\output\paper_d_observe_local\$RUN_ID"
New-Item -ItemType Directory -Force -Path $ROOT | Out-Null

$env:PYTHONPATH = "c:\Users\Admin\MyProjects\automax;c:\Users\Admin\MyProjects\automax\output\testnet_abcd_setup\code"
$env:ENABLE_BINANCE_TESTNET_ORDER = "0"

# 建議工程覆蓋時長（非策略驗證門檻）— 例：6 小時
python -m d_quiet_short_v1.paper_main `
  --paper `
  --observe-only `
  --runtime-root $ROOT `
  --run-id $RUN_ID `
  --hours 6 `
  --disk-limit-mb 512 `
  --max-events 0
```

可選：先 `--once` 做單輪煙霧（仍會打公開行情；**確認前請勿跑**若你要求連煙霧也不要）：

```powershell
python -m d_quiet_short_v1.paper_main --paper --observe-only --runtime-root $ROOT --run-id $RUN_ID --once
```

### 停止
- 前景：`Ctrl+C`（SIGINT；不平殺、無補造出場）  
- 或對 PID：`Stop-Process -Id (Get-Content "$ROOT\output\testnet_d_quiet_short_v1\paper.pid")`  

資料根實際輸出目錄：`$ROOT\output\testnet_d_quiet_short_v1\`。

---

## 5. 建議觀測時段與健康檢查

| 項目 | 建議 |
|------|------|
| 時段 | 至少覆蓋 1～2 個完整 15m 訊號窗＋其後 manage 循環（工程覆蓋）；**小時數／筆數不是策略通過門檻** |
| 健康 | 每 ~60s 更新 `observation_health.json`；日誌有 `[觀測心跳]` |
| 抽查 | `SETTINGS_SNAPSHOT.json` 含 BAR=0.0075／RANGE=0.015／`NEW_OBSERVATION_SETTINGS`；`native_quote_vol.jsonl` 有 bid/ask/mid；`fills.jsonl` **不存在或空** |
| 缺口 | `OBSERVE_*_FAIL`／`DATA_INVALID`／`data_valid=false` = 資料無效告警，**不得當成無訊號** |
| 持久化失敗 | `OBSERVE_PERSIST_FAIL` 告警並標記；不靜默當無訊號 |

---

## 6. 明確聲明

**尚未啟動，等待確認。**  
觀測時長僅用於工程與資料覆蓋；**不得**把固定小時數或固定交易／觸價筆數當成策略驗證通過門檻。  
策略仍**未驗證**。
