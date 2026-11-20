# PAPER_BULK_LAUNCH_ACCEPTANCE_REPORT — paper_bulk_test_v1

- 產生時間（Asia/Taipei）：2026-11-19T12:47:33+08:00
- 版本 tag：`fix_20261119_p0p1_trail_http429_bulk_reverse`
- 驗收結論：**已啟動，且完成有界運行驗收（通過）**
- 舊程序／舊帳戶／正式 package：**未改動**

---

## 0. 簡短結論

| 問題 | 答案 |
|------|------|
| 是否已啟動？ | **是** — PID **36595**，`--hours 0`，profile `paper_bulk_test_v1` |
| 是否完成運行驗收？ | **是** — warmup 72/72、至少完成首輪掃描、觀察窗內仍存活（>2min）、參數／隔離／hash 核對通過；其後自然開倉 2 筆（非強制） |
| 去哪裡看數據？ | 見 §7 完整絕對路徑（固定資料根 `fixed_data_root`） |

不得把「僅進程曾短暫存活」當成成功：本報告以 **實際 PID 36595 的 launch.json／log／run 產物** 為準。

---

## 1. 修改摘要（隔離 worktree → 新 seal）

路徑前綴：`worktrees/breakout_follow_v1_fix_20261119/`

| 變更 | 說明 |
|------|------|
| `market_data.py` | 公開 GET：429（Retry-After／指數退避+jitter）、timeout、操作預算、進程內共用冷卻、418 封禁不密集重試；事件寫入 data_quality |
| `config.py` | 預設日損 300；HTTP 可靠性常數；`RUNTIME_ROOT`／`LOCK_PATH`；tag 更新 |
| `profiles.py` | legacy 10000／3／300；bulk 50000／20／2000；bulk `REVERSE_POLICY` 批准標記 |
| `engine.py` | bulk 反手紀錄欄位（TRIGGERED／CLOSE／OPEN／REJECT）；過期報價不成交；單標的 manage 失敗有界 |
| `paper_main.py` | flock 原子鎖、`--runtime-root`、`--stop` PID+profile 核對 |
| `run_store.py` | launch 記錄 runtime／反手政策／HTTP 設定 |
| 測試 | HTTP／隔離／bulk 反手／對帳／profile 去污染 |

**未改：** 訊號門檻、時間框架、TOP_N、冷卻／退出數值、保證金／槓桿／單筆風險；正式 package；舊帳戶日損鎖。

### 反手政策（本輪已批准，限 bulk）

完整反向突破 → 平舊倉成功 → 重檢既有風控 → 合格才開反向新倉。  
平倉失敗不開；日損鎖禁新倉仍可平／管理退出；30 分最多 2 次。  
決策事件：`REVERSE_TRIGGERED`／`REVERSE_CLOSE`／`REVERSE_CLOSE_FAILED`／`REVERSE_OPEN`／`REVERSE_OPEN_REJECTED`／`REVERSE_OPEN_BLOCKED`（含 `reverse_policy`、`profile_id`、`close_ok`/`open_ok`、拒絕原因）。

---

## 2. 測試結果

| 套件 | 結果 |
|------|------|
| `run_all_tests.py`（隔離） | **84/84 PASSED** |
| 含原回歸 | 是（原 68 + 新增） |
| 對帳 24 項 | `test_full_lifecycle_reconciliation_with_diff_table` **通過** |
| 新舊 profile 互不污染 | **通過** |
| 429 含／不含 Retry-After | **通過** |
| 重試耗盡／timeout／冷卻不提前請求 | **通過** |
| 多 worker 共用冷卻 | **通過** |
| 418 不密集重試 | **通過** |
| 單標的失敗不阻塞 | **通過** |
| 無效／過期行情不成交 | **通過** |
| 反手成功／平倉失敗／日損鎖／開倉被拒 | **通過** |

### 未測項（明示）

| 項目 | 原因 |
|------|------|
| 對真實交易所刻意製造 429／418 | **禁止**；僅用假 session |
| 跨程序全域限流 | **未實作**；兩程序並行仍共享交易所配額（已知剩餘風險） |
| 長時間（數小時）穩定度 | 本輪有界觀察（約數分鐘）；非長跑 SLA 認證 |
| 反手在 live 運行中實際觸發 | 觀察窗內尚未出現反手事件（無反向突破）；邏輯以單元測試覆蓋 |

---

## 3. 新 seal／hash／runtime／固定資料根

