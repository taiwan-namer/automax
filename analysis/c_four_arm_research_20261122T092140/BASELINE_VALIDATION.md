# BASELINE_VALIDATION

## 凍結

- 時間：2026-11-22T09:21:40+08:00（見 `BASELINE_FREEZE.json`）
- git HEAD：`81813a261a94dbdbcecfaef9d62df5617098bfe6`（短 `81813a2`）
- profile：`testnet_c_inv_v1`，mode=`paper`
- run_id：`20261121T201533_9a0f7b4f`
- config_hash：`2cad8ee26c1d00bd`
- EXIT_PLAN_ID：`c_pyramid300_r2_p0tp1pct_20261120`
- 原報告：`analysis/c_10h_review_20261122T085501/{BRIEF,RECONCILIATION,SUPPLEMENT}.md`

## Runner 狀態（未任意停止／重啟）

- PID 檔記載 `5236`：實際為 `conhost`，**不是**存活中的 C python runner。
- 掃描時未發現 `testnet_c` / `paper_main` python 行程。
- **未**對原 C 行程下達 stop/restart。

## 舊帳本保全

- 原帳戶路徑未覆寫：`output/paper_bcd_local/data_c/output/testnet_c_inv_v1/account_state.json`
- 未平 4：1000BONK / AVAX / 1000SHIB / 1000PEPE — **保持未平**，未挪入研究帳戶、未標結束。
- 待成交 5：BCH / ASTER / ADA / APT / AKE — **原樣保留**。

## P0 錨點定義（沿用原版）

- P0 = OPEN 限價／觸價成交價；寫入 `pyramid.p0_entry` 與 `rebound_exit_price`。
- `stop_frozen=True`：Pstop 不隨均價或時間刷新。
- P1/P2 加倉只改持倉均價與層列表，**不得**覆寫 `p0_entry`（研究修補在 ADD 路徑再次寫回凍結錨點）。
- 本樣本無 P0 分批成交；若未來出現，依 G6 列阻擋，不自創錨點規則。
- 價格驗證依 tick／取整；不接受 0.2% 寬鬆容差。

## 帳務重現（配對 B0）

| 項目 | 值 |
|------|-----|
| 配對 B0 已平淨額合計 | **-26.462858 U** |
| 原審查已平淨額 | **-26.462859 U** |
| \|差\| | **≈1e-6 U** |
| `reproduce_closed_ok` | **true** |

說明：B0 使用同一 fills 時間軸重建層級 PnL；這是**持倉管理配對**，不是獨立訊號回測。  
帳戶層 run_delta 已實現 -20.57U（含未平 cycle 已實現部分）**不**與「14 已平 -26.46」混稱。

## 研究修補（不接實盤、不改 A/B 策略邏輯意圖）

已做（紙上／共用模組，C paper 路徑）：

- paper fill：`fill_source=paper_simulation`、`execution_model_version`、禁止 `is_confirmed_exchange_price=True`
- `PYRAMID_ADD` → `trades.csv`；`cash_delta_u`／`margin_lock_u`／`margin_release_u`
- 同報價：`ADD_BLOCKED_STOP_PRIORITY`（停損優先於加倉）
- `atomic_write_json` / `append_jsonl` 有上限重試
- `ArchiveWriter.is_alive` + heartbeat

**未**覆寫原 C 帳本；**未**把研究參數寫入正式 `testnet_c_inv_v1` 啟動設定；**未**送 testnet／實盤單。
