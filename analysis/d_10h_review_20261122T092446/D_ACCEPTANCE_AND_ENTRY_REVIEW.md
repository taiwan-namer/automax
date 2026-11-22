# D_ACCEPTANCE_AND_ENTRY_REVIEW

**時間**：2026-11-22  
**約束**：未啟動新 paper 成交 runner；未掃 TP／SL 參數；未改策略交易規則；舊 HYPE／舊帳本／舊 run 只讀保全。  
**聲明**：本文件為工程驗收＋進場診斷；**不宣稱策略已驗證**。

---

## 1. 工程驗收總表

| 項目 | 結果 |
|------|------|
| 主迴圈遇 PersistError 不直接崩潰（新碼 catch＋測試） | **通過** |
| 保存不可靠時禁止新增掛單／開倉 | **通過** |
| 不得產生無可靠記錄的模擬成交（halt 下 queue／exit 抑制） | **通過** |
| 持倉無法可靠模擬出場 → 告警／管理中斷事件（非正常風控） | **通過**（程式＋契約測試） |
| 恢復前對帳；不能只刪 halt 檔 | **通過** |
| 第二 writer 鎖防護 | **通過** |
| 設定快照／native quote-VOL 格式（隔離 fixture） | **通過**（**未**冒稱真實行情運行） |
| 舊碼歷史 run 遇 PE 會崩潰 | **已知歷史事實**（已由新碼修復；非本輪回歸失敗） |
| 真實行情長跑驗收 | **未測**（本輪禁止啟動 runner） |

**總評**：隔離整合測試 **通過**；具備「啟動新隔離觀測 run」的工程前提，但仍須對帳＋新 runtime_root；**本輪不啟動**。

### Halted 時各行為（明確口徑）

| 能力 | Halted 時 |
|------|-----------|
| 報價拉取 | **繼續**（可視性） |
| 持倉估值（記憶體 MTM／roll） | **繼續**（記憶體；不宣稱已持久化） |
| 出場觸發偵測 | **可計算** |
| 模擬平倉 | **停止**；寫 `MANAGEMENT_INTERRUPTED_NO_RELIABLE_EXIT` 並告警 |
| 成交記帳（OPEN／CLOSE fill） | **停止新增**；既有路徑若 persist 失敗 → halt＋`MANAGEMENT_INTERRUPTED_PERSIST` |
| 新增掛單／開倉 | **禁止** |
| 帳戶落盤 | **跳過／失敗即 halt**；side-file `PERSIST_HALT.json` |
| 狀態宣稱 | 前綴 `[盯倉-持久化中斷]`；**不得稱正常盯倉／正常風控** |

---

## 2. 報告修正及數值差異

### 2.1 四臂績效分開呈現（修正 D_NEXT_REVIEW 混計）

口徑：固定進場＋SL3；**無 VOL**；局部 OHLC CF；**新測試設定**。  
HYPE **不計入已平**；未平用 **最後已知 mid**（見 2.2）。

| 臂 | 模型已平淨損益（10 筆已平 Σ cycle_net） | 未平估值（HYPE mid，含費若平） | 總估值（另列，**非**已平淨損益） |
|----|------------------------------------------|--------------------------------|----------------------------------|
| TP 0.5% | **−28.5015U** | **−2.4149U** | −30.9165U（標示：總估值） |
| TP 1% | **−33.0034U** | **−2.4149U** | −35.4184U（標示：總估值） |
| TP 1.5% | **−33.0034U** | **−2.4149U** | −35.4184U（標示：總估值） |
| TP15U | **−33.0034U** | **−2.4149U** | −35.4184U（標示：總估值） |

相對舊 D_NEXT_REVIEW：舊表把 HYPE 截止 K 界限混進 Σ，且曾用 OHLC close 估 −1.7720；**本表已拆分且改 mid**。

### 2.2 HYPE 估值更正（禁止 21:03 未完成 K）

