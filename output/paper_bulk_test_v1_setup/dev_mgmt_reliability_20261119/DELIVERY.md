# 交付摘要（管理可靠性離線實作＋日報修正＋成本診斷＋切換準備）

狀態：**離線完成；未部署；未切換；未停機；未改 sealed／帳戶／金庫**

## 1. 改動清單與離線測試

### 開發副本（唯一改交易碼處）
`output/paper_bulk_test_v1_setup/dev_mgmt_reliability_20261119/`
- 來源：`runtime_bulk_v1_20261119T122904`（PROVENANCE.json 哈希吻合）
- **未**寫入 sealed runtime

| 檔案 | 內容 |
|---|---|
| `config.py` | MANAGE_DUE／BOOK 短預算／SCAN_SLICE（基準值） |
| `market_data.py` | get_json 可覆寫預算；`fetch_book_tickers_manage` |
| `portfolio.py` | `phase=book\|atr\|full`；Book 仍執行既有 stop |
| `manage_pass.py` | due 鐘、批次／每倉完成事件、不補跑 |
| `engine.py` | 插隊 manage、掃描切片 |
| `run_store.py` | `manage_events.jsonl` |

預算基準：DUE=5；BOOK timeout=5／attempts=2／op_budget=10；SLICE=2。  
不合入 N/T 禁止開倉；未改止損／TP／反手／倉位／日損。

### 測試
`tests/run_mgmt_offline_tests.py` → **22 passed, 0 failed**（見 `OFFLINE_TEST_RESULTS.md`）  
涵蓋：E1 不補跑、E2 429/418、E3 取消無背景續跑、Book 不停用既有 stop、ATR 失敗保留 stop、無倉不冒充成功、單倉隔離、部分成功分類。  
**fake clock ≠ 真實網路保證。**

### 日報工具修正
`tools/paper_daily_review/`：管理前間隔改 MANAGE-only、觀測權益標題、日損鎖 NA 來源、候選狀態用語、集群 JSON 不截斷、K 線頭尾覆蓋。

---

## 2. 396s 缺口分類

見 `analysis_packs/daily_reports/2026-11-19/GAP_396S_EVIDENCE.json`

| 項 | 值 |
|---|---|
| max≈396.38s | **12:38:44 → 12:45:20** |
| purpose | **SIGNAL_EVAL → SIGNAL_EVAL**（非 MANAGE） |
| runs | `123832_91fcc722` → `124504_79a79b4d`（**跨 run**） |
| 持倉 | 缺口起點 **無持倉** |
| 稱呼 | **不得稱「失管 396s」**（無完成事件；且非管理前批次） |
| 有持倉期間最大管理前間隔 | ≈**59.0s**（14:01:36→14:02:35，同 run，n_pos=2）— 仍只是 quote 批次，非完成態 |

---

## 3. 成本候選比較表

見 `analysis_packs/daily_reports/COST_CANDIDATE_DIAG_20261119.md`  
原始 asof **15:34:22** 凍結 **n=47／sum≈−1631.96**；之後另有 3 筆完整平倉另列。  
狀態：**值得診斷／部署證據不足**。0.25/0.40 僅探索。

---

## 4. 承接持倉與回滾

見 `SWITCH_PREP_HANDOFF.md` / `SWITCH_PREP_VERIFY.json`  
- 活帳戶唯讀 sha 未變；schema 相容；重載冪等  
- 停止不自動平倉；不可原地 manage-only  
- 回滾禁止還原舊帳戶快照  

---

## 5. 可否進入 paper 切換？

**否（現在不可）。** 阻擋：
1. DEV 未封新 sealed runtime  
2. 未批准部署／切換窗口  
3. 舊程序仍在跑且有持倉／續開倉  
4. 無已批准安全停新倉開關  
5. 切換空窗未實測記錄  

---

## 6. 待你批准的部署清單

1. 將 DEV 打成新 sealed runtime（哈希／tag）  
2. 批准停止舊 PID 的操作窗口與空窗度量  
3. 單實例啟動新 runtime（同一 account_state；禁止 --once 當 init）  
4. 切換後驗收：manage_events、無重複 fill、持倉欄位保留  
5. 回滾計畫確認（不還原帳戶快照）  
6. （可選另批）成本候選進樣外；集中／突破仍候選不部署  
7. （不合入本包）N/T 健康禁止開倉政策  

**到此停止；未實際切換。**