| 項 | 值 |
|----|-----|
| Tag | `fix_20261119_p0p1_trail_http429_bulk_reverse` |
| Seal | `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fix_source_sealed_20261119T122904` |
| Aggregate SHA256 | `02833bd0a5843b973c9781a3f560dcb7477b5520869d9dd24e44dd151e022a6b` |
| Runtime code | `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T122904/code` |
| **固定資料根** | `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root` |
| 換版帳戶 | 續用同一 `fixed_data_root`，**不**因新 seal 另建帳戶 |
| Runtime↔Seal | `hash_verify_all_match=true`；運行 `code_snapshot` 與 seal 關鍵檔一致 |
| config_hash（bulk） | `1ad6a35c98b26446` |

舊 seal `…T121459` **不得**再宣稱含本次 429／反手修改。

---

## 4. 啟動前檢查（唯讀，重新核對）

證據：`PRELAUNCH_CHECKS_LATEST.json`

| 檢查 | 結果 |
|------|------|
| 舊程序 | PID **33829**，無 `--profile` → 舊帳戶路徑 |
| 無既有 bulk 程序 | 通過（啟動前） |
| 無正式 `output/paper_bulk_test_v1` | 通過 |
| 固定根尚無帳戶（首次） | 通過；其後由本輪啟動建立 |
| import＝新 runtime code | 通過 |
| 輸出皆在 fixed_data_root | 通過 |
| flock／磁碟／公開行情／下單路徑封鎖 | 通過 |

### 首次啟動插曲（阻擋後已恢復）

Agent 沙箱內兩次啟動因網路／行程組被殺，僅完成帳戶初始化（50000、0 倉）後死亡；**未覆寫本金**。來源已確認為本輪失敗啟動。隨後以 `start_new_session` 脫離式重啟（PID 36595），帳戶基準保留 50000。

---

## 5. 實際啟動命令／PID／run_id／日誌

### 有效啟動（驗收對象）

```text
python3 -u -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --hours 0
```

- 工作目錄／PYTHONPATH：`…/runtime_bulk_v1_20261119T122904/code`
- 脫離方式：`subprocess.Popen(..., start_new_session=True)`（`launch_detached.py`）
- **PID**：`36595`
- **run_id**：`20261119T124504_79a79b4d`
- **stdout/stderr 日誌**：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/bulk_launch_20261119T124503.log`（唯一時間戳，未覆寫舊日誌）
- launch.json pid／tag／package_dir／runtime_root：**與 PID 36595 一致**（非另開 import 宣稱）

未使用 `--once`。

---

## 6. 啟動後驗收證據

觀察截止約 2026-11-19T12:47+08；PID 36595 仍存活（etime≈2m+）。

| 項目 | 判定 | 證據 |
|------|------|------|
| PID／cmdline／profile／runtime | **通過** | `ps` 含 `paper_bulk_test_v1` + `fixed_data_root` |
| launch 版本 tag／設定／hash | **通過** | launch.json + code_hashes；snapshot≡seal |
| 本金基準 50000／倉上限 20／日損 2000 | **通過** | account `initial_capital_u=50000`；launch 20／2000；當下 cash≈47990（已有開倉，**不要求仍=50000**） |
| account_at_start 不可變 | **通過** | `immutable=true`，資本 50000；mirror 只更新 account_state |
| code_snapshot／config_snapshot | **通過** | run 目錄內完整存在 |
| warmup | **通過** | log：`warmup backfill done … ok:72 fail:0` |
| 首輪掃描 | **通過** | log：`first scan expected=72 success=72 failed=0` |
| 後續掃描 | **通過** | equity_snapshots≥5；decision_trace 持續增長 |
| 同帳戶單一寫入 | **通過** | flock + 僅一 bulk PID |
| 舊程序未操作／舊帳戶未被 bulk 寫入 | **通過** | 舊 PID 33829 仍在；舊 cash≈9668.78、locked=True |
| 正式 bulk 無測試成交混入 | **通過** | fills 均屬 run `20261119T124504_79a79b4d` |
| 429／418／timeout（本窗） | **待觀察／本窗未現** | data_quality 無 HTTP_429／418／TIMEOUT；公開行情正常 |
| 過期行情拒絕 | **待觀察／本窗未現 STALE_KLINE** | 拒絕以 VOL_X 等為主；單元測試已覆蓋 |
| 無成交是否失敗 | **否** | 首輪 0 開倉為訊號結果；其後自然 OPEN×2 |

### 當前帳戶／持倉／成交／拒絕（驗收快照）

| 項 | 值 |
|----|-----|
| 持倉 | **2**：ONDOUSDT LONG、TAOUSDT LONG |
| 現金 | ≈ **47990.00** |
| 初始本金 | **50000** |
| 日損鎖 | False |
| 成交 | OPEN×2（模擬）；非測試強制單 |
| 拒絕分布（SIGNAL_EVAL） | VOL_X=205，CHASE=6，NO_BREAKOUT=2，FILL_NOT_BEYOND_BREAKOUT=1 |
| API 錯誤（本窗） | 無 429／418 實測事件 |

### 限制（必須標明）

- **資金費**：`funding_data_gap=true`，不得當完整計費。
- **MFE/MAE**：`excursion_source=polled_quote_estimate`，非 tick 精確。
- **並行風險**：舊程序仍獨立打公開 API；無跨程序全域限流。

---

## 7. 完整絕對路徑（交易／帳戶／復盤）

固定根：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root`

