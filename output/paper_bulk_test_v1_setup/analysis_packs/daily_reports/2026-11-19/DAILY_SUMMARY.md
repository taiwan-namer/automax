# 每日復盤 2026-11-19（Asia/Taipei）

- 工具版本：`0.1.0`／schema `daily_report_v1`
- 截止 asof：`2026-11-19T20:18:12.063889+08:00`
- 視窗：`2026-11-18T20:15:00+08:00` ≤ t < `2026-11-19T20:15:00+08:00`
- 完整性：**完整日** — asof 已過日界且來源讀取無截斷尾

## 1. 結論（3–5 句）
- 本日標示為「完整日」：視窗內完整平倉 n=106，完整淨利合計≈-3604.6890080763887，勝率=0.09433962264150944（僅完整 trade_id）。
- 帳戶權益日變化≈-3628.5902620522393（結算界快照 ok=False）；與 C 完整平倉合計分開解讀；跨日 OPEN 費不當作今日才發生。
- 管理完成間隔=ok（）；目前僅能報告管理前報價批次間隔，不得稱為完成態。
- 策略調整候選中值得下一階段驗證者：CAND_COST_THRESHOLD, CAND_BATCH_SAME_DIR, CAND_BREAKOUT_QUALITY；皆為提案，非部署。
- 執行可靠性修復與進場條件變更分開追蹤；本輪未修改交易策略／配置／runtime／帳戶。

本節僅描述截至 asof 的可觀測結果。部分日不得外推為全日期望；完整日需待日界結束後以相同工具重跑。任何策略或管理修復皆未因本報表自動生效。

