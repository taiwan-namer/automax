# launchd 排程說明（**未安裝／未載入**）

狀態：文件與範例 plist 已就緒；**本輪不執行** `launchctl bootstrap/load`、不安裝到 `~/Library/LaunchAgents`。

## 設計

- 時區：`Asia/Taipei`（plist 設 `TZ`；工具內部亦固定 Taipei）。
- **結算界**：每日 **20:15**。交易日 `D` 視窗為半開區間 `[D-1 20:15, D 20:15)`。
- **報告**：每日 **20:25** 產製「最近已結算日」（20:15 後即為當日 `D`）。
- 另建議同次或緊接執行 `--mode backfill` 補缺日（有限天數，見 CLI）。
- 交易程序**不必**停止；本工具唯讀。
- **注意**：運行中 paper 引擎的日損 `day_key` 仍為日曆午夜（`today_key`），與本報告結算界分離；若要引擎同步 20:15 日切，需另行批准 sealed 部署。

## 關機／休眠

- macOS `StartCalendarInterval`：**不保證**休眠錯過後自動補跑。
- 因此：每次喚醒後的手動或登入後腳本可跑一次 `backfill`；**不要假定**系統必然補跑。
- 本設計**不**新增交易心跳；僅報表錯誤寫入 `launchd.err.log`。

## 有限重試（建議，啟用前再批）

```bash
# 範例：最多 3 次、間隔 60s — 尚未納入 plist
for i in 1 2 3; do
  python3 -m paper_daily_review --setup-root "$SETUP" --mode yesterday && \
  python3 -m paper_daily_review --setup-root "$SETUP" --mode backfill && break
  sleep 60
done
```

## 若將來批准安裝（現在不要做）

1. 複製 `com.automax.paper-daily-review.plist.example` → `~/Library/LaunchAgents/com.automax.paper-daily-review.plist`
2. 依本機 python 路徑與 SETUP 路徑改 ProgramArguments
3. `mkdir -p .../daily_reports/logs`
4. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.automax.paper-daily-review.plist`
5. 驗證：`launchctl print gui/$(id -u)/com.automax.paper-daily-review`

卸載：`launchctl bootout gui/$(id -u)/com.automax.paper-daily-review`

## 本輪狀態

- 範例路徑：`tools/paper_daily_review/schedule/com.automax.paper-daily-review.plist.example`
- **未** copy 到 LaunchAgents  
- **未** bootstrap／load  
- **待批准**：排程啟用

## 反向實驗（獨立，待批准）

- 範例：`com.automax.paper-reverse-daily-review.plist.example`
- Label：`com.automax.paper-reverse-daily-review`
- 必帶：`--profile paper_reverse_fixed_exit_v1`
- 輸出：`analysis_packs/daily_reports_paper_reverse_fixed_exit_v1/`
- **未**載入；與 bulk 排程分開；**待批准**才可 bootstrap
- 接線說明：`analysis_packs/daily_reports_paper_reverse_fixed_exit_v1/WIRING.md`
