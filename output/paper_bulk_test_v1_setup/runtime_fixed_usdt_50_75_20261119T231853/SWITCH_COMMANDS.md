# paper_reverse_fixed_usdt_50_75_v1（B）

## 現行規則（2026-11-20）
- 進場：訊號 mid 為參考價；**多掛 −0.5% 限價／空掛 +0.5% 限價**；TTL **60s** 觸價成交，否則作廢
- 退出：固定毛 **TP +50／SL −75**
- 標記：`fix_20261120_limit_entry_0p5_tp50_sl75`

## 啟動
```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_fixed_usdt_50_75_20261119T231853/code
PYTHONPATH=. python3 -u -m breakout_follow_v1.paper_main \
  --paper --profile paper_reverse_fixed_usdt_50_75_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --hours 0
```

## 監看（目前這輪）
```bash
tail -f /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/usdt50_75_limit0p5_20261120T035208.out.log
```
