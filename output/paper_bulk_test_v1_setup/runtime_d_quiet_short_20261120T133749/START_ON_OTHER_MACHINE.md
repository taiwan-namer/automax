# 換機啟動 D paper 成交（本機不啟動、不動 A／B／C）

## 已確認，可 `--allow-fills`
- 保證金 300U × 10x = 名義 3000U
- TP／SL 按**毛額** 150／30；報告另列費／滑／資金費 → 淨利
- 雙側近價 0.1% 深度各 ≥50k；3000U walk 記錄；退出不拒平

## 1. 拷貝／clone
將整個 repo（至少含本 runtime）放到新機。建議獨立資料根：

```bash
export AUTOMAX=/PATH/TO/automax
export D_RT=$AUTOMAX/output/paper_bulk_test_v1_setup/runtime_d_quiet_short_20261120T133749
export D_DATA=$AUTOMAX/output/paper_bulk_test_v1_setup/fixed_data_root_D
mkdir -p "$D_DATA" "$AUTOMAX/output/paper_bulk_test_v1_setup/launch_logs"
```

**不要**使用本機正在跑 A／B／C 的 `fixed_data_root`。

## 2. 驗收
```bash
cd "$D_RT"
PYTHONPATH=code python3 tests/test_d_quiet_short_acceptance.py
```

## 3. 啟動 paper 成交
```bash
cd "$D_RT/code"
export PYTHONPATH=.
export PYTHONUNBUFFERED=1
export BREAKOUT_RUNTIME_ROOT="$D_DATA"

nohup python3 -u -m d_quiet_short_v1.paper_main \
  --paper \
  --allow-fills \
  --runtime-root "$D_DATA" \
  --hours 0 \
  > "$AUTOMAX/output/paper_bulk_test_v1_setup/launch_logs/d_quiet_short_$(date +%Y%m%dT%H%M%S).out.log" \
  2> "$AUTOMAX/output/paper_bulk_test_v1_setup/launch_logs/d_quiet_short_$(date +%Y%m%dT%H%M%S).err.log" &

echo "D_PID=$!"
```

## 4. 6h／8h 結算快照（一次性 sleeper，非 cron）
```bash
BREAKOUT_RUNTIME_ROOT="$D_DATA" \
nohup python3 -u "$D_RT/tools_snapshot_6h_8h.py" \
  > "$AUTOMAX/output/paper_bulk_test_v1_setup/launch_logs/d_quiet_snapshots.out.log" 2>&1 &
```

## 5. 監看
```bash
tail -f $AUTOMAX/output/paper_bulk_test_v1_setup/launch_logs/d_quiet_short_*.out.log
pgrep -lf 'd_quiet_short_v1.paper_main'
python3 -c "import json;print(json.load(open('$D_DATA/output/paper_d_quiet_short_v1/account_state.json')))"
```

## 6. 停止／回滾
```bash
kill <D_PID>   # 不平殺；帳本保留
# 清空 D：rm -rf "$D_DATA/output/paper_d_quiet_short_v1"
```

說明：模擬結果 ≠ 實盤成果。
