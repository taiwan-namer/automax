# paper_reverse_fixed_usdt_50_75_inv_v1（帳戶 C）

鏡像 B（`paper_reverse_fixed_usdt_50_75_v1`）新開倉，**方向相反**；退出同毛利 +50 / −75。不自掃訊號。

## 啟動
```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_mirror_inv_b_20261120T004427/code
PYTHONPATH=. python3 -u -m breakout_follow_v1.paper_main \
  --paper --profile paper_reverse_fixed_usdt_50_75_inv_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --hours 0
```

## 監看
```bash
tail -f /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/mirror_inv_b_*.out.log
```

## 日結／日報（與 A／B 相同口徑）
- 結算界：每日 **20:15** Asia/Taipei
- 產報：每日 **20:25**（`com.automax.paper-mirror-inv-b-daily-review`）
- 報告根：`analysis_packs/daily_reports_paper_reverse_fixed_usdt_50_75_inv_v1/`
- 接線：同目錄上階 `analysis_packs/.../WIRING.md`

```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/tools/paper_daily_review
PYTHONPATH=. python3 -m paper_daily_review \
  --setup-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup \
  --profile paper_reverse_fixed_usdt_50_75_inv_v1 \
  --mode yesterday
```
