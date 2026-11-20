# NO_SIGNAL 子條件診斷（no_signal_breakdown）

> 診斷時間：2026-11-18 21:50（CST / Asia/Taipei +0800）  
> 性質：允許修改**診斷／紀錄**；**未改**門檻／TP／SL／門派啟用／路由／成交規則；**未**自動啟動交易程序；**未**開倉、未重置帳本  
> 重要：下列「第一拒絕原因」受 **detect 短路順序**影響，**不是**各條件的獨立失敗率

---

## 0. 結論摘要

| 項目 | 判定 |
|---|---|
| 停機前 959 次 FADE/MOMO 評估全 NO_SIGNAL | 成立（既有 decision_trace） |
| 離線重建（vault + 當時 personality 快照） | **主因：`VOL_X_RANGE`（量倍不在 5x~18x）** |
| 資料不足／未收盤誤用 | **未**見為主因；PIEVERSE 21:29 未收盤根有被排除 |
| 路由 `recent_volume_ratio`≈1.5~4 | **≠** detect `vol_x`；強制 MOMO 分流 ≠ 進場爆量條件 |

標記：本報告離線部分為 **「離線重建診斷」**（vault 為事後狀態 + decision_trace 時間戳 + 分流快照），**不是**逐 tick 歷史位元級重現。

---

## 1. 每門派評估數（live decision_trace）

來源：`output/decision_trace_20261118.jsonl`，`event=SIGNAL_EVAL` 且 `block_reason=NO_SIGNAL`。

| 門派 | 評估次數 |
|---|---:|
| SPIKE_FADE_1M | 568 |
| SPIKE_MOMO_1M | 391 |
| **合計** | **959** |

（其他門派在該時段 primary_only 下未成為 NO_SIGNAL 主量。）

---

## 2. 第一拒絕原因分布（離線重建；順序依賴）

重建範圍：MMT 全 37 次 + 焦點幣 + FADE 抽樣 40 → 共 **191** 次（寫入 `output/no_signal_offline_summary.json`）。

| first_reject_code | 次數 | 佔本重建樣本 |
|---|---:|---:|
| **VOL_X_RANGE** | 190 | 99.5% |
| **BODY_RATIO** | 1 | 0.5% |
| INSUFFICIENT_CLOSED_BARS / VOL_MA_ZERO / ZOMBIE / 方向閘 | 0 | 0% |

依門派：

| 門派 | VOL_X_RANGE | BODY_RATIO |
|---|---:|---:|
| SPIKE_MOMO_1M | 150 | 1 |
| SPIKE_FADE_1M（抽樣40） | 40 | 0 |

**不得解讀為：**「影線／殭屍／方向條件都通過」——多數在 `VOL_X_RANGE` 即短路，後續為 `NOT_EVALUATED`。

---

## 3. 資料不足／資料錯誤 vs 策略條件未滿足

| 類別 | 代碼 | 本重建結果 |
|---|---|---|
| 資料不足 | `INSUFFICIENT_CLOSED_BARS` | **0** |
| 資料錯誤／分母為零 | `VOL_MA_ZERO` | **0** |
| 策略條件未滿足（量倍） | `VOL_X_RANGE` | **190** |
| 策略條件未滿足（實體） | `BODY_RATIO` | **1** |
| 策略條件未滿足（影線／型態／禁空／位置） | `FADE_*` / `DIRECTION_POS_GATE` / `ZOMBIE_*` | **本樣本皆 NOT_EVALUATED（未跑到）** |

未收盤過濾：屬資料邊界處理，見 § 未收盤K；**排除未收盤根後仍有足夠 closed bars 做評估**。

---

## 4. 焦點幣個別診斷

### 4.1 MMTUSDT（37× MOMO，皆 NO_SIGNAL）

| 項目 | 值 |
|---|---|
| 模式 | **離線重建診斷** |
| 第一拒絕 | **37/37 = VOL_X_RANGE** |
| vol_x 分布 | min **0.003** / median **0.836** / max **4.306**（門檻 **[5.0, 18.0]**） |
| 最接近門檻例 | 21:27:02 → vol_x≈**4.31**（仍 <5） |
| 21:29:02 | vol_x≈**1.27**；訊號根 21:28；vault 當時無 21:29 根（excl 空） |
| 路由 recent_volume_ratio | 約 **1.76**（強制 MOMO 用）→ **不同指標** |

