# DATA_AUDIT — D

## 快照
- 截止：`2026-11-22T09:24:54.432668+08:00`
- 方法：對 `paper_bcd_local/data_d/output/testnet_d_quiet_short_v1/` 及 log／launch_meta 做 `copy2` 唯讀快照（`snapshots/`）。
- 所有表共用此截止；帳本末更新 `2026-11-21T21:03:11.682901+08:00`（之後無新成交）。

## 身分防混
- 僅使用 profile `testnet_d_quiet_short_v1` + run_id `2026-11-21T1433_040b980f`。
- 未混入 A/B/C/E 帳本；testnet_live 下空殼 D 未用作績效。

## 資料盤點
| 資料 | 位置（快照內） | 筆數／範圍 | 備註 |
|------|----------------|------------|------|
| fills | fills.jsonl | OPEN 11 / CLOSE 10 | 模擬 |
| signals | signals.jsonl | 21 | 有 SIGNAL 事件 |
| events | events.jsonl | QUEUE 19 / CANCEL 8 / ENTRY 11 | |
| scan | scan_events.jsonl | {'QUEUE_RESULT': 19, 'SIGNAL_BLOCKED_DEPTH': 24, 'SIGNAL_BLOCKED_SLOT_CAP': 2} | 無完整 SCAN_STATS 聚合檔 |
| account | account_state.json | 1 | 殘倉 HYPE |
| trades.csv | 有 | 與 fills 對照用 | |
| K 線 | D vault **不存在** | — | 分析借用 paper C 1m vault |
| funding | — | N/A | funding_data_gap |

## 時區
- 紀錄為 `+08:00`（Asia/Taipei）。K 線 open_time 毫秒 epoch → 同區轉換。

## 串聯
- `trade_id` = `run_id:SYMBOL:hex`；`signal_id` = `SYMBOL:bar_close_time:SHORT:version`。
- OPEN↔CLOSE 以 trade_id 1:1（本批無分批腿）。
- 訊號→掛單：events 含 signal_id；部分訊號未進 QUEUE（深度／槽位擋）。

## 缺漏
- 缺 D 原生 K 線歸檔 → 路徑分析用 sibling 1m，標借用。
- 缺 bid/ask 歷史队列 → 不能重建真實 maker 排隊。
- funding N/A。
- scan 無完整「掃描宇宙每輪拒絕原因」總表（僅有部分 SIGNAL_BLOCKED_*）。
- **不可**用成交反推訊號總數；訊號日誌有 21 筆，以該數為準。

## 帳務對帳
| 項 | 值 |
|----|----|
| 毛已實現（已平） | -30.5087 U |
| 出場費（已平） | 1.5095 U |
| 進場費（全部 OPEN 含殘倉） | 1.6440 U |
| Σ close net_u | -32.0182 U |
| 帳戶 realized_net_u | -32.0182 U（= Σ close net，**不含**進場費） |
| 經濟已平 cycle 淨 | -33.5124 U（= close_nets − 已平進場費） |
| 若誤把進場費再扣進 realized_net 的差額 | -1.64402801（屬口徑差，非漏帳） |
| 費用合計 vs 帳戶 fees | 3.1535 vs 3.1535（差 0.00000000） |
| 現金核對 | acct 4966.3378 vs implied initial+realized_net−全部進場費 4966.3378（差 0.00000000） |
| funding | N/A |
| 滑價 | 已含於成交價；未再扣 |

**對帳結論：PASS。** D 的 `realized_net` 口徑=Σclose_net；進場費走現金／fees。經濟績效請用 cycle 淨（已平 −33.51U）。現金恒等式成立（差≈0）。

## Paper 樂觀假設（確認）
- 進場：觸價（bid≥limit）即以 limit 成交，無視排队深度耗盡（另有診斷 walk）。
- 出場：ask walk；深度不足仍出場並揭露 shortfall（本批未見 shortfall 欄位細查→N/A）。
- 非交易所真實成交。
