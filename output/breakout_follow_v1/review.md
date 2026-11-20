# breakout_follow_v1 review

> 更新時間：2026-11-20T13:24:38+08:00（Asia/Taipei）
> strategy_version：`breakout_follow_v1`｜run_id：`20261119T115315_b984d9a7`｜PID：`33829`
> config_hash：`11b6c16d45b49fad`（algo `stable_v1`）
> run_dir：`output/breakout_follow_v1/runs/20261119T115315_b984d9a7`

## 程序與資料

- 帳戶：`output/breakout_follow_v1/account_state.json`｜初始本金 10000.0
- **已實現淨損益**：-669.7691 U｜**累計費用**：150.0953 U｜**現金**：9330.2309 U
- **浮動損益（最近快照）**：+0.0000 U｜**權益（含浮盈）**：9330.2309 U
- 持倉數：0｜日損鎖定：True｜日起始權益：9668.782178357955
- 合格標的：81｜漲幅前10：10｜跌幅前10：10
- 最近掃描覆蓋：expected=81 success=81 failed=0 — **失敗者不計已掃**
- 最近掃描耗時：32.127s｜候選 0｜開倉 0
- 資金費：`funding_data_gap=True`（缺口時不得冒充完整淨收益）
- 滑價假設每側 0.0002｜手續費假設每側 0.0005（SIMULATION_ASSUMPTION）

## 拒絕分布（decision_trace）

| reason | n |
|---|---:|
| VOL_X | 107302 |
| NO_BREAKOUT | 2541 |
| STALE_QUOTE | 1071 |
| CHASE | 647 |
| DAILY_LOSS_LOCK | 427 |
| SPREAD | 70 |
| FILL_NOT_BEYOND_BREAKOUT | 38 |
| SIGNAL_DELAY | 1 |
| MAX_POSITIONS | 1 |

## 成交統計（模擬）

- OPEN=7 PARTIAL_TP=0 CLOSE=7
- 已實現淨損益 -669.7691 U（含費用已扣）｜費用合計 150.0953 U
- 分段淨損益 wins=0 losses=7｜PF=0.0｜每筆期望=-95.6813
- 權益曲線含浮盈最大回撤（快照）：-372.5053 U

## 持倉

_目前無持倉_

## 資料檔

- decision_trace / fills / equity / universe_history / quote_events / stop_events / klines_1m / data_quality.md
- 匯出：`python3 -m breakout_follow_v1.export_snapshot --run-id 20261119T115315_b984d9a7`

## 模擬限制

- 成交為公開 bid/ask + 不利滑價假設，**非**交易所實際成交。
- 未收集的逐筆行情**不會**補造。
- 參數首版預設，**未**最佳化，**不**保證獲利。