量倍定義（程式實際）：

- 分子：最後一根**已收盤** 1m 的 `volume`
- 分母：`mean(vol[i-VOLUME_MA_PERIOD : i])`，**排除訊號根**，`VOLUME_MA_PERIOD=20`
- 分母≤0 → `VOL_MA_ZERO`（本幣未觸發）

### 4.2 PIEVERSEUSDT（59× MOMO）

| 項目 | 值 |
|---|---|
| 第一拒絕 | **59/59 = VOL_X_RANGE** |
| vol_x | min 0.006 / median 0.710 / max **3.017**（皆 <5） |
| **21:29:02 未收盤核對** | vault 含 open_time=**21:29:00**；`now=21:29:02` → `open+60s=21:30 > now` → **已排除** |
| 實際傳入 detect 的訊號根 | **21:28:00**（closed 最後一根） |
| 該次 vol_x | ≈**2.028**（num=105020 / ma≈51781.65） |
| 15m | 最後 open **21:15**；於 21:29:02 尚未收盤（21:30 才收）；**MOMO 不讀 15m**，僅作邊界旁證 |

### 4.3 PIPPINUSDT

| 項目 | 值 |
|---|---|
| 重建筆數 | 55（含較早仍在可交易池時段） |
| 第一拒絕 | VOL_X_RANGE **54**；BODY_RATIO **1** |
| vol_x | min≈0 / median 0.715 / max **9.822**（有一次進入 5~18 後死於實體） |
| 後期路由 | WATCH_ONLY（pos≈69.6% 未達強制 MOMO）— 與「曾評估時 NO_SIGNAL」分開看 |

---

## 5. 量倍／影線／實體與門檻對照

### 5.1 detect 出口順序（讀自程式，非猜測）

**SPIKE_MOMO_1M**（`detect_spike_momo_1m`）：

1. `_closed_klines` 排除未收盤  
2. `len(closed) < VOLUME_MA_PERIOD+2` → None  
3. `vol_ma <= 0` → None  
4. `vol_x not in [SPIKE_MOMO_VOL_X_MIN, SPIKE_MOMO_VOL_X_MAX]`（5~18）→ None  
5. `_is_zombie_liquidity_1m` → None  
6. `body_ratio < SPIKE_MOMO_BODY_MIN`（0.65）→ None  
7. 紅K 且 `pos_pct < 50` → None；黑K 且 `pos_pct > 50` → None；十字線 → None  

**SPIKE_FADE_1M**（`detect_spike_fade_1m`）：

1. closed 過濾  
2. `len < VOLUME_MA_PERIOD+3` → None  
3. `vol_ma <= 0` → None  
4. `vol_x not in [5,18]`（FADE 專用門檻）→ None  
5. zombie → None  
6. SHORT 複合：`T-1紅 & 上影≥0.40 & T黑`；若成立再看 `ban_short`（24h>25% 且 pos>80%）  
7. LONG 複合：`T-1黑 & 下影≥0.40 & T紅`  
8. 皆否 → None  

### 5.2 與路由量比差異

| 指標 | 定義 | 用途 |
|---|---|---|
| `recent_volume_ratio` | mean(近 RECENT 根) / mean(近 BASELINE 根) | 分流／強制 MOMO（≥1.5 等） |
| detect `vol_x` | 單根爆量 / 前 20 根均量（排除該根） | **進場** 5x~18x |

本時段焦點幣常見：`recent_volume_ratio` 1.5~4 **已夠分流**，但 detect `vol_x` **多 <5** → 全被 `VOL_X_RANGE` 擋。

### 5.3 影線／實體

本重建樣本中，因量倍先失敗，**影線／實體實際值多為 NOT_EVALUATED**。  
唯一例外：PIPPIN 1 次過量倍後 `BODY_RATIO` 失敗（實體 <0.65）。

---

## 6. 無法重現的資料缺口

