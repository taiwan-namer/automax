# paper_daily_review（唯讀）

每日紙上帳戶復盤工具。不修改策略／配置／帳戶／金庫／runtime；不呼叫交易所；不啟用排程。

## 日界（報告用）

- **結算**：每日 **20:15**（Asia/Taipei）。交易日 `D` = `[D-1 20:15, D 20:15)`。
- **產報**：每日 **20:25**（排程範例；launchd 未載入）。
- 與運行中 paper 引擎日損 `day_key`（日曆午夜）**分離**，除非另行批准改引擎。

## 路徑

- 套件：`tools/paper_daily_review/paper_daily_review/`
- 輸出：`analysis_packs/daily_reports/YYYY-MM-DD/`
- 共用：`DAILY_INDEX.md`、`CHANGELOG.md`、`STRATEGY_HYPOTHESES.md`
- 排程範例（未載入）：`schedule/`

## 使用

```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/tools/paper_daily_review
PYTHONPATH=. python3 -m paper_daily_review \
  --setup-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup \
  --mode day --day 2026-11-19
```

測試：

```bash
PYTHONPATH=. python3 -m pytest tests -q
# 或：
PYTHONPATH=. python3 tests/run_offline_tests.py
```

## 狀態分離

| 項目 | 狀態 |
|---|---|
| 本工具 | 已實作（離線）；日界已改 20:15／報告 20:25 |
| launchd | 文件 only，未載入 |


## 多 profile

| profile | 帳戶 | 報告輸出 |
|---|---|---|
| `paper_bulk_test_v1`（預設） | `fixed_data_root/output/paper_bulk_test_v1` | `analysis_packs/daily_reports/` |
| `paper_reverse_fixed_exit_v1`（A） | `fixed_data_root/output/paper_reverse_fixed_exit_v1` | `analysis_packs/daily_reports_paper_reverse_fixed_exit_v1/` |
| `paper_reverse_fixed_usdt_50_75_v1`（B） | `fixed_data_root/output/paper_reverse_fixed_usdt_50_75_v1` | `analysis_packs/daily_reports_paper_reverse_fixed_usdt_50_75_v1/` |
| `paper_reverse_fixed_usdt_50_75_inv_v1`（C） | `fixed_data_root/output/paper_reverse_fixed_usdt_50_75_inv_v1` | `analysis_packs/daily_reports_paper_reverse_fixed_usdt_50_75_inv_v1/` |

```bash
PYTHONPATH=. python3 -m paper_daily_review \
  --setup-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup \
  --profile paper_reverse_fixed_exit_v1 \
  --mode day --day 2026-11-20
```

反向排程範例（**未載入**）：`schedule/com.automax.paper-reverse-daily-review.plist.example`
