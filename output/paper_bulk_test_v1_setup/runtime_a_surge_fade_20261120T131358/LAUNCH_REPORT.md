# A 暴漲轉弱做空 Paper — 啟動報告

- **已啟動**：2026-11-20T13:29:02+08:00（Asia/Taipei）
- 版本：`a_surge_fade_short_v1_20261120`
- 執行器 PID：見 `A_SURGE_FADE_LAUNCH_POINTER.json`（目前 78364）
- 快照 sleeper PID：78365（一次性 +6h/+8h，非 cron）
- Runtime：`runtime_a_surge_fade_20261120T131358`
- 舊 A（`paper_reverse_fixed_exit_v1`）：未啟動；持倉未撤未平
- B/C：未修改、未重啟（59740 / 59741）

## 重啟說明
首次啟動（77483）掃描一次拉取過多 1m K 線，阻塞管理迴圈逾數分鐘。
已改為「每切片 ≤12 標的／牆鐘 ≤25s、標的間強制 manage、cursor 輪詢宇宙」後於 13:29:02 重啟 A。

## 三臂（隔離資金簿）
| 臂 | profile | 第一目標 | 第二目標 |
|---|---|---|---|
| A_TP100 | paper_a_surge_fade_tp100_v1 | -1% 平70% | -2% |
| A_TP200 | paper_a_surge_fade_tp200_v1 | -2% 平70% | -4% |
| A_TP350 | paper_a_surge_fade_tp350_v1 | -3.5% 平70% | -7% |

## 配置摘要
- 只做空；自主掃描；非鏡像
- 進場：S×1.01 限價賣空；中價 ≤S×0.98 撤未成交
- 停損：進場均價×1.005；首次70%後剩餘停損→進場價（≠扣費保本）
- TTL：舊A `LIMIT_ENTRY_TTL_SEC=0`；沿用「相對S跌2%撤單」有界條件，無時間TTL無限掛單
- 費率 5bps／滑價 0.0002；無 maker 優惠
- 成交模型：`bid≥限價` 觸價，成交價=限價；過期報價不開／不平
- 宇宙：USDT 永續 + TradFi/槓桿/穩定幣排除；硬排除僅 BTC/ETH；**未宣稱排除全部大型幣**
- 流動性：MIN_QUOTE_VOLUME=20M；MAX_SPREAD=0.0008

## 驗收
`PYTHONPATH=code python3 tests/test_a_surge_fade_acceptance.py` → ALL PASSED

## 快照
- 一次性 sleeper +6h/+8h → `reports/snapshot_{6h,8h}.json`
- 非 cron／systemd；環境可排程與否：以 sleeper 實作，已掛起

## 回滾
1. `kill <A_pid>`（pointer）
2. 可選刪 `fixed_data_root/output/paper_a_surge_fade_*`
3. 不影響 B/C／舊A

## 帳戶回撤
未完成完整帳戶回撤計算（標註）。
