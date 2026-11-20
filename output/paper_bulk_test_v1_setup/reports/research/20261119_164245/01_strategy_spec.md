# 01 策略規格（依實際 sealed／運行設定；非任意 worktree）

基準 sealed：
- A 早期／日損2000：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fix_source_sealed_20261119T122904/breakout_follow_v1`（及同日更早 seal）
- B 管理修復：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fix_source_sealed_20261119T155304/breakout_follow_v1` tag `fix_20261119_mgmt_reliability_v1`
- C 日損5000＋同批同向：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fix_source_sealed_20261119T160723/breakout_follow_v1` tag `fix_20261119_mgmt_dl5000_batchside1`

引擎 hash（C）：`7441009efec28c05f517b7b03718156d6d327aa7b7e7c53f5efac9ec83a38220`  
profiles hash（C）：`c63942a59ad25adc24a9b3c38194cad37d938bd7fb64969c9462b4a326d862dc`  
manage_pass hash（B=C 未改）：`6cf8648fc2b27fe3d8e095b84f40c214b5dfedf3ccc2214d53aff3676aaaf159`

## 掃描標的池與頻率
- Universe：USDT-M 永續，依 24h quote volume 等過濾；`TOP_N=10` gainers/losers 標籤；持倉標的掉榜仍掃描。  
  來源：`market_data.refresh_universe`、`UNIVERSE_REFRESH_MINUTES=10`（config）
- 掃描節奏：每分鐘掃描（`SCAN_SECOND=2` 對齊）；主迴圈 `engine.scan_once` / `paper_main`
- 暖機：啟動補齊 K 線（觀測用）；`KLINES_FETCH_LIMIT=40` 等（config）

## K線／指標／收盤
- 週期：**1m** vault；突破區間 lookback `RANGE_LOOKBACK=20`；量能均線 `VOLUME_MA_PERIOD=20`；ATR `ATR_PERIOD=14` Wilder  
  來源：`signals.detect_breakout`、`market_data.wilder_atr`、`closed_klines`
- 收盤判定：使用已閉合 bar（排除未收盤）；訊號延遲上限 `SIGNAL_MAX_DELAY_SEC=90`
- vol_x：訊號 bar 量 / 均量；門檻 `VOL_X_MIN=3.0`
- 突破緩衝：`BREAKOUT_BUFFER_ATR=0.1`；追價上限 `MAX_CHASE_ATR=1.0`
- 價差：`MAX_SPREAD=0.0008`；報價年齡 `QUOTE_MAX_AGE_SEC=5` / `BOOK_TICKER_MAX_AGE_SEC=5`

## LONG／SHORT 進場
- 完整突破訊號（方向依突破上／下沿）→ 通過點差／新鮮度／vol_x／chase 等條件  
  來源：`signals.detect_breakout`
- 開倉：`portfolio.try_open`（日損鎖、倉數、保證金、風險預算、同標的、cooldown、無有效報價不得捏造）

## 候選評分／排序（重要）
- **排序規則原本就存在**（早於 C 版）：`vol_x` 降序 → `spread` 升序 → `symbol` 升序  
  位置：`engine.scan_once` 候選 `sort`；C 版抽成 `_candidate_rank_key` 並寫入 `SCAN_BATCH_RANK`
- **C 版新增（策略變更）**：同一 `scan_batch_id`、同方向最多成功新開 1 檔（`SAME_SCAN_BATCH_SIDE_LIMIT`）；反手新開併入同批排序；退出不受限  
  位置：`engine.py`（C sealed）

## 部位／風險
- 保證金 `MARGIN_PER_TRADE_USDT=1000`；槓桿 `LEVERAGE=10`；名義約 `NOTIONAL_USDT=10000`
- 單筆最大虧損預算 `MAX_LOSS_PER_TRADE_USDT=100`；最多同時倉 `MAX_CONCURRENT_POSITIONS=20`；每標的 `MAX_PER_SYMBOL=1`
- 來源：`profiles.PAPER_BULK_TEST_V1` + `portfolio.try_open`

## 止損／BE／部分止盈／追蹤／反手
- 初始止損：`ATR_STOP_MULT=1.5` × ATR（`portfolio` 開倉）
- 追蹤：`TRAIL_ATR_MULT=1.5`
- 部分止盈：`PARTIAL_TP_NET_USDT=100`、`PARTIAL_TP_FRACTION=0.3`
- BE：程式內 breakeven 邏輯（`portfolio.manage_position`）
- 反手：完整反向突破 → 先平後開；30 分窗最多 2 次；C 版新開受同批同向上限  
  來源：`engine.execute_reverse`、`MAX_REVERSES_PER_WINDOW=2`

## 費用／成交模擬
- 單邊費率 `FEE_RATE_ONE_SIDE=0.0005`（**paper 模擬費**）
- 不利滑價 `SLIPPAGE_ADVERSE=0.0002`
- 成交用 book bid/ask；無有效報價不得假造成交  
  fills 欄位含 `simulation_assumption`

## 日損
- A/B：`DAILY_LOSS_LOCK_USDT=2000`；C：`5000`
- 日界：`TZ_NAME=Asia/Taipei`；`day_start_equity_u` + `day_key`；觸發設 `day_new_entries_locked`（禁新開，仍可管／平）
- 解鎖：日切或經測試的 `reevaluate_daily_loss_lock`；**不重設 day_start**
- 來源：`account.py`、`engine.scan_once`、`profiles.py`

## 同批同向（僅 C）
- 明確 `scan_batch_id`；所有新開倉入口（含反手 reopen）經開倉迴圈限制
- 退出／減倉不受限；不得因跳過反手新倉而漏平倉

## 管理排程（B 起部署，C 沿用）
- `MANAGE_DUE_SEC=5`；book timeout 5s、最多 2 次；`MANAGE_BOOK_OP_BUDGET_SEC=10`；`SCAN_SLICE_MAX_WAIT_SEC=2`
- due 插隊、不補跑；Book／ATR 分階；`manage_events`；429/418 共用冷卻  
  來源：`manage_pass.py`、`market_data.py`
- **fake clock 測試 ≠ 真實網路硬牆鐘保證**

## 版本對照提醒
- 不可用目前未封 worktree 冒充歷史
- Overlay 印出的舊 CODE_FIX_TAG 字串不可凌駕 code hash／cwd
