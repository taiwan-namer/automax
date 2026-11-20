# paper_reverse_fixed_usdt_50_75_v1 報告接線

- 帳戶：`fixed_data_root/output/paper_reverse_fixed_usdt_50_75_v1/`
- 報告輸出：`analysis_packs/daily_reports_paper_reverse_fixed_usdt_50_75_v1/`
- CLI 必須帶 `--profile paper_reverse_fixed_usdt_50_75_v1`
- 與 ATR 反向帳戶分帳；不混合。
- 退出模式：固定毛 TP+50 / SL-75（非 ATR）
- 自動排程：與既有 reverse 相同 20:25 Asia/Taipei（另 plist，非心跳）

```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/tools/paper_daily_review
PYTHONPATH=. python3 -m paper_daily_review \
  --setup-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup \
  --profile paper_reverse_fixed_usdt_50_75_v1 \
  --mode yesterday
```
