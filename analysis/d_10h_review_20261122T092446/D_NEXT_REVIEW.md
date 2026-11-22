# D_NEXT_REVIEW

**產出時間**：2026-11-22（分析目錄 `analysis/d_10h_review_20261122T092446/`）  
**約束遵守**：未啟動 runner、未重啟舊 run、未下實盤單；未補造 HYPE 出場；未將 CF 結果寫入舊帳本。  
**聲明**：本文件為取證／局部診斷，**不宣稱策略已驗證**。  
**原始數據**：`next_review_data.json`；腳本 `_next_review_audit.py`。

---

## 1. 已完成／未完成

### 已完成
1. 1000SHIB 固定窗 bars 稽核（含 30m／60m 首末時間、總筆數、唯一時間戳）。
2. 「歧義」重新分項統計（不再把「無雙觸及」寫成「無不確定性」）。
3. HYPE 未平估淨 −1.7720U 公式對照 mid gross −2.11428U；未實現／已實現分開。
4. 可證實運行／停機／未知區間（wall span ≠ 連續運行）。
5. D 範圍持久化故障修復＋單元測試（失敗停新掛／開倉；不吞例外繼續無帳成交；恢復先對帳；副作用檔）。
6. 啟動設定完整快照寫入 + D 原生行情／VOL 輸入紀錄路徑（下輪 paper 生效；本輪未開跑）。
7. 固定原進場、SL=3U，四檔 TP 局部比較（費用／截止／邊界一致；VOL 明示不可重建）。
8. 舊 HYPE 與帳本保全（僅唯讀快照分析）。

### 未完成／剩餘
1. **本輪未跑 paper**：`SETTINGS_SNAPSHOT.json`／`native_quote_vol.jsonl` 僅程式已接線，尚無新運行產物。
2. **VOL_SPIKE 無法在本局部比較中重建**（缺原生 mid roll 時間序列）→ 非完整策略回測。
3. **歷史 RANGE 有效值仍 UNKNOWN**；BAR 僅能說與 ~0.01 相容，**未證明**當次執行 BAR=0.01。
4. **未 commit**（見下）。
5. 共用模組 `common/*` **未改**；若未來要把 halt 閘門抽到共用層，需另開影響評估後再動 A/B/C。

---

## 2. 修改檔案及 commit

### 修改／新增檔案（D 範圍）
| 路徑 | 變更 |
|------|------|
| `output/testnet_abcd_setup/code/d_quiet_short_v1/util.py` | `PersistError`；`atomic_write_json` 失敗改拋 PersistError |
| `output/testnet_abcd_setup/code/d_quiet_short_v1/portfolio.py` | halt 標記、side-file、`save_account` 失敗停新單、load 先對帳、queue／pending 閘門 |
| `output/testnet_abcd_setup/code/d_quiet_short_v1/engine.py` | PersistError 捕捉；停 fills；寫入 `native_quote_vol.jsonl` |
| `output/testnet_abcd_setup/code/d_quiet_short_v1/config.py` | `settings_snapshot_dict`；paths：`quote_vol`／`settings_snapshot`／`persist_halt` |
| `output/testnet_abcd_setup/code/d_quiet_short_v1/paper_main.py` | 啟動時寫完整 settings 進 LAUNCH_META + SETTINGS_SNAPSHOT |
| `output/testnet_abcd_setup/code/d_quiet_short_v1/test_persist_halt.py` | 5 項單元測試 |
| `analysis/d_10h_review_20261122T092446/_next_review_audit.py` | 本輪稽核／TP 腳本 |
| `analysis/d_10h_review_20261122T092446/next_review_data.json` | 本輪數值產物 |
| `analysis/d_10h_review_20261122T092446/D_NEXT_REVIEW.md` | 本文件 |

### 共用模組影響（先報、未改 A/B/C）
- **本次只改 `d_quiet_short_v1/*`**，與 A/B/C 程式碼路徑隔離。
- `util.atomic_write_json` 為 **D 套件內複本**，不是 `common` 共用寫入器 → **不影響 A/B/C**。
- `load_account` 仍呼叫既有 `common.settlement_apply.recover_account_settlement_journal`（恢復對帳，行為與前一致，未改 common）。

### Commit
**沒有。**（本輪未建立 git commit。）

---

## 3. 實際執行的測試與結果

```text
python -m unittest d_quiet_short_v1.test_persist_halt -v
# 工作目錄: output/testnet_abcd_setup/code
# Ran 5 tests in ~0.08s — OK
```

