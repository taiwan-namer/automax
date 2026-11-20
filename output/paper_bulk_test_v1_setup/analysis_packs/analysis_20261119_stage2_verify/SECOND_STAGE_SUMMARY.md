# paper_bulk_test_v1 第二階段唯讀查證摘要

生成：2026-11-19T15:04:46+08:00  
歷史分析截止（不變）：**2026-11-19T14:23:11+08:00**  
Target run：`20261119T132017_bd1972b2`  
程式依據：`runs/.../code_snapshot`；與 launch 指向之 sealed runtime **hash 吻合**（engine/portfolio/market_data/run_store/config；`code_hashes.json` + `package_dir` 比對）。  
PID 39844 cmdline：`python -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 --runtime-root .../fixed_data_root`（與 launch.json 一致）。

---

## 1. 最新近況（與歷史樣本分開；時點不同勿硬湊）

| 來源 | 時間 | 距讀取約 |
|---|---|---|
| 本摘要讀取 | 2026-11-19T15:04:46+08:00 | 0 |
| ACCOUNT_ROOT `account_state.json` `updated_at` | 2026-11-19T15:04:41+08:00 | 6s |
| 最近 `equity_snapshots` | 2026-11-19T15:04:04+08:00 | 43s |
| run `review.md` mtime／文內更新 | 約 14:55:11（文內） | 較舊於帳戶 |

**帳戶（2026-11-19T15:04:41+08:00）**  
- 現金 **44822.36 U**｜已實現 **-1177.6358 U**｜累計費用 **400.24 U**｜`funding_data_gap=True`  
- 持倉 **4**：XRPUSDT LONG, INJUSDT LONG, JCTUSDT LONG, NIGHTUSDT SHORT  
- 保證金合計（倉位 margin 加總）**4000 U**｜日損鎖 **False**｜日起始權益 50000  

**權益快照（2026-11-19T15:04:04+08:00，與帳戶非同一秒）**  
- 權益 **48852.30**｜浮動 **29.93**｜快照內 realized **-1177.6358**｜cash **44822.36**｜positions欄 **4**  

**掃描（review 文內，時點偏舊）**：最近 expected／success／failed 以 review 為準（約 14:55：71/71/0，耗時~6.4s）。**不得**與 15:00 帳戶混成同一對帳。

> 以下 §2–§5 只用 ≤14:23:11 歷史資料。

---

## 2. PIPPIN 13:59:05 報價跳動

### 已證實
1. **兩種 purpose 皆打同一公開 API**：`GET /fapi/v1/ticker/bookTicker`（`market_data.py` `fetch_book_tickers` L675–695）；回應後以本機 `time.time()` 寫入 **同一批** 全市場 `fetched_at`。**無** bookTicker 快取（K 線 vault 快取與此無關）。
2. **時間語義**：`fetched_at`＝本機收齊 bookTicker 後時間戳；`recorded_at`＝寫入 `quote_events` 時 `now_iso()`（`run_store.py` L289–290）；fills 的 `book_ts`＝當次 book 的 `fetched_at`（`portfolio.py` L444）。**沒有**交易所事件時間欄位 → 不能當市場真實時間。
3. **兩筆為不同次請求**：MANAGE_POLL `fetched_at=1795067945.13475`；MANAGE `fetched_at=1795067945.527479`（差 **0.393s**）。路徑：POLL＝主迴圈 sleep 後刷新（`engine.py` L773–797）；MANAGE＝`scan_once` 開頭再拉一次（L299、L368–379）。
4. **止損／成交用 MANAGE 那筆**：CLOSE `book_ts=1795067945.527479`、價 **0.017283456**＝`ask 0.01728 ×(1+0.0002)`（與 MANAGE 列一致）。
5. 寫入規則：每次成功取得可用 book 且進入 manage 前 **都** `log_quote_event`（失敗／不可用則跳過並可能寫 DQ，不寫假價）。

### 關鍵時間線（≤8）
1. 13:59:01 POLL ask=0.01697  
2. 13:59:05 POLL ask=**0.01698** mark=0.01698340＜stop（L52）  
3. 13:59:05 MANAGE ask=**0.01728** mark=0.017283456≥stop（L53）  
4. 同秒 INITIAL_STOP 成交 @0.017283456  
5. code：POLL L773–797；scan books L299；MANAGE log L368–379；`manage_position` 觸發 L545–560  
6. 無交易所逐筆 → **無法**定首次穿價時刻  

### 原因分類（不單選）
| 類 | 判定 | 證據 |
|---|---|---|
| A 市場真實快速跳動 | **有跡象** | 兩次成功 API 的 ask 差 +1.77%/0.39s |
| B 快取／新鮮度差 | **反證為主** | 無 book 快取；fetched_at 不同；皆新請求 |
| C 流程差異 | **已證實有差異、但同 API** | POLL vs scan-MANAGE 呼叫點不同 |
| D 資料不足 | **已證實部分不足** | 無中間 tick／無交易所時間 |

**待確認**：0.39s 內交易所是否連續成交推動 ask（需逐筆，本庫沒有）。

---

## 3. TAO「56 秒」代表什麼

