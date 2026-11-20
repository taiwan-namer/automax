# PAPER_BULK_PRELAUNCH_CHECK — paper_bulk_test_v1

- 產生時間（Asia/Taipei）：2026-11-19T12:15:30+08:00
- 本輪：**僅隔離修改／測試／獨立 runtime 準備**；未停舊程序、未改舊帳戶、未初始化正式 bulk 帳戶、未長時間啟動、未覆蓋正式 package
- 是否具備「等待批准啟動」條件：**是（預啟動驗收通過；政策項見 §8 待確認）**

---

## 0. 現況（唯讀核對）

| 項目 | 實測 |
|------|------|
| 正式程序 | PID **33829**，`python -m breakout_follow_v1.paper_main --paper --hours 0.0`（自 11:53，仍存活） |
| 正式 run_id | `20261119T115315_b984d9a7` |
| 正式帳戶 | `…/output/breakout_follow_v1/account_state.json` |
| 持倉／日損鎖／現金 | **0**／**True**／≈**9668.78** |
| 正式源碼 `DAILY_LOSS_LOCK_USDT` | **300.0**（未改動） |
| 正式 `output/paper_bulk_test_v1/` | **不存在**（本輪未初始化） |

---

## 1. 補驗收結果與證據

### 1.1 測試

| 項目 | 結果 |
|------|------|
| `run_all_tests.py` | **68/68 PASSED**（含新增對帳與 flock） |
| 摘要 | `worktrees/…/test_results/summary.json`；封存副本見 seal |

### 1.2 舊 profile 預設去污染

| 檢查 | 預期 | 實際 | 結果 |
|------|------|------|------|
| 模組預設 `DAILY_LOSS_LOCK_USDT` | 300 | 300 | 通過 |
| `apply_profile(breakout_follow_v1)` | 10000／3／300 | 同 | 通過 |
| `apply_profile(paper_bulk_test_v1)` | 50000／20／2000 | 同 | 通過 |
| bulk → 切回 legacy | 還原 10000／3／300 | 同 | 通過 |
| 正式運行中帳戶／日損 | 不動 | PID 33829 仍在；帳戶未改 | 通過 |

證據測試：`tests.test_daily_loss_lock_2000.test_legacy_default_is_300_bulk_is_2000`

### 1.3 完整成交↔帳戶對帳（非空樣本）

「空樣本 n/a」**不等於**對帳通過。本輪在獨立 tmp 建立有成交案例：

開倉 → 部分止盈 → 剩餘平倉；逐項 expected／actual／diff 全過。

證據檔：`output/paper_bulk_test_v1_setup/RECONCILIATION_DIFF_EVIDENCE.json`（24 列，`all_ok=true`）

| 項目 | 預期 | 實際 | 差異 |
|------|------|------|------|
| open.qty | 100 | 100 | 0 |
| open.fee_u | 5.0 | 5.0 | 0 |
| open.cash | 48995 | 48995 | 0 |
| unrealized（helper vs 公式） | ≈70.9304 | 同 | 0 |
| equity.with_unrealized | ≈50065.93 | 同 | 0 |
| partial.qty | 30／剩餘 70 | 同 | 0 |
| partial.net／fee | ≈42.2602／1.5219 | 同 | 0 |
| partial 後保證金仍全額 | 1000 | 1000 | 0 |
| final.net／fees 合計 | ≈77.6261／10.0625 | 同 | 0 |
| flat cash＝capital+realized | ≈50114.8863 | 同 | ~1e-11 |
| fills 序 | OPEN→CLOSE_PARTIAL→CLOSE | 同 | — |
| **funding_data_gap** | **True（明示缺漏）** | **True** | — |

**資金費：`funding_data_gap=true`，未完整計費，不得宣稱完整淨收益。**

測試資料僅寫入 `paper_bulk_test_v1_setup/prelaunch_recon_*` 與測試 tmp；**未**寫入正式 `output/breakout_follow_v1` 或正式 `output/paper_bulk_test_v1`。

### 1.4 同 profile 原子互斥

| 機制 | 狀態 |
|------|------|
| `fcntl.flock(LOCK_EX\|LOCK_NB)` on `paper_profile.lock` | **已實作**；持鎖至進程結束 |
| 僅查 PID 再啟動 | **不足**；現為輔助檢查，主互斥為 flock |
| 測試 | `test_profile_flock_is_exclusive` 通過 |

### 1.5 封存 hash 一致

| 項目 | 值 |
|------|-----|
| Seal | `…/fix_source_sealed_20261119T121459` |
| Aggregate SHA256 | `716f24017a83632216a85b1b1a58f03ed3b5de2f058977b0ea374f4b2701d6ac` |
| Runtime↔Seal 檔案核對 | **全部 match**（`RUNTIME_MANIFEST.hash_verify_all_match=true`） |

關鍵檔 hash：

