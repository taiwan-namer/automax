# paper_bulk_test_v1 review

> 更新時間：2026-11-19T12:07:50+08:00（Asia/Taipei）
> profile：`paper_bulk_test_v1`｜strategy_version：`breakout_follow_v1`｜run_id：`2026-11-19T1207_6734c9db`｜PID：`UNKNOWN`
> code_fix_tag：`fix_20261119_p0p1_trail_after_partial_daily2000_bulk_v1`｜config_hash：`x`（algo `stable_v1`）
> run_dir：`/Users/alan/MyProjects/automax/worktrees/breakout_follow_v1_fix_20261119/test_results/tmp/bfv1_test_review_reconciles_and_handles_empty_ogll9yrc/output/paper_bulk_test_v1`

## 程序與資料

- 帳戶：`/Users/alan/MyProjects/automax/worktrees/breakout_follow_v1_fix_20261119/test_results/tmp/bfv1_test_review_reconciles_and_handles_empty_ogll9yrc/output/paper_bulk_test_v1/account_state.json`｜初始本金 50000.0
- **已實現淨損益**：+0.0000 U｜**累計費用**：0.0000 U｜**現金**：50000.0000 U
- **浮動損益（最近快照）**：+0.0000 U｜**權益（含浮盈）**：50000.0000 U
- 持倉數：0／上限 20｜峰值持倉（快照）：0｜保證金使用率：0.00%
- 日損鎖定：False｜日起始權益：50000.0｜日損上限：2000.0
- 合格標的：0｜漲幅前10：0｜跌幅前10：0
- 最近掃描覆蓋：expected=None success=None failed=None — **失敗者不計已掃**
- 最近掃描耗時：0s｜候選 0｜開倉 0
- 資金費：`funding_data_gap=True`（缺口時不得冒充完整淨收益）
- 滑價假設每側 0.0002｜手續費假設每側 0.0005（SIMULATION_ASSUMPTION）
- fills 分段淨損益合計：+0.0000 U（對帳參考；與帳戶 realized 口徑見成本說明）

## 拒絕分布（decision_trace）

_尚無拒絕紀錄_

## 成交統計（模擬）

- OPEN=0 PARTIAL_TP=0 CLOSE=0
- 已實現淨損益 +0.0000 U（含費用已扣）｜費用合計 0.0000 U
- 勝率=n/a（樣本不足）｜平均盈=n/a（樣本不足）｜平均虧=n/a（樣本不足）｜盈虧比=n/a（樣本不足）｜PF=0.0000
- 分段淨損益 wins=0 losses=0｜每筆期望=+0.0000
- 平均持有秒數=n/a（樣本不足）（僅有 hold_sec 欄位之 CLOSE）
- 權益曲線含浮盈最大回撤（快照）：+0.0000 U

### 分組（CLOSE）

- 按標的：{}
- 按多空：{}
- 按退出原因：{}

## 持倉

_目前無持倉_

## 資料檔

- decision_trace / fills / equity / universe_history / quote_events / stop_events / klines_1m / data_quality.md
- 匯出：`python3 -m breakout_follow_v1.export_snapshot --run-id 2026-11-19T1207_6734c9db`

## 模擬限制

- 成交為公開 bid/ask + 不利滑價假設，**非**交易所實際成交。
- 未收集的逐筆行情**不會**補造。
- 參數首版預設，**未**最佳化，**不**保證獲利。