| 項目 | 值 |
|------|-----|
| 截止／帳本時間 | `account.updated_at=2026-11-21T21:03:11.682901+08:00` |
| 行情時間 | roll_samples 最後 mid 時間 **`2026-11-21T21:03:11.681901+08:00`** |
| 價格種類 | **book mid**（帳本 `roll_samples[].px`） |
| 價格 | **59.8655** |
| 數量／進場／進場費 | 5.04／59.446／0.14980392U |
| 未實現 gross | `(59.446−59.8655)×5.04 =` **−2.11428U** |
| 若平倉費用口徑 | 另加 `exit_fee=\|mid×qty\|×0.0005` 與既有 entry_fee → cycle_net≈**−2.4149U** |
| 禁止 | 使用 **21:03** 這根 1m K 的 high／low／close（該 K 至 21:04 才完整；截止 21:03:11 時不可用） |
| 舊 −1.7720 | 來自 OHLC close=59.738 → **作廢作為截止有效估值** |
| 已實現 | 帳本 `realized_net_u` **不含** HYPE 未平；未實現不得計入已實現 |

### 2.3 1000SHIB 30m bars 更正

- 正確：**30m bars=31**；60m bars=61。  
- 底層 `supplement_data.json` 原本正確。  
- `SUPPLEMENT.md` 正文已是 30m=31；已加 **ERRATA**：若衍生報告誤寫 30m=61 → 更正為 31（**報告文字錯誤，資料正確**）。

### 2.4 PermissionError 21:02:28 vs account.updated_at 21:03:11

| 節點 | 時間 | 含義 |
|------|------|------|
| 最後心跳列印 | `21:02:28.723883` | 迴圈心跳；**不是** PE 拋出時刻 |
| 最後成功帳本樣本 | mid=59.8655 @ `21:03:11.681901` | `update_position_marks`→`save_account` **成功落盤** |
| account.updated_at | `21:03:11.682901` | 與上同一成功寫入戳記 |
| PermissionError 列 | 日誌末行，**無獨立時鐘戳** | 堆疊顯示 `scan_once→manage_all→save_account→atomic_write`；舊碼 **未捕捉** → 行程終止 |
| 重試預算 | `atomic_write` 最多約 12 次、sleep 合計 ~3.9s | 僅約束**該次失敗呼叫**內「開始重試→最終拋出」 |
| 進程終止 | 日誌在 PE 後結束、無优雅停止句 | 終止於未捕捉例外；**不能**把 `21:02:28` 當終止時間 |
| 間隔說明 | 心跳→成功寫入 ≈43s | 落在 60s 心跳間隔內的 manage／scan；其間無新心跳列印屬預期 |

**結論**：`21:02:28`＝錯誤前最後心跳；`21:03:11`＝最後成功持久化（含 mid）；其後同輪或緊接的 `save_account` 失敗並殺死舊進程。三者不可併成同一時間點。

---

## 3. 21 訊號漏斗與進場診斷

**母體**：`signals.jsonl` 中 `event=SIGNAL` **21** 筆（禁止成交反推訊號）。

### 漏斗

| 階段 | 筆數 |
|------|------|
| 訊號 | 21 |
| 有掛單（QUEUE） | 19 |
| 成交（OPEN） | 11 |
| 撤單（無成交） | 8 |
| 訊號但無掛單 | 2（TAO、UNI） |

串聯狀態：`SIGNAL_QUEUE_FILL=11`／`SIGNAL_QUEUE_CANCEL=8`／`SIGNAL_NO_QUEUE=2`。

### 聚集（不可當 10 次獨立驗證）

- **18:30 桶**：11 個標的同時段訊號（含後續多筆成交）→ 共同行情事件。  
- **18:45**：DOT、SOL  
- **19:30**：TAO、UNI、NEAR、SUI、HYPE  

### 逐筆摘要（詳見 `entry_diagnosis_data.json`）