| 檔案 | sha256 |
|------|--------|
| config.py | `b159d639988e8e050da469b10388b9eb74c914642625d8bccb4e5a20dad03ce0` |
| profiles.py | `8d891476202fa85509e71eea3908c50bb7cfbaa48347d5714a8cf1acec02c9dc` |
| paper_main.py | `60043be0dd5715e29be06d2eb3540fcd133476910f329c248ff5a68121496979` |
| portfolio.py | `af494ebd930e54c8b9d5d2e30897f35a7561e1ed7093ec9ae15a15f83fac7984` |
| engine.py | `1bc865b308e2bf64ea48d968590eef0551468d0bdab12e864be8361ccad691d0` |
| account.py | `f4963888b99e399b63540daf5d109b53134ac50a4b3f3d6d2ecdf6e6ebf30ad6` |

---

## 2. 獨立部署目錄／import／完整路徑

**不得**在可改動 worktree 上長跑；**不得**覆蓋 `/Users/alan/MyProjects/automax/breakout_follow_v1/`（正式 PID 33829 使用中）。

| 角色 | 絕對路徑 |
|------|----------|
| 固定執行碼 | `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T121459/code/` |
| 資料根 `BREAKOUT_RUNTIME_ROOT` | `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T121459/data_root/` |
| 實際 import | `…/runtime_…/code/breakout_follow_v1/config.py`（已 probe，非 live package、非 worktree） |
| 帳戶（啟動後才會建立） | `…/data_root/output/paper_bulk_test_v1/account_state.json` |
| fills／trades／equity／decision_trace／review | `…/data_root/output/paper_bulk_test_v1/*` |
| PID／鎖 | `…/paper.pid`、`…/paper_profile.lock` |
| Run 包 | `…/data_root/output/paper_bulk_test_v1/runs/<RUN_ID>/` |
| Vault | `…/data_root/data/paper_bulk_test_v1/kline_vault_v2_clean/` |

路徑一律經 `RUNTIME_ROOT` **resolve 為絕對路徑**；cwd 改變不會寫到另一份相對目錄。  
目前 `data_root` **尚無帳戶檔**（未初始化）。

啟動腳本：

- `…/code/run_paper_bulk.sh`（自動設 `BREAKOUT_RUNTIME_ROOT` + `PYTHONPATH`）
- `…/code/stop_paper_bulk.sh`（PID+profile 核對後 SIGTERM）

---

## 3. 完整有效設定與版本

### 3.1 Profile 對照

| 參數 | 舊 `breakout_follow_v1` | `paper_bulk_test_v1` |
|------|-------------------------|----------------------|
| 本金 | **10000** | **50000** |
| 最多倉 | **3** | **20** |
| 日損鎖 | **300** | **2000** |
| 保證金／槓桿／名目 | 1000／10／10000 | 同（沿用） |
| 單筆風險／每標的 | 100／1 | 同 |
| 冷卻／反手窗／上限 | 3／30／2 | 同 |
| 部分止盈／trail／ATR／VOL | 0.30／100／1.5／1.5／3.0 | 同 |
| TOP_N | **10**（不擴大） | **10** |
| CODE_FIX_TAG | `…_trail_after_partial` | `…_trail_after_partial_bulk_v1` |

### 3.2 版本識別

| 項 | 值 |
|----|-----|
| Seal stamp | `20261119T121459` |
| Aggregate | `716f2401…1d6ac` |
| config_hash（bulk overlay） | `6106755bb3d0fe06` |
| config_hash（legacy overlay） | `ad6ee02a8f23b67e` |
| 測試 | 68/68 |

---

## 4. 核對過的啟動／停止／驗收命令

### 4.1 `--once` 語意

`--once` = **完整一輪掃描**，可觸發模擬開／平倉；**不是純初始化**。  
若批准長跑：直接 `--hours 0` 即可首次建帳戶／run（`load_or_create_account`）；**不必**先跑 `--once`。

### 4.2 建議單一啟動流程（待批准後才執行）

```bash
# 工作目錄與 import 固定在 runtime code；資料寫入 data_root
/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T121459/code/run_paper_bulk.sh --hours 0
```

等價：

```bash
export BREAKOUT_RUNTIME_ROOT="/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T121459/data_root"
export PYTHONPATH="/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T121459/code"
cd "$PYTHONPATH"
python3 -u -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 \
  --runtime-root "$BREAKOUT_RUNTIME_ROOT" --hours 0
```

啟動時會：`apply_profile` → **flock** → 進程／PID 輔助檢查 → 建 run／帳戶（若不存在）。

### 4.3 停止（必須核對 PID 身分與 profile）

```bash
…/code/stop_paper_bulk.sh
# 或
python3 -u -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 \
  --runtime-root "$BREAKOUT_RUNTIME_ROOT" --stop
```

停止邏輯：讀 PID 檔 → 確認進程存活 → cmdline 含 `paper_main` → **profile 相符** → 才 SIGTERM。  
**拒絕**僅因舊 pid 檔存在就殺進程；pid 不存活／profile 不符則失敗返回。

**本輪未對 PID 33829 執行停止。**

### 4.4 驗收命令（唯讀）

