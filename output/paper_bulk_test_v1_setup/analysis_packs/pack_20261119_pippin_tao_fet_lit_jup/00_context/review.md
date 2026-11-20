# paper_bulk_test_v1 review

> 更新時間：2026-11-19T14:23:11+08:00（Asia/Taipei）
> profile：`paper_bulk_test_v1`｜strategy_version：`breakout_follow_v1`｜run_id：`20261119T132017_bd1972b2`｜PID：`39844`
> code_fix_tag：`fix_20261119_p0p1_trail_http429_bulk_reverse`｜config_hash：`1ad6a35c98b26446`（algo `stable_v1`）
> run_dir：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1/runs/20261119T132017_bd1972b2`

## 程序與資料

- 帳戶：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1/account_state.json`｜初始本金 50000.0
- **已實現淨損益**：-1169.3353 U｜**累計費用**：295.1675 U｜**現金**：47830.6647 U
- **浮動損益（最近快照）**：+58.5857 U｜**權益（含浮盈）**：48889.2504 U
- 持倉數：1／上限 20｜峰值持倉（快照）：8｜保證金使用率：2.00%
- 日損鎖定：False｜日起始權益：50000.0｜日損上限：2000.0
- 合格標的：70｜漲幅前10：10｜跌幅前10：10
- 最近掃描覆蓋：expected=70 success=70 failed=0 — **失敗者不計已掃**
- 最近掃描耗時：6.836s｜候選 0｜開倉 0
- 資金費：`funding_data_gap=True`（缺口時不得冒充完整淨收益）
- 滑價假設每側 0.0002｜手續費假設每側 0.0005（SIMULATION_ASSUMPTION）
- fills 分段淨損益合計：-569.7143 U（對帳參考；與帳戶 realized 口徑見成本說明）

## 拒絕分布（decision_trace）

| reason | n |
|---|---:|
| VOL_X | 4199 |
| NO_BREAKOUT | 101 |
| CHASE | 52 |
| STALE_QUOTE | 9 |
| SPREAD | 7 |
| RISK_BUDGET_EXCEEDED | 3 |
| FILL_NOT_BEYOND_BREAKOUT | 2 |

## 成交統計（模擬）

- OPEN=17 PARTIAL_TP=2 CLOSE=20
- 已實現淨損益 -1169.3353 U（含費用已扣）｜費用合計 295.1675 U
- 勝率=0.2000｜平均盈=70.4241｜平均虧=-47.3006｜盈虧比=1.4889｜PF=0.3309
- 分段淨損益 wins=4 losses=18｜每筆期望=-53.1516
- 平均持有秒數=475.8969（僅有 hold_sec 欄位之 CLOSE）
- 權益曲線含浮盈最大回撤（快照）：-719.5438 U

### 分組（CLOSE）

- 按標的：{'JCTUSDT': 2, 'QNTUSDT': 1, 'HEIUSDT': 1, 'PENGUUSDT': 1, 'ICPUSDT': 1, 'ICNTUSDT': 1, 'VVVUSDT': 1, 'JUPUSDT': 1, 'EVAAUSDT': 1, 'HBARUSDT': 1, 'PIPPINUSDT': 1, '1000PEPEUSDT': 1, 'TAOUSDT': 1, 'FETUSDT': 1, 'LITUSDT': 1, 'KAVAUSDT': 1, 'XPLUSDT': 1, 'ASTERUSDT': 1, 'ZROUSDT': 1}
- 按多空：{'SHORT': 10, 'LONG': 10}
- 按退出原因：{'INITIAL_STOP': 18, 'TRAILING_STOP': 2}

## 持倉

| symbol | side | qty_rem | entry | stop_eff | partial | trade_id |
|---|---|---:|---:|---:|---|---|
| PUMPUSDT | LONG | 3757234.0 | 0.0026615322 | 0.002653409808660082 | False | `20261119T132017_bd1972b2:PUMPUSDT:2eb79a9a4d` |

## 資料檔

- decision_trace / fills / equity / universe_history / quote_events / stop_events / klines_1m / data_quality.md
- 匯出：`python3 -m breakout_follow_v1.export_snapshot --run-id 20261119T132017_bd1972b2`

## 模擬限制

- 成交為公開 bid/ask + 不利滑價假設，**非**交易所實際成交。
- 未收集的逐筆行情**不會**補造。
- 參數首版預設，**未**最佳化，**不**保證獲利。