| 商品 | 訊號時間 | 生命週期 | 撤單／結果 | 訊號→成交(s) |
|------|----------|----------|------------|--------------|
| TRX | 14:35:12 | 掛→撤 | QUIET_FAIL:BAR_AMP_FAIL | — |
| 1000PEPE | 18:37:06 | 掛→撤 | DEPTH_FAIL | — |
| AAVE | 18:37:07 | 掛→撤 | DEPTH_FAIL | — |
| ADA | 18:37:07 | 掛→成 | — | 724 |
| AVAX | 18:37:15 | 掛→成 | — | 2012 |
| BCH | 18:37:16 | 掛→撤 | DEPTH_FAIL | — |
| BNB | 18:37:18 | 掛→成 | — | 3185 |
| DOGE | 18:37:29 | 掛→成 | — | 2009 |
| LINK | 18:39:07 | 掛→成 | — | 678 |
| LTC | 18:39:09 | 掛→撤 | DEPTH_FAIL | — |
| XLM | 18:40:24 | 掛→成 | — | 3073 |
| XRP | 18:40:26 | 掛→成 | — | 3077 |
| DOT | 18:51:25 | 掛→撤 | DEPTH_FAIL | — |
| SOL | 18:53:23 | 掛→成 | — | 2306 |
| WLD | 19:03:10 | 掛→成 | — | 468 |
| 1000SHIB | 19:17:12 | 掛→成 | — | 871 |
| TAO | 19:30:10 | **無掛單** | UNKNOWN 下游 | — |
| UNI | 19:30:14 | **無掛單** | UNKNOWN 下游 | — |
| NEAR | 19:33:17 | 掛→撤 | PRICE_DROP_FROM_P | — |
| SUI | 19:33:26 | 掛→撤 | PRICE_DROP_FROM_P | — |
| HYPE | 19:36:12 | 掛→成 | — | 2362 |

決策時 1m 已收盤報酬／振幅、嵌入 15m 安靜欄位、BTC／ETH 1m 基準見 `entry_diagnosis_data.json`（僅用決策時已知；未用成交後上漲反推過濾）。  
未成交／撤單＝描述性對照，**不冒稱可成交盈利**。

### 前瞻假設（最多兩個；不部署、不搜門檻）

1. **H1_CLUSTER_COOLDOWN**：同 15m 桶多標的齊發時共享波動；可前瞻測「同桶限 1 新掛／冷卻」是否降相關虧損。  
2. **H2_POST_SIGNAL_1M_ADVERSE**：訊號後、成交前用**已收盤** 1m 不利振幅作再確認；僅決策時資料。

---

## 4. 是否具備啟動新隔離觀測 run 的條件

| 條件 | 狀態 |
|------|------|
| Persist halt／不崩潰／禁新開倉 | 已具備（程式＋測試） |
| 設定快照＋quote／VOL 紀錄接線 | 已具備（fixture 驗證格式） |
| 舊帳本隔離（新 runtime_root） | **必須**；否則不准啟動 |
| 舊 HYPE 對帳／是否手動處理殘倉 | **啟動前必做**；本輪未做實盤／testnet 動作 |
| 策略參數已驗證 | **否** |
| 本輪啟動 | **否** |

**結論**：工程上可準備「新隔離觀測 run」；**策略未驗證**；**現在不啟動**。

---

## 5. 修改檔案、測試命令、commit

### 修改／新增（D 範圍）
- `output/testnet_abcd_setup/code/d_quiet_short_v1/util.py`
- `output/testnet_abcd_setup/code/d_quiet_short_v1/portfolio.py`（halt、reconcile 閘門、marks persist 旗標）
- `output/testnet_abcd_setup/code/d_quiet_short_v1/engine.py`（halt 下管理中斷、PersistError 不崩潰）
- `output/testnet_abcd_setup/code/d_quiet_short_v1/config.py`／`paper_main.py`（快照路徑；上輪已接）
- `output/testnet_abcd_setup/code/d_quiet_short_v1/test_persist_halt.py`
- `output/testnet_abcd_setup/code/d_quiet_short_v1/test_persist_integration.py`（新建）
- `analysis/d_10h_review_20261122T092446/SUPPLEMENT.md`（SHIB ERRATA）
- `analysis/d_10h_review_20261122T092446/_entry_diagnosis.py`／`entry_diagnosis_data.json`
- `analysis/d_10h_review_20261122T092446/D_ACCEPTANCE_AND_ENTRY_REVIEW.md`（本文件）

### 測試命令
```text
cd output/testnet_abcd_setup/code
python -m unittest d_quiet_short_v1.test_persist_halt d_quiet_short_v1.test_persist_integration -v
# Ran 15 tests — OK

python analysis/d_10h_review_20261122T092446/_entry_diagnosis.py
```

### Commit
**已建立 D 專用 commit**（未納入 A／B／C 或其他工作樹雜項）：  
- 主交付：`97f8823` — *Add D persist-halt acceptance, entry diagnosis, and review pack.*  
- 範圍：`output/testnet_abcd_setup/code/d_quiet_short_v1/*` + `analysis/d_10h_review_20261122T092446/*`。  
（若本段隨後有 follow-up 小修 commit，以 `git log --oneline -3` 為準。）