```bash
# 進程
ps -p "$(cat "$BREAKOUT_RUNTIME_ROOT/output/paper_bulk_test_v1/paper.pid")" -o pid,etime,command=

# 帳戶／持倉
python3 -c "import json;print(json.load(open('$BREAKOUT_RUNTIME_ROOT/output/paper_bulk_test_v1/account_state.json')))"

# 鎖
cat "$BREAKOUT_RUNTIME_ROOT/output/paper_bulk_test_v1/paper_profile.lock"

# hash 對齊 seal
python3 -c "import json;print(json.load(open('…/runtime_bulk_v1_20261119T121459/RUNTIME_MANIFEST.json'))['hash_verify_all_match'])"
```

---

## 5. 反手政策（現行行為｜不擴大）

| 項 | 現行實際 |
|----|----------|
| 程式入口 | `engine.execute_reverse`；掃描遇反向完整突破訊號時呼叫（約 L331） |
| 觸發條件 | 已有持倉 **且** `sig.side != pos.side`（完整反向突破訊號，非僅價格反向） |
| 行為 | 先平舊倉；成功後才評估開反向新倉；平倉失敗不開；開倉被拒保留已平 |
| 同標的多空 | 禁止同時持有；`try_open` 亦拒 `SAME_SYMBOL_POSITION` |
| 日損鎖 | 禁止反向**新倉**，仍允許平倉 |
| 窗口限制 | `REVERSE_WINDOW_MINUTES=30`，`MAX_REVERSES_PER_WINDOW=2` |
| 本輪變更 | **無**；未擴大反手權限 |

**待確認（§8）：** 先前背景有「不得開反向新倉」約束；現行碼在完整反向突破後**會**嘗試開反向新倉。缺乏明確已批准「允許反手開倉」文件前，標為 **待確認**，不自行裁定關閉或放寬。

---

## 6. 行情與 API（兩程序並行）

| 機制 | 現行 |
|------|------|
| 限流 | 進程內 `KLINES_API_CONCURRENCY=12` + `KLINES_MIN_INTERVAL_SEC=0.02`；**兩程序各自計數，共享交易所配額** |
| HTTP 429 | `market_data` 僅 `raise_for_status()`；**無專用 429 退避重試**（風險已記錄，本輪不改策略／限流參數） |
| 過期行情 | `KLINE_MAX_AGE_SEC=90` → `STALE_KLINE` 拒絕訊號；報價另有 `QUOTE_MAX_AGE_SEC`／`BOOK_TICKER_MAX_AGE_SEC` |
| 宇宙外持倉 | `symbols = universe ∪ positions.keys()`；掉榜標的**持續**拉 K／報價並 `manage_position` 退出 |
| TOP_N=10 | 漲跌各最多 10 → 合格宇宙約 ≤20；**不擴大宇宙**。`MAX_CONCURRENT_POSITIONS=20` 是倉位上限，**不保證**同時填滿 20；宇宙不足時開倉機會受限 |

---

## 7. 啟動後查看位置

皆在 `BREAKOUT_RUNTIME_ROOT` 下（批准啟動後才會出現）：

| 用途 | 路徑 |
|------|------|
| 帳戶 | `…/output/paper_bulk_test_v1/account_state.json` |
| 成交 | `…/fills.jsonl`、`…/trades.csv` |
| 權益 | `…/equity_snapshots.jsonl` |
| 決策 | `…/decision_trace.jsonl` |
| 復盤 | `…/review.md` 與 `…/runs/<RUN_ID>/` 內報告／快照／`data_quality.md` |
| 當前 run 指針 | `…/current_run.json` |

正式舊輸出仍在：`/Users/alan/MyProjects/automax/output/breakout_follow_v1/`（本輪未寫入）。

---

## 8. 尚待確認的政策與阻擋事項

| ID | 項目 | 狀態 |
|----|------|------|
| P1 | 反手後是否允許開反向新倉（現行碼會開；背景約束曾禁止） | **待確認** — 不自行改碼 |
| P2 | 部分平倉不按比例釋放保證金 | 沿用既有；若需改須另批 |
| P3 | 並行兩程序無跨進程 429 退避 | 已知風險；未改 |
| P4 | 使用者批准正式 bulk **初始化＋長跑** | **未批准** — 本輪未做 |
| P5 | 停舊程序／改舊帳戶／解日損鎖 | **未批准** — 本輪未做 |
| B1 | 長跑必須用本報告 runtime（seal `T121459`），禁止 worktree | 阻擋誤用 |

---

## 9. 本輪明確未做

- 未停止 PID 33829  
- 未修改正式 package／舊帳戶／舊日損  
- 未建立正式 `output/paper_bulk_test_v1/account_state.json`  
- 未執行 `--once` 或長時間 bulk 程序  
- 未擴大宇宙／反手／策略參數（僅隔離、路徑、鎖、測試、文件）

---

## 10. 相關產物索引

| 檔案 |
|------|
| `…/PAPER_BULK_PRELAUNCH_CHECK.md`（本檔） |
| `…/PAPER_BULK_TEST_READY_REPORT.md` |
| `…/fix_source_sealed_20261119T121459/` |
| `…/runtime_bulk_v1_20261119T121459/` |
| `…/RECONCILIATION_DIFF_EVIDENCE.json` |
| `…/PRELAUNCH_SEAL_POINTER.json` |