### 已證實
- 可觀察 quote：14:04:44 MANAGE_POLL → 14:05:40 MANAGE_POLL（fetched_at 差 **≈56.45s**）；**空窗內任何標的 quote_events=0**。  
- 同期 DQ：`HTTP_TIMEOUT` bookTicker @14:05:05 / 14:05:21 / 14:05:35，隨後 **`MANAGE_POLL_ERROR timeout budget for /fapi/v1/ticker/bookTicker`**（`data_quality.jsonl`）。  
- 程式：POLL 先 `fetch_book_tickers`，整批失敗則進 except，**不會**對各倉呼叫 `manage_position`（`engine.py` L772–811）。  
- 同空窗 LIT/FET/KAVA 等亦同為 56.5s → **全市場管理輪詢中斷**，非 TAO 單幣被跳過。  
- 持倉管理與分鐘掃描**共用**主迴圈；掃描前以 ≤5s sleep 切片做 POLL；掃描內先 MANAGE 再對宇宙拉 K／評分（網路慢可阻塞後續）。

### 口徑分開
- **可觀察事件間隔**：成功寫入之 MANAGE/MANAGE_POLL 相鄰 fetched_at 差。  
- **已證實成功管理間隔**：同上（因成功管理前必寫 quote）；**失敗嘗試不產生 quote**。  
- **不得**寫成「程式設定成 56s 才管一次」：設定 `MANAGE_POLL_SEC=5`（`config.py` L195）；本次 56s 是 **bookTicker 逾時／預算耗盡** 造成的成功管理空窗。

### 歷史區間（≤14:23:11）成功管理事件間隔
樣本 **n=1592**｜P50 **≈5.14s**｜P95 **≈9.87s**｜max **≈59.0s**  
限制：不含失敗 POLL；max 亦見於 14:01–14:02 附近 K 線／book 逾時段。

### 待確認
- 逾時根因（本機網路／對方限速／事件迴圈佔用）僅有 timeout 紀錄，**無法**細分。

---

## 4. 五筆跨週期比較（固定 trade_id；≤14:23:11）

**定義**：訊號= `signal_open_time` 那根 1m OHLC；10m/15m=1m 以 **UTC 邊界**聚合之**進場前已完整收盤**桶（derived）；進場後 1/3/5m＝進場分鐘起第 n 根 **收盤相對成交價** 的方向調整報酬（多漲為正／空跌為正；**分鐘近似**）；止損後 5/10m 是否触及入場價＝出場後 1m OHLC 診斷，**非持倉收益、非當時濾網**。

| 標的 | 向 | 開→平 | 完整淨U | 止損距% | 風險U | 來回費/風險 | 訊號振幅% | 實體比 | 近完整10m% | 近完整15m% | 後1/3/5m調 | 止後5/10回入場 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| PIPPIN | S | 13:55→13:59 | **-184.85** | 0.809 | 80.94 | 0.125 | 0.879 | 0.53 | -0.41 | 0.00 | +0.27/+0.22/-0.67 | Y/Y |
| TAO | L | 14:03→14:05 | **-31.83** | 0.171 | 17.13 | **0.583** | 0.204 | 0.28 | +0.29 | +0.38 | -0.06/-0.20/-0.26 | N/N |
| FET | L | 14:03→14:05 | **-37.25** | 0.210 | 20.96 | 0.476 | 0.311 | 0.50 | +0.08 | 0.00 | -0.10/-0.25/-0.33 | N/Y |
| LIT | L | 13:54→14:06 | **+183.77** | 0.313 | 31.30 | 0.323 | 0.447 | 0.53 | +0.19 | -0.27 | -0.19/-0.07/+0.39 | Y/Y* |
| JUP | S | 12:59→13:53 | **+87.92** | 0.261 | 26.14 | 0.381 | 0.373 | 0.60 | -0.26 | +0.30 | -0.06/+0.35/+0.32 | Y/Y* |

\*成功單「回入場」僅描述出場後價格曾穿越入場價，**不是**策略訊號。  
TAO/FET：**進場後分鐘近似持續逆向**；PIPPIN 前3m 曾有利但第5m 轉逆。  
**不**宣稱換 10m/15m 濾網會變好（樣本 5、有偏）。

---

## 5. 報表修正規格（只規格，不改程式）

review 應分欄，禁止混用：

**A 帳戶累計**（至報表時點）：realized_net、fees、funding_gap、cash、權益（註明浮盈時點）、日損鎖。  
**B 當次 run 事件**：OPEN/CLOSE/CLOSE_PARTIAL 計數；分段淨損益合計；拒絕原因。  
**C 跨 run 完整 trade_id**：完整平倉數、勝敗、勝率、總淨利、均盈虧、盈虧比、PF、期望；淨利＝OPEN 費（或 OPEN.realized_net_u）+ 所有 PARTIAL/CLOSE.net_u（**不重扣**）。  
**D 未平倉**：標的／方向／已扣開倉費；**勿**計入完整勝率；未平倉開倉費與已平倉費用分開列。  

另標：`funding_data_gap`；MFE/MAE=`polled_quote_estimate`；cash_delta≠PnL。

---

## 6. 下一步（最多 3）

1. **可直接採用的分析口徑**：績效用 C 完整 trade_id；超額止損拆「觀測跳價 vs 2bp 滑價」；管理健康看「成功 manage 間隔 + MANAGE_POLL_ERROR／bookTicker timeout」並列，不把空窗當成參數間隔。  
2. **需要另行批准的程式修改**：review 分 A/B/C/D 欄；可選加強 DQ（逾時時記錄 attempt 與 sleep 進度）——**本次不改**。  
3. **尚需更多樣本的策略假設**：「進場後 1–5m 方向調整報酬」能否當品質標籤；10m/15m 當濾網是否有用——需更大樣本，**禁止**本階段部署調參。

---

*唯讀查證；未改策略／runtime／帳戶／金庫；未呼叫交易所補資料；未新增心跳。*
