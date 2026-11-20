# paper_reverse_fixed_exit_v1 報告接線

## 隔離
- 帳戶：`fixed_data_root/output/paper_reverse_fixed_exit_v1/`
- 報告輸出：`analysis_packs/daily_reports_paper_reverse_fixed_exit_v1/`
- **不**寫入 `analysis_packs/daily_reports/`（舊 bulk）
- CLI 必須帶 `--profile paper_reverse_fixed_exit_v1`

## 日界
- 結算：Asia/Taipei **20:15**
- 產報：Asia/Taipei **20:25**
- 首期交易日標籤：**2026-11-20**
  - 報告視窗：`[2026-11-19 20:15, 2026-11-20 20:15)`
  - **實際覆蓋**：`[2026-11-19 20:49:55, 2026-11-20 20:15)` — **非完整 24 小時**

## 日損口徑（引擎）
- 上限 **8000 USDT／日曆日**，午夜切日
- `2026-11-19` 與 `2026-11-20` 各有獨立 8000 上限
- **≠** 整場測試累計虧損保證上限

## 排程
- 範例 plist：`tools/paper_daily_review/schedule/com.automax.paper-reverse-daily-review.plist.example`
- **未載入** launchd；需另行批准才可 bootstrap

## 手動產報（結算後／試產）

```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/tools/paper_daily_review
PYTHONPATH=. python3 -m paper_daily_review \
  --setup-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup \
  --profile paper_reverse_fixed_exit_v1 \
  --mode day --day 2026-11-20
```

試產（未到 20:15，標記為部分日）：

```bash
PYTHONPATH=. python3 -m paper_daily_review \
  --setup-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup \
  --profile paper_reverse_fixed_exit_v1 \
  --mode day --day 2026-11-20 \
  --asof "$(TZ=Asia/Taipei date +%Y-%m-%dT%H:%M:%S%z | sed 's/\([0-9][0-9]\)$/:\1/')"
```

## 營運約定（2026-11-19）
- **20:15 停開倉／產報**：由使用者屆時指示執行；代理人不自行搶先停機或結算產報。
- 引擎內仍有 `NEW_ENTRIES_UNTIL_ISO=2026-11-20T20:15:00+08:00`（到點自動擋新開、持倉續管）；與「手動指示停開倉／產報」並行，報告以指示為準。
- 日報排程：`com.automax.paper-reverse-daily-review` 已載入（20:25）；舊 bulk 排程**未**載入，舊報告目錄保留且分開。
