# paper_reverse_fixed_usdt_50_75_inv_v1（帳戶 C）

## 現行身分（2026-11-20 起）
- **獨立自掃**（非鏡像 B）
- 與 B **相同**：訊號門檻、限價 ±0.5%、毛 TP+50／SL−75
- 與 B **相反方向**：`SIGNAL_DIRECTION_FLIP=False`（B=True）
  - 同一突破訊號規則下：B 做逆勢翻轉；C 做順勢
- 標記：`fix_20261120_c_selfscan_nofip_limit0p5_tp50_sl75`
- 程式：與 B 同 runtime `runtime_fixed_usdt_50_75_20261119T231853`

## 隔離
- 帳戶：`fixed_data_root/output/paper_reverse_fixed_usdt_50_75_inv_v1/`
- 報告：`analysis_packs/daily_reports_paper_reverse_fixed_usdt_50_75_inv_v1/`
- CLI：`--profile paper_reverse_fixed_usdt_50_75_inv_v1`

## 日界
- 結算 20:15／產報 20:25 Asia/Taipei

## 監看
```bash
tail -f /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/usdt50_75_inv_selfscan_*.out.log
```
