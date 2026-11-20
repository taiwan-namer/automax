# paper_reverse_fixed_exit_v1 啟動命令（僅模擬）

## 身分
- profile: `paper_reverse_fixed_exit_v1`
- runtime: `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_rev_fixed_exit_20261119T204210`
- data-root: `/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root`
- 與 `paper_bulk_test_v1` **帳戶完全隔離**；不搬移舊未平倉

## 啟動（禁止 --once）
```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_rev_fixed_exit_20261119T204210/code
nohup env PYTHONPATH=. python3 -u -m breakout_follow_v1.paper_main \
  --paper --profile paper_reverse_fixed_exit_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --hours 0 \
  > /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/reverse_20261119T204210.out.log \
  2> /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/reverse_20261119T204210.err.log &
```

## 停止（不平倉）
```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_rev_fixed_exit_20261119T204210/code
PYTHONPATH=. python3 -m breakout_follow_v1.paper_main \
  --paper --profile paper_reverse_fixed_exit_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --stop
```