## 2. 當時實際策略（提案／實作／部署分離）
實際運行身分以 launch／code_hashes／fills 內欄位為準；`current_run.json` 與 `paper.pid` 只是檔案指標，不能單獨證明進程仍存活或指令列未變。沙箱內 `ps` 結果亦不可作為唯一依據。
- profile：`paper_bulk_test_v1`
- current_run_id（檔案指標，非進程證明）：`20261119T161354_5cb8369a`
- code_fix_tag：`fix_20261119_mgmt_dl5000_batchside1`
- config_hash：`e7d7f96bbb59c6f6`
- runtime cwd（launch 紀錄）：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T160723/code`
- 日損鎖設定：`5000.0` USDT
- 同日涉及 run：20261119T124504_79a79b4d, 20261119T131629_436e6cae, 20261119T132017_bd1972b2, 20261119T155834_00889cc1, 20261119T161354_5cb8369a
- 狀態標示：本報表＝**分析工具已實作產出**；管理可靠性修復＝**設計／提案（未實作未部署）**；策略候選＝**待驗證提案（未改配置）**；排程＝**文件 only（未載入）**。
- 跨版本持倉：開倉版本與退出管理版本分開紀錄；不得把退出當下 runtime 當成開倉時策略身分。
  - `20261119T124504_79a79b4d:JUPUSDT:c7c28ec23f` 開倉 run=`20261119T124504_79a79b4d`／退出 run=`20261119T132017_bd1972b2`
  - `20261119T124504_79a79b4d:JCTUSDT:a539837ca6` 開倉 run=`20261119T124504_79a79b4d`／退出 run=`20261119T132017_bd1972b2`
  - `20261119T131629_436e6cae:QNTUSDT:58ea98544c` 開倉 run=`20261119T131629_436e6cae`／退出 run=`20261119T132017_bd1972b2`
  - `20261119T131629_436e6cae:PENGUUSDT:d62206cf8c` 開倉 run=`20261119T131629_436e6cae`／退出 run=`20261119T132017_bd1972b2`
  - `20261119T132017_bd1972b2:ENAUSDT:cd1f2725f2` 開倉 run=`20261119T132017_bd1972b2`／退出 run=`20261119T161354_5cb8369a`
  - `20261119T132017_bd1972b2:LITUSDT:67e371c0b1` 開倉 run=`20261119T132017_bd1972b2`／退出 run=`20261119T161354_5cb8369a`
  - `20261119T155834_00889cc1:BREVUSDT:b782bf069a` 開倉 run=`20261119T155834_00889cc1`／退出 run=`20261119T161354_5cb8369a`

## 3. A／B／C／D 績效
四欄必須分開閱讀：A 是帳戶水位與當日變化；B 是視窗內事件／分段；C 是跨 run 完整 trade_id 且 CLOSE 落在當日者；D 是未平倉及其已扣 OPEN 費。不可用分段勝率冒充完整交易勝率，也不可把 cash_delta 當成損益。
### A 帳戶
- 結算界快照可用（欄位名 midnight_snapshot_ok，語意=日界±120s）：`False`
- 起始快照：`2026-11-19T12:38:41+08:00`（相對日界開始 lag=59021.0s） equity=`50000.0` cash=`50000.0`
- 結束／最近快照：`2026-11-19T20:15:13+08:00`（相對日界結束 lag=13.0s；部分日為負值屬預期） equity=`46371.40973794776` cash=`44385.311012376835`
- **觀測權益變化（2026-11-19T12:38:41 至 2026-11-19T20:15:13）**：`-3628.5903` — **不是**完整日損益；缺結算界快照。
- 日現金變化：`-5614.6890` — **cash_delta 不是 PnL**（含保證金佔用等）
- 費用與 funding：帳戶 realized 已含紙上費用；`funding_data_gap=True`，funding 不得當已核對。
- 讀取當下 account_state（非午夜冒充）：cash_u=`44385.3110` realized=`NA` — live account_state — NOT a settlement-boundary (20:15) substitute
- 日損鎖狀態：`False`（來源：account_state.day_new_entries_locked 或 equity.day_locked）

### B 各 run 事件／分段（非完整交易勝率）
- 視窗內事件數：`224`
- run `20261119T124504_79a79b4d`：OPEN=11 CLOSE=9 PARTIAL=0 seg_net≈-504.6233
- run `20261119T131629_436e6cae`：OPEN=2 CLOSE=0 PARTIAL=0 seg_net≈-9.9990
- run `20261119T132017_bd1972b2`：OPEN=40 CLOSE=42 PARTIAL=5 seg_net≈-1345.5549
- run `20261119T155834_00889cc1`：OPEN=2 CLOSE=1 PARTIAL=0 seg_net≈-39.3422
- run `20261119T161354_5cb8369a`：OPEN=53 CLOSE=54 PARTIAL=5 seg_net≈-1715.1696
- 註：B = event/segment PnL in window; NOT complete-trade winrate。分段淨利可含當日 OPEN 費與部分止盈，但同一 trade 可能跨 run 才完整，故 B≠C。

### C 完整 trade_id（當日 CLOSE）
- n=106 勝=10 負=96 平=0 勝率=0.09433962264150944 均盈虧=-34.006500076192346 合計=-3604.6890080763887 PF=0.2083613294387977 期望=-34.006500076192346 費用合計=1060.1619544556645
- 註：C = complete trade_id with CLOSE in day window; OPEN fee may belong to prior calendar day
- 完整淨利規則：若 OPEN 存在 `realized_net_u` 鍵且值非 null（**含合法 0**）則用其值；否則使用 −OPEN.fee_u；再加所有 PARTIAL／CLOSE 的 net_u。不可用 truthiness 把 0 誤判缺值；不重複扣費。
- 當日平倉完整績效與帳戶當日損益分開；跨日 OPEN 費屬於開倉當日帳戶，不可當作今天才發生的費用事件，但仍計入該完整 trade 的 C 淨利。

### D 未平倉
- 未平倉數：`2`；當日窗口內 OPEN 費分量合計：`-9.99997954672992`
- 註：OPEN fees for open positions stay in account A; not mixed into C winrate
  - `20261119T161354_5cb8369a:FETUSDT:1bd4e0c0cc` FETUSDT LONG opened=2026-11-19T19:33:15+08:00 fee_comp=-4.9999805961599995
  - `20261119T161354_5cb8369a:1000PEPEUSDT:f30831260d` 1000PEPEUSDT SHORT opened=2026-11-19T20:07:13+08:00 fee_comp=-4.99999895056992

## 4. 管理健康
有效報價／管理前紀錄批次間隔，與真正管理完成間隔必須分開。前者來自 `quote_events`（寫在 manage_position 前）；後者需完成事件 schema，舊資料為 NA。觀測間隔不是設定參數，也不是市場事件時間。
- 有效報價／管理前批次間隔（秒）：n_batches=4865 mean=5.506232756784552 p50=5.027343999999999 p95=10.213761499999999 max=373.7358
  - MANAGE/MANAGE_POLL pre-manage quote batches only by default. NOT manage-complete; NOT configured poll; NOT 「失管」. All-purpose max may be cross-run SIGNAL_EVAL (see GAP evidence).
- 真正管理完成間隔：status=`ok` value/mean=`5.056964109318407` reason=``
- DQ 分解：timeouts≈13 429≈0 418≈0 manage_blocked≈0；counts 摘要見 metrics。
- 超額止損：以成交 `exit_reason`／既有分析包拆解 ask 缺口與滑價假設；本節不把管理間隔尖峰直接等同市場跳空時間。

## 5. 策略品質與集中風險
以下為**條件子集診斷**與候選提案，不是完整策略回測，也不是批准開新倉。同批同向交易不可視為完全獨立樣本；費用來源為紙上模擬費率，不能冒充真實帳戶費率。進場後 1／3／5 分鐘報酬只作結果標籤，不能直接當進場可用特徵。執行可靠性修復與進場策略變更分開，避免把不同修改效果混在一起。
### 候選 `CAND_COST_THRESHOLD` — 成本門檻／進場與目標相容性
- 現有證據：`{"n_with_risk": 106, "n_total": 106, "median_fee_to_risk": 0.39236989686904755, "mean_fee_to_risk": 0.4575449200840619, "pct_fee_to_risk_gt_0_25": 0.6981132075471698, "pct_fee_to_risk_gt_0_40": 0.4716981132075472}`
- 候選改動與理由：若 fee/initial_risk 偏高，候選為：提高最小風險距離或名義門檻、或要求目標R與費用相容；不以放寬止損稀释比例。
- 資料缺口：['真實帳戶費率未核對', '滑價僅紙上模型', '無完整未進場拒絕樣本']
- 回測／回放可行性：條件子集診斷：在既有成交上篩 fee/risk 超門檻者的淨利分布；非完整回測（跳過單會改變資金與後續機會）
- 候選狀態：**值得診斷／部署證據不足**（未批准、未部署；≠可部署）
- 與管理修復分離：執行可靠性修復獨立；本項為進場策略條件候選
### 候選 `CAND_BATCH_SAME_DIR` — 同批同向集中曝險限制
- 現有證據：`{"n_clusters": 4, "clusters": [{"t0": "2026-11-19T12:47:07+08:00", "side": "LONG", "n": 2, "symbols": ["ONDOUSDT", "TAOUSDT"], "notional_u": 19999.817853738, "initial_risk_sum_u": 37.29828157598962, "window_sec": 30.0}, {"t0": "2026-11-19T12:57:08+08:00", "side": "SHORT", "n": 2, "symbols": ["AKEUSDT", "ICNTUSDT"], "notional_u": 19999.962197401997, "initial_risk_sum_u": 126.77280812017733, "window_sec": 30.0}, {"t0": "2026-11-19T14:03:11+08:00", "side": "LONG", "n": 6, "symbols": ["FETUSDT", "TAOUSDT", "KAVAUSDT", "JCTUSDT", "1000PEPEUSDT", "XPLUSDT"], "notional_u": 59999.89438706153, "initial_risk_sum_u": 150.93825195001116, "window_sec": 30.0}, {"t0": "2026-11-19T15:12:10+08:00", "side": "SHORT", "n": 2, "symbols": ["LITUSDT", "ENAUSDT"], "notional_u": 19999.858638118, "initial_risk_sum_u": 67.78433980817128, "window_sec": 30.0}], "window_sec": 30.0}`
- 候選改動與理由：候選：每批風險預算、評分排序取捨、同向名義／初始風險上限。保留決策只能用當時資訊，不可事後挑贏家。
- 資料缺口：['當時排序分數若未落盤則無法公正重放取捨', '跳過單改變後續可開倉集合']
- 回測／回放可行性：條件子集：對同批同向>N的集群，比較『全留』vs『僅留當時最高分K檔』的既有成交淨利（標記為診斷）
- 候選狀態：**值得診斷／部署證據不足**（未批准、未部署；≠可部署）
- 與管理修復分離：與管理逾時／報價延遲分開；集中曝險是進場政策
### 候選 `CAND_BREAKOUT_QUALITY` — 突破品質濾網（完整樣本）
- 現有證據：`{"n_complete": 106, "exit_reason_counts": {"INITIAL_STOP": 96, "TRAILING_STOP": 10}, "sum_net_u": -3604.6890080763887, "post_entry_labels_attached": false, "note": "進場後1/3/5m報酬僅作結果標籤，不可當進場特徵；10m/15m濾鏡僅用當時已完整收盤K"}`
- 候選改動與理由：候選：訊號棒實體／事前趨勢強度／波動分位門檻；僅用決策時已閉合K。對全部符合條件成交做子集診斷，不挑五筆故事。
- 資料缺口：['完整回測需宇宙掃描拒絕日誌與當時評分快照', '無拒絕樣本時只能做成交條件子集診斷', '資料不足則不虛構完整策略回測數字']
- 回測／回放可行性：若有1m vault：以開倉時間向前取已閉合10m/15m特徵，標記可否通過候選濾網；比較通過/未通過子集淨利。禁止用1m OHLC捏造精確book成交路徑。
- 候選狀態：**值得診斷／部署證據不足**（未批准、未部署；≠可部署）
- 與管理修復分離：與 stop 超額／book 逾時分開；品質濾網不替代管理修復

## 6. K 線與資料品質
檢查當日涉及標的之 1m 是否存在於本機金庫、缺分鐘、重複、OHLC 異常。10m／15m derived 僅可用決策當下已完整收盤桶；不可使用進場後才可取得的資訊當作當時濾網。缺資料只標記；不聯網補抓、不回寫金庫。
- 缺失標的：無
- 網路補抓：False；金庫回寫：False
- 1000LUNCUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- 1000PEPEUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- AAVEUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- ADAUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- AKEUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- APTUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- ARBUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- ARIAUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- ASTERUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- AVAXUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- BCHUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- BLESSUSDT: status=ok bars=606 內部缺分≈0 頭缺≈834 尾缺≈None dup=0 ohlc_bad=0
- …其餘 37 標的見 metrics.json／kline 細節

## 7. 最近 7 日與累計
- 可彙總日數：1；缺日：['2026-11-18', '2026-11-17', '2026-11-16', '2026-11-15', '2026-11-14', '2026-11-13']；累計完整平倉 n／sum_net（僅有報表之日）：50／-1757.7009971582459
收益比較必須同時列出曝險（名義／初始風險）與市場時段差異；部分日與完整日不可直接並列當同等樣本。版本切換日應分 run／code_fix_tag 列示。

## 8. 下一步（最多 3）
1. **已證實問題**：管理完成事件缺失 → 完成間隔 NA；bookTicker 逾時導致管理前間隔拉長（見既有分析包）。修復實作仍待批准。
2. **待驗證策略假設**：CAND_COST_THRESHOLD:成本門檻／進場與目標相容性；CAND_BATCH_SAME_DIR:同批同向集中曝險限制；CAND_BREAKOUT_QUALITY:突破品質濾網（完整樣本）
3. **待批准修改**：管理可靠性實作／部署；策略候選進樣外驗證；launchd 載入。皆未在本輪執行。

### 假設追蹤（承接上一日，非每日重猜）
- [open] CAND_COST_THRESHOLD: CAND_COST_THRESHOLD: 成本門檻／進場與目標相容性 — 提案 only，未部署
- [open] CAND_BATCH_SAME_DIR: CAND_BATCH_SAME_DIR: 同批同向集中曝險限制 — 提案 only，未部署
- [open] CAND_BREAKOUT_QUALITY: CAND_BREAKOUT_QUALITY: 突破品質濾網（完整樣本） — 提案 only，未部署

---
證據索引與 CSV 見同目錄。讀取採固定邊界；JSONL 末行不完整則延後並警告；多檔非原子快照已於 manifest 揭露。同日重跑冪等；原始帳戶／行情／策略／runtime／金庫**未修改**。本輸出不構成開新倉或變更既有開倉政策的批准。