| 缺口 | 影響 | 標記 |
|---|---|---|
| 無當時 API 原始 response 快照 | 只能用 vault 事後 CSV | **離線重建診斷**（非歷史位元重現） |
| 無每輪傳入 detect 的 DataFrame 歸檔（無訊號時） | 無法證明與 live 記憶體完全一致 | UNKNOWN（一致性） |
| 舊 decision_trace 無 `first_reject_code` | 停機前 live 拒絕子碼只能重建 | 重建結果；非當下寫入 |
| frozenset → `config_hash` 不穩定 | 跨行程 hash 漂移 | 見 §8；**未**覆寫 manifest |
| FADE 全量 568 僅抽 40 | 全量第一拒絕分布未完整重建 | 抽樣；MMT/PIEVERSE 全量 MOMO 已覆蓋 |

---

## 7. 測試結果與修改檔案

### 測試

```text
python3 tests/test_no_signal_diag.py
→ all ok
```

涵蓋：有訊號／無訊號修改前後一致、方向與 entry 一致、資料不足、零量分母、門檻邊界、未收盤K排除、診斷寫入警告不改判斷。

### 修改／新增檔案

| 檔案 | 用途 |
|---|---|
| `signal_reject_diag.py` | **新增**旁路 probe／條件狀態／量倍細節／寫檔節流警告 |
| `live_paper_trader.py` | FADE/MOMO 掛 probe（不改短路與回傳）；`scan_once` 寫入 `signal_reject`；可選 `now_ms` 僅離線 |
| `audit_observability.py` | `_append_jsonl` 失敗改為有限度提示（不再 `except: pass`） |
| `tests/test_no_signal_diag.py` | **新增**決策不變回歸 |
| `tools/offline_no_signal_replay.py` | **新增**離線重建（寫 `signal_reject_offline_rebuild.jsonl`，**不**偽造 live decision_trace） |
| `output/no_signal_offline_summary.json` | 重建結果 |
| `output/no_signal_breakdown.md` | 本報告 |

### 後續 live 行為（需自行重啟程序才生效）

- 每分鐘既有掃描下，NO_SIGNAL 會帶 `signal_reject.first_reject_code` 與 conditions  
- 另寫 `output/signal_reject_diag_YYYYMMDD.jsonl`  
- **無**新增週期心跳；**未**啟動交易程序

---

## 8. config_hash 唯讀檢查（不強行改一致）

| 來源 | hash |
|---|---|
| `run_manifest.json`（凍結） | `9f20b63fa8200136` |
| 本診斷行程重算 | `b049c021d8b6fb3b`（隨行程變） |

**不穩定序列化物件（已確認）：**

- `EXCLUDED_MAJOR_BASES` : `frozenset`
- `NON_CRYPTO_SUBTYPE_KEYWORDS` : `frozenset`
- `TRADFI_BASE_BLACKLIST` : `frozenset`

`config_hash()` 使用 `json.dumps(..., default=str)`；`str(frozenset)` **元素順序不穩定**（同進程內重建 `frozenset(list(fs))` 的 `str` 亦可不同）。  
這足以造成跨啟動／跨寫入時點 hash 不一致。  

**本診斷未**改 hash 演算法、**未**覆寫舊 manifest、**未**為了看起來一致而重寫欄位。

---

## 9. FADE / MOMO return None 出口清單（證據：原始碼順序）

見 §5.1。旁路代碼對應：`INSUFFICIENT_CLOSED_BARS` / `VOL_MA_ZERO` / `VOL_X_RANGE` / `ZOMBIE_LIQUIDITY` / `BODY_RATIO` / `DIRECTION_POS_GATE` / `FADE_SHORT_PATTERN` / `FADE_BAN_SHORT` / `FADE_LONG_PATTERN`。

---

**一句話：**  
在可重建範圍內，MMT／PIEVERSE 等 MOMO 評估幾乎全部死在 **進場量倍 `vol_x` 低於 5x**；路由用的 `recent_volume_ratio` 不能當成已滿足進場爆量。未收盤 1m（如 PIEVERSE 21:29）在 detect 前有被排除，不是「用到未收盤K才沒訊號」的主因。
