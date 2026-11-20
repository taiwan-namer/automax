# 預先準備：啟動／回滾命令（paper_bulk_test_v1）

## 身分
- profile: `paper_bulk_test_v1`
- runtime-root: `.../fixed_data_root`
- NEW: `runtime_bulk_v1_20261119T160723`
- OLD: `runtime_bulk_v1_20261119T155304`
- 停止方式：同 profile `paper_main --paper --profile ... --runtime-root ... --stop`

## 停止舊版
```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T155304/code
PYTHONPATH=. python3 -m breakout_follow_v1.paper_main \
  --paper --profile paper_bulk_test_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --stop
```

## 啟動新版（禁止 --once）
```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T160723/code
nohup env PYTHONPATH=. python3 -u -m breakout_follow_v1.paper_main \
  --paper --profile paper_bulk_test_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --hours 0 \
  > /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/switch_20261119T160723.out.log \
  2> /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/switch_20261119T160723.err.log &
```

## 回滾（先確認新版退出，再啟舊版；不還原帳戶快照）
```bash
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T160723/code
PYTHONPATH=. python3 -m breakout_follow_v1.paper_main \
  --paper --profile paper_bulk_test_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --stop
# 確認退出後：
cd /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_bulk_v1_20261119T155304/code
nohup env PYTHONPATH=. python3 -u -m breakout_follow_v1.paper_main \
  --paper --profile paper_bulk_test_v1 \
  --runtime-root /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root \
  --hours 0 \
  > /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/rollback_to_155304.out.log \
  2> /Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs/rollback_to_155304.err.log &
```