| 測試 | 結果 |
|------|------|
| `test_atomic_write_raises_persist_error` | OK |
| `test_save_account_marks_halt_and_side_file` | OK |
| `test_queue_blocked_when_halted` | OK |
| `test_load_reads_halt_marker_before_resume` | OK |
| `test_clear_halt_after_reconcile` | OK |

稽核腳本：

```text
python analysis/d_10h_review_20261122T092446/_next_review_audit.py
# → next_review_data.json
```

---

## 4. 資料問題修正

### 4.1 1000SHIB「30m bars=61」
| 項目 | 值 |
|------|-----|
| 進場時間 | `2026-11-21T19:31:43.331419+08:00` |
| 30m 窗 | start=`19:31:43` → end=`20:01:43` |
| 30m 首／末 K | `19:31:00` / `20:01:00` |
| 30m 總筆數／唯一時間戳 | **31 / 31** |
| 重複／缺口 | 無 |
| 60m 總筆數／唯一時間戳 | **61 / 61**（首 `19:31` 末 `20:31`） |

**結論**：`supplement_data.json` 與重算一致（30m=31、60m=61）。**非重複 K、非切窗錯誤。**  
若把 60m 的 `bars=61` 讀成 30m，屬排版／閱讀混淆。  
**無需重算固定窗結果**（`diffs_vs_stored=[]`）。

### 4.2 歧義重新定義（11 筆進場）
| 類型 | 合計 |
|------|------|
| 同根 TP/SL 雙觸及（對原 TP15/SL3 路徑掃描） | **0** |
| 進場當根含進場前價格 | **11** |
| 截止當根含截止後價格 | **1**（HYPE 未平） |
| 資料缺口 | **0** |
| 截尾 | **0** |
| last OHLC vs 原 mid/ask 價種差異 | **11**（CF 一律存在） |

**規則**：不得將「沒有雙觸及」寫成「沒有不確定性」。  
跨截止 K 若無法排除截止後價格 → **不得當完整可用行情**（HYPE MTM 已標 `cutoff_bar_incomplete`）。

### 4.3 HYPE 未平估淨 −1.7720U
| 項目 | 值 |
|------|-----|
| 估值時間（account.updated_at） | `2026-11-21T21:03:11.682901+08:00` |
| 行情 bar open | `2026-11-21T21:03:00+08:00` |
| 價格種類 | **sibling C 1m OHLC close**（非 mid／非 ask） |
| 價格 | **59.738** |
| 數量 | 5.04 |
| 進場價／進場費 | 59.446 / 0.14980392U |
| 公式 | `cycle_net=(entry−mark)×qty − \|mark×qty\|×0.0005 − entry_fee` |
| 未實現 gross（OHLC close） | ≈ **−1.4717U** |
| 未實現 cycle_net（若按 close 平） | ≈ **−1.7720U**（吻合先前報告） |
| 對照 mid=59.8655 | gross=**−2.11428U**；含費 cycle≈**−2.4149U** |
| 帳本已實現 realized_net_u | 與 HYPE 未平嚴格分開；**未把 MTM 記入已實現** |
| 保全 | **未補造出場、未改舊帳本** |

### 4.4 運行／停機／未知
| 口徑 | 小時 |
|------|------|
| Wall span（首證據→account 截止） | **≈6.50h**（僅起訖跨度） |
| 可證實運行（證據間隙 ≤120s） | **≈5.34h** |
| 可歸類停機（含 PermissionError 間隙） | **≈1.17h** |
| 未知（≥15m 無證據且無 PE） | **≈0h**（本輪閾值下） |

PermissionError 時間戳：
- `2026-11-21T18:01:11+08:00`
- `2026-11-21T19:41:09+08:00`
- `2026-11-21T21:02:28+08:00`（近帳本截止）

停機段（PE 後至下一證據）：
1. `18:01:11` → `18:36:43`（≈0.59h）
2. `19:41:09` → `20:15:33`（≈0.57h）

**注意**：`LAUNCH_META.json` 的 `launched_at=20:15:33` 反映較晚覆寫／重啟；外層 `paper_bcd_launch_meta_d.json` `started_at`≈`18:36:43`。Wall ~6.5h **不是**連續運行證明。

### 4.5 歷史設定口徑（沿用）
- 訊號 `bar_amps` 證據：主交易時段 BAR 上限 **≥約 0.00952**，與 **0.01 相容**。
- **不得宣稱已證明 BAR=0.01** 為當次執行閾值；現行程式 `BAR_AMP_MAX=0.0075` 與訊號證據不一致，屬已知張力。
- **RANGE 歷史有效值：UNKNOWN**（外層 meta 曾見 0.02，config 為 0.015，未完整重現證明）。
- 本輪 TP 比較標籤：`NEW_TEST_SETTINGS_fixed_entry_SL3_no_VOL`（**新測試設定**，非完整重現歷史）。
- 訊號數以 `signals.jsonl` 計，**不由成交反推**。

