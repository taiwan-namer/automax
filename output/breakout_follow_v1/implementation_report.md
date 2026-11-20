# breakout_follow_v1 實作報告

> 產生時間：啟動前自動寫入／更新  
> 性質：**獨立模擬策略**（非 FADE/MOMO 調參）  
> 參數：**首版預設**，SIMULATION_ASSUMPTION，**未**最佳化、**不**保證獲利

---

## 1. 隔離與安全

| 項 | 狀態 |
|---|---|
| 獨立輸出目錄 | `output/breakout_follow_v1/` |
| 獨立帳戶 | `output/breakout_follow_v1/account_state.json`（首次才建 10000U） |
| 獨立 K 線庫 | `data/breakout_follow_v1/kline_vault/` |
| 舊 FADE/MOMO 帳本／manifest／孤立 OPEN | **未觸碰** |
| 舊 FADE 全面禁空 | **仍保留**（`tests/test_fade_long_only.py`） |
| Paper 入口 | `python3 -m breakout_follow_v1.paper_main --paper`（缺 `--paper` 無法用預設啟動） |
| HTTP | 僅 GET；`GuardedSession` + `FORBIDDEN_ORDER_PATH`；POST/order 路徑拋 `LiveTradingForbidden` |

### 啟動前進程

實作時檢查：無 `main.py --live`／`breakout_follow_v1.paper_main` 進程。若無法確認或發現衝突 → **不自動啟動**。

---

## 2. 掃描可行性（60 秒）

公開 REST 探測（實測）：

- 合格 `quoteVolume>=20M` 約 **140** 標的
- `bookTicker` 全市場一次拉取 ~0.05s
- 50 幣 `klines limit=30` concurrency=10 ~0.27s → 外推全市場 **~1s**

**結論：可維持每分鐘掃描，未降級成 10 分鐘。**  
失敗標的計入 `failed`，**不宣稱已掃**。

---

## 3. 策略摘要（首版）

- 訊號：已收盤 1m；S 前 20 根區間；`vol_x>=3`；突破 ±0.1×ATR；Wilder ATR(14) 排除 S
- 成交：公開 bid/ask + 不利滑價 0.0002 + 單邊費 0.0005（**假設**）
- 風險：每筆保證金 1000×10x；估損 >100U → `RISK_BUDGET_EXCEEDED`（不縮 SL／不減倉）
- 日損：日起始淨權益降 ≥300U → 鎖新倉
- 分批：淨浮盈 ≥100U 平 30% 一次 → 剩餘含成本保本 → ATR 移動停利只收緊
- 反轉：先平後開；反手失敗不留倉；30 分最多 2 次反手

---

## 4. 檔案

| 路徑 | 用途 |
|---|---|
| `breakout_follow_v1/config.py` | 參數 |
| `breakout_follow_v1/safety.py` | 實盤路徑封鎖 |
| `breakout_follow_v1/account.py` | 原子帳戶／持倉 |
| `breakout_follow_v1/market_data.py` | 宇宙／K線／盤口 |
| `breakout_follow_v1/signals.py` | 突破偵測 |
| `breakout_follow_v1/portfolio.py` | 開平／分批／追蹤／反轉 |
| `breakout_follow_v1/engine.py` | 主循環 |
| `breakout_follow_v1/paper_main.py` | `--paper` 入口 |
| `breakout_follow_v1/review.py` | `review.md` |
| `tests/breakout_follow_v1/test_breakout_core.py` | 核心測試 |

---

## 5. 測試結果

見執行當下記錄（`python3 tests/breakout_follow_v1/test_breakout_core.py` → `breakout core ok`；`test_fade_long_only.py` → 通過）。

涵蓋：多空突破、量倍邊界、區間排除 S、未收盤排除、價差／風險拒絕、部分止盈一次、反轉先平後開失敗無幽靈倉、重啟保本金、實盤路徑封鎖、config hash 穩定。

---

## 6. 模擬限制

- 非真實成交；資金費可能 `funding_data_gap=true`
- 持倉管理目標 ≤5s 輪詢，異常時可能延遲
- 6 小時為**觀察窗口**，時間到**不**強制平倉、**不**自動調參

---

## 7. 實際啟動證據

| 項 | 值 |
|---|---|
| 命令 | `python3 -u -m breakout_follow_v1.paper_main --paper --hours 6` |
| PID | `85985`（見 `paper.pid`；以 `start_new_session` 脫離工具 shell） |
| 備用啟動 | `./breakout_follow_v1/launch_paper_6h.sh` |
| 版本 | `breakout_follow_v1` |
| run_id | `2026-11-19T0232_a8c35e7a` |
| 帳戶 | `output/breakout_follow_v1/account_state.json`（本金 10000，重啟沿用） |
| 首輪掃描 | expected=70 success=70 failed=0 duration≈1.6s（stdout／review） |
| 日誌 | `output/breakout_follow_v1/paper_stdout.log` |
| 舊策略進程 | 未啟動；舊 `live_trades_log`／legacy manifest **未改** |

合格宇宙約 70（USDT 永續＋排除 TradFi／非加密＋成交額≥20M；**不過濾**主流大幣，但受 exchangeInfo 可交易篩選影響）。
