# B／C Paper 退出規則升級 — 啟動報告

- 啟用時間：2026-11-20T10:47:59+08:00（Asia/Taipei）（`start_new_session` 守護）
- Runtime：`runtime_bc_exit_20261120T103434`
- 僅 paper；未改實盤／未動其他 profile（含 pid 33829）
- 備份：`backups_bc_exit_20261120T103434/`
- 指標：`BC_EXIT_LAUNCH_POINTER.json`

## 版本 ID

| 策略 | profile | EXIT_PLAN_ID | CODE_FIX_TAG | PID |
|---|---|---|---|---|
| B | `paper_reverse_fixed_usdt_50_75_v1` | `b_partial70_30_tp50_tp100_20261120` | `fix_20261120_b_partial70_30_tp50_tp100` | 59740 |
| C | `paper_reverse_fixed_usdt_50_75_inv_v1` | `c_pyramid300_r2_p0tp1pct_20261120` | `fix_20261120_c_pyramid300_r2_p0tp1pct` | 59741 |
| 舊倉／舊掛單 | 同上帳戶 | `legacy_fixed_gross_tp50_sl75` | 啟動時戳記 | — |

## 最終規則

### B
- 訊號／限價 0.5%／名義 10000／保證金 1000／槓桿 10／費率滑價不變
- 新開倉：70% @ 原整倉毛 TP50 價；剩餘 30% @ 原整倉毛 TP100 價
- 首次 70% **實際成交後**，剩餘停損移至進場均價（≠扣費保本）
- 首次止盈完成前：原 SL75 不變
- 同報價 TP/SL 競態：止損優先；禁止超量／重複平倉

### C
- 訊號／限價 0.5%／FLIP=False 不變
- 新開倉：每層保證金 300、名義 3000；最多 P0/P1/P2
- 追加：相對前層不利 0.5%；退出後不循環補回
- P0 止盈：相對 P0 成交價有利 1%（非毛 100、非全倉毛 30 誤解）
- P1/P2：反彈至 **P0 進場價** 各自退出（舊 R2 方向）
- Pstop：第三層再不利 0.5%（理論路徑自 P0 凍結；不隨均價刷新）
- **精度差異**：交易所 tick 會改變理論價。例 tick=0.01 時 P0=10 → P2≈9.90、Pstop≈9.85（理論未取整為 9.90025 / 9.85074875）

## 隔離
- 舊持倉／舊掛單戳記 `legacy`；保留原 TP/SL；成交時沿用名義 10000／毛 50/75
- 不撤既有掛單；新規則僅新訊號掛單戳記新 `exit_plan_id`
- 帳戶回撤計算：**未完成**（不用獨立交易回撤冒充）

## 驗收
`PYTHONPATH=code python3 tests/test_bc_exit_acceptance.py` → ALL PASSED  
（P0-only TP／兩層反彈不補倉／三層停損／B SL／B70→TP100／B70→進場停／legacy 隔離／理論價）

## 統計入口
- fills：`fixed_data_root/output/<profile>/fills.jsonl`（含 `exit_plan_id`）
- runs：`.../runs/<run_id>/`
- 指標過濾：`exit_plan_id == b_partial70...` 或 `c_pyramid300...`
- **禁止**把舊離線 +78.4357 / +39.4143 當本日新版本績效

## 回滾
1. `kill 59740 59741`（或 paper_main `--stop` 同 profile）
2. 必要時還原 `backups_bc_exit_20261120T103434/{B,C}_account_state.json`
3. 用 `runtime_fixed_usdt_50_75_20261119T231853` 重啟舊版

## 自動報告
未新設排程；既有若有則沿用。本輪不假稱已設自動報告。

## 終端機監看
```bash
# B
tail -f /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/bc_exit_B_20261120T104749.out.log

# C
tail -f /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/bc_exit_C_20261120T104749.out.log
```