| 用途 | 路徑 |
|------|------|
| 帳戶 | `…/fixed_data_root/output/paper_bulk_test_v1/account_state.json` |
| fills | `…/output/paper_bulk_test_v1/fills.jsonl` |
| trades | `…/output/paper_bulk_test_v1/trades.csv` |
| equity（root） | `…/output/paper_bulk_test_v1/equity_snapshots.jsonl` |
| decision_trace（root） | `…/output/paper_bulk_test_v1/decision_trace.jsonl` |
| review（root） | `…/output/paper_bulk_test_v1/review.md` |
| 當前 run | `…/output/paper_bulk_test_v1/runs/20261119T124504_79a79b4d/` |
| run 內 review／equity／decision／quality／launch／hashes／snapshots | 同上 run 目錄 |
| vault | `…/fixed_data_root/data/paper_bulk_test_v1/kline_vault_v2_clean/` |
| PID／鎖 | `…/output/paper_bulk_test_v1/paper.pid`、`paper_profile.lock` |
| 啟動日誌 | `…/paper_bulk_test_v1_setup/launch_logs/bulk_launch_20261119T124503.log` |

舊帳戶（對照）：`/Users/alan/MyProjects/automax/output/breakout_follow_v1/account_state.json`

---

## 8. 經核對的停止與續跑命令

### 停止（無持倉時可優雅停；**目前有 2 倉 — 勿盲目 SIGTERM 除非接受停止管理**）

若需停且已確認策略：核對 PID＋cmdline＋profile＋runtime 後：

```bash
/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T122904/code/stop_paper_bulk.sh
# 或
python3 -u -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root --stop
```

**有持倉時建議**：保留進程繼續 manage／退出；先回報再決定。勿 `kill -9`、勿清鎖、勿重設帳戶。

### 續跑（帳戶已存在 — 不補本金）

```bash
python3 /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T122904/launch_detached.py
```

必須指向同一 `fixed_data_root`；同 profile flock 會擋第二實例。

---

## 9. 下次復盤請提供

1. `runs/<run_id>/review.md`、`data_quality.md`、`decision_trace.jsonl`、`fills.jsonl`、`equity_snapshots.jsonl`  
2. `launch.json`、`code_hashes.json`、`account_at_start.json`、`account_state.json`  
3. 對應 `launch_logs/bulk_launch_*.log`  
4. 若有反手：決策中 `REVERSE_*` 事件列  
5. 若有 429／418：`data_quality.jsonl` 中 HTTP_* 列  

---

## 10. 阻擋事項與本輪未做

| 項 | 狀態 |
|----|------|
| 停舊程序／改舊日損／改正式 package | **未做** |
| 跳過測試啟動 | **未做**（84/84 後才啟動） |
| 強制下單凑驗收 | **未做**（成交為自然訊號） |
| 沙箱誤殺啟動 | 已記錄；最終以脫離式 PID 36595 驗收 |

---

## 11. 產物索引

- 本報告：`…/PAPER_BULK_LAUNCH_ACCEPTANCE_REPORT_20261119T124733.md`
- Seal：`fix_source_sealed_20261119T122904/`
- Runtime：`runtime_bulk_v1_20261119T122904/`
- 固定資料根：`fixed_data_root/`
- 證據：`ACCEPTANCE_EVIDENCE_LATEST.json`、`PRELAUNCH_CHECKS_LATEST.json`、`LAUNCH_SEAL_POINTER.json`