---

## 5. 四個 TP 局部比較

**設定（新測試）**：固定原進場價／量／進場費；SL 毛利 **3U**；截止=`account.updated_at`；費用單邊 5bps 計入 cycle_net；1m OHLC 邊界處理一致。  
**VOL_SPIKE：無法重建** → **不是完整策略回測**。  
**舊 HYPE／帳本未混入**；下表為診斷 CF。

### 匯總
| 臂 | 保守 Σ cycle_net | 樂觀 Σ cycle_net | 狀態 |
|----|------------------|------------------|------|
| TP 價格幅度 0.5% | **−30.9997** | **−30.2735** | 10 EXITED + 1 OPEN |
| TP 價格幅度 1% | **−35.5016** | **−34.7755** | 同上 |
| TP 價格幅度 1.5% | **−35.5016** | **−34.7755** | 同上 |
| 原每倉毛利 TP15U | **−35.5016** | **−34.7755** | 同上 |

差異來源：僅 **ADA** 在 0.5% 臂先觸 TP（+1.20U）；其餘臂 ADA 亦先觸 SL。1%／1.5%／TP15U 本樣本路徑相同。雙觸及=0；HYPE 未平界限用截止 K 高／低（保守／樂觀），**截止 K 非完整可用行情**。

### 逐筆（cycle_net U）
| 商品 | 0.5% 保守／樂觀 | 1% | 1.5% | TP15U | 備註 |
|------|-----------------|----|------|-------|------|
| ADAUSDT | **+1.2005／+1.2005（TP）** | −3.3014 SL | −3.3014 SL | −3.3014 SL | |
| LINKUSDT | −3.3015 SL | −3.3015 | −3.3015 | −3.3015 | 同 |
| AVAXUSDT | −3.2959 SL | −3.2959 | −3.2959 | −3.2959 | 同 |
| DOGEUSDT | −3.3015 SL | −3.3015 | −3.3015 | −3.3015 | 同 |
| WLDUSDT | −3.3014 SL | −3.3014 | −3.3014 | −3.3014 | 同 |
| BNBUSDT | −3.2964 SL | −3.2964 | −3.2964 | −3.2964 | 同 |
| XLMUSDT | −3.3015 SL | −3.3015 | −3.3015 | −3.3015 | 同 |
| XRPUSDT | −3.3014 SL | −3.3014 | −3.3014 | −3.3014 | 同 |
| 1000SHIBUSDT | −3.3015 SL | −3.3015 | −3.3015 | −3.3015 | 同 |
| SOLUSDT | −3.3009 SL | −3.3009 | −3.3009 | −3.3009 | 同 |
| HYPEUSDT | −2.4981／−1.7720（未平界限） | 同 | 同 | 同 | 未實現；非已平倉 |

---

## 6. 下一步 paper 方案（**尚未啟動**）

1. **對帳後再啟**：確認 `fills.jsonl` ↔ `account_state` ↔（若有）settlement journal；清 `PERSIST_HALT.json` 僅在對帳完成後（`clear_persist_halt_after_reconcile`）。
2. **保全舊 run**：舊 HYPE／舊帳本／本分析快照只讀；新 paper 用**新 runtime_root** 或新 PROFILE，避免混帳。
3. **啟用本輪已接線記錄**：確認啟動後存在 `SETTINGS_SNAPSHOT.json`、`LAUNCH_META.settings`、`native_quote_vol.jsonl`。
4. **建議紙上參數標籤**：明示 `NEW_TEST`；BAR／RANGE 若採 0.01／（待定）須寫「新測試設定」，不冒稱歷史重現。
5. **TP 候選**：本局部樣本僅 0.5% 對 ADA 有差；**不足以選參**。若要 paper 比較，應用原生 mid＋VOL 紀錄重跑，而非本 OHLC CF。
6. **持久化**：Windows 檔案鎖仍可能觸發 PersistError；halt 後應告警並人工處理，禁止自動吞錯續開倉。
7. **本輪不啟動** runner／不下單。

---

## 7. 總結句

資料面：1000SHIB 30m=31（非 61）；歧義已分項；HYPE −1.7720 為 OHLC close 未實現估價，與 mid gross −2.11428 價種不同。  
工程面：D 專用 persist halt＋測試已落地；設定／行情 VOL 紀錄已接線但未開跑。  
TP 局部：無 VOL、非完整回測；四臂皆大幅為負，僅 0.5% 多一筆 ADA TP。  
**策略尚未驗證；paper 未啟動。**
