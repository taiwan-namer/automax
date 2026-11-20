# PAPER_BULK_TEST_READY_REPORT — paper_bulk_test_v1

- 產生時間（Asia/Taipei）：2026-11-19T12:08:30+08:00
- 本輪：**僅隔離副本修改與測試**；未部署、未停機、未重啟、未啟動長時間交易、未解除舊帳戶日損鎖
- 是否具備等待使用者批准啟動的條件：**是（隔離就緒）**

---

## 0. 現況確認（重新唯讀）

| 項目 | 2026-11-19T12:08 實測 |
|------|------------------------|
| 正式程序 | PID **33829**，`paper_main --paper --hours 0.0`，已運行 ~14m+（自 11:53） |
| 正式 run_id | `20261119T115315_b984d9a7` |
| 正式 launch tag | `fix_20261119_p0p1_trail_after_partial` |
| 正式帳戶 | `output/breakout_follow_v1/account_state.json` |
| 持倉 | **0** |
| 日損鎖 | **True**（`day_key=2026-11-19`，`day_start_equity=10000`） |
| 正式磁碟 `DAILY_LOSS_LOCK_USDT` | **300.0**（運行中記憶體亦為此版） |

### 基底版本釐清

| 層 | 狀態 |
|----|------|
| 正式已部署 | `…_trail_after_partial`（日損仍 300） |
| 隔離 worktree 基底 | `…_trail_after_partial_daily2000` + 本輪 **paper_bulk_test_v1** |
| 關係 | bulk profile **建於 worktree 最新修復副本**；正式程序尚未含 daily2000／bulk |

實盤安全：`GuardedSession` / `assert_public_url` 攔截 `/fapi/v1/order` 等；僅允許公開 GET 行情。`--paper` 必填。靜態掃描禁止 `create_order`／`API_SECRET` 等。

---

## 1. 基底設定核對（聲稱沿用 vs 實際）

| 參數 | 基底 worktree | bulk overlay | 一致？ |
|------|---------------|--------------|--------|
| 保證金 | 1000 | 1000 | 是 |
| 槓桿 | 10 | 10 | 是 |
| 名目 | 10000 | 10000 | 是 |
| 單筆風險上限 | 100 | 100 | 是 |
| 每標的持倉 | 1 | 1 | 是 |
| 冷卻 | 3 分 | 3 | 是 |
| 反手窗／上限 | 30 分／2 | 同 | 是 |
| 部分止盈分數／門檻 | 0.30／100 | 同 | 是 |
| TRAIL_ATR_MULT | 1.5 | 1.5 | 是 |
| TOP_N（宇宙） | 10 | 10 | 是（不擴大） |
| ATR_STOP／VOL_X | 1.5／3.0 | 同 | 是 |

### bulk 明確變更

| 參數 | 舊基底預設 | paper_bulk_test_v1 |
|------|------------|---------------------|
| `INITIAL_CAPITAL_USDT` | 10000 | **50000** |
| `MAX_CONCURRENT_POSITIONS` | 3 | **20** |
| `DAILY_LOSS_LOCK_USDT` | 2000（worktree）／300（正式） | **2000** |
| `PROFILE_ID`／輸出根 | `breakout_follow_v1` | **`paper_bulk_test_v1`** |
| vault | `data/breakout_follow_v1/kline_vault_v2_clean` | **`data/paper_bulk_test_v1/kline_vault_v2_clean`** |

未偷偷改訊號條件、時間框架、部分止盈／移動停利政策（仍：部分止盈成功後才啟用保本與 trail）。

---

## 2. 本次修改檔案（隔離 worktree）

路徑前綴：`worktrees/breakout_follow_v1_fix_20261119/`

| 檔案 | 變更 |
|------|------|
| `breakout_follow_v1/profiles.py` | **新增** profile 定義 |
| `breakout_follow_v1/config.py` | `PROFILE_ID`、`apply_profile()`、路徑重綁 |
| `breakout_follow_v1/paper_main.py` | `--profile`；同 profile 互斥；允許與舊 profile 並行 |
| `breakout_follow_v1/account.py` | `profile_id`；MFE/MAE 欄位 |
| `breakout_follow_v1/portfolio.py` | 多原因 `block_reasons`；成交／MFE-MAE  enrichment |
| `breakout_follow_v1/engine.py` | 決策列加 profile／分類／signal_key |
| `breakout_follow_v1/review.py` | 勝率／PF／分組／資金使用；空樣本 n/a |
| `breakout_follow_v1/run_store.py` | launch 記錄 profile／本金／倉上限 |
| `tests/test_paper_bulk_test_v1.py` | **新增** bulk 測試 |
| `run_all_tests.py` | 納入 bulk 模組 |

封存：`output/paper_bulk_test_v1_setup/fix_source_sealed_20261119T120802/`

---

## 3. 帳戶與輸出隔離

| 用途 | 新 profile 路徑 |
|------|-----------------|
| 帳戶 | `output/paper_bulk_test_v1/account_state.json` |
| fills／trades／equity／decision_trace／review | `output/paper_bulk_test_v1/*` |
| PID／run 指針 | `output/paper_bulk_test_v1/paper.pid`、`…/current_run.json`（啟動後） |
| 新 run 包 | `output/paper_bulk_test_v1/runs/<NEW_RUN_ID>/`（含 `code_snapshot/`、`account_at_start.json`） |
| 獨立 vault | `data/paper_bulk_test_v1/kline_vault_v2_clean/` |
| 舊帳戶／舊 vault | **不動**（`output/breakout_follow_v1/*`、`data/breakout_follow_v1/*`） |

- 首次 `load_or_create_account`：僅當帳戶檔不存在時以 **50000** 初始化。  
- 重啟：載入既有帳戶，**不**再補到 50000。  
- 同 profile 雙程序：preflight 阻擋；**不同** profile（舊 breakout vs bulk）可並行。  
- API 限流：仍用既有 `KLINES_API_CONCURRENCY`／`KLINES_MIN_INTERVAL_SEC`；並行兩程序會**共享交易所配額壓力**（風險已記錄，未繞過限流）。

---

## 4. 策略政策（不變）

- 已收盤 K、合併去重、訊號規則：沿用修復版。  
- 部分止盈成功後才保本／移動停利；失敗不標記成功。  
- 初始止損有效；成交價不倒填。  
- 反手：沿用基底（完整反向突破，非僅價格反向）。  
- 日界：Asia/Taipei；達 2000 禁新倉、仍管理退出；**2000 非硬保證最終損失上限**。  
- 不重啟解鎖、不重設日基準。

---

## 5. 復盤資料增強

決策：`profile_id`、`run_id`、`strategy_version`、`signal_key`、`signal_open_time`、條件／ATR／vol、`block_reasons[]`、`reject_category`、報價／資料時間。  
成交：名目／保證金／費率／滑價假設、`funding_data_gap`、退出原因、有效停損、MFE/MAE（**標記 `polled_quote_estimate`**）、持有秒數。  
報表：成交數、勝率／PF（樣本不足→n/a）、已實現／浮動／回撤、費用、持倉峰值、資金使用率、標的／多空／退出分組、拒絕分布。

---

## 6. 測試

```bash
cd /Users/alan/MyProjects/automax/worktrees/breakout_follow_v1_fix_20261119
python3 run_all_tests.py
```

```
SUMMARY passed=65 failed=0 total=65
```

| 要求項 | 結果 |
|--------|------|
| 1 首次 50000／重啟不重設 | **通過** |
| 2–3 超過 3 倉至 20；第 21 拒；釋放後可開 | **通過** |
| 4 同標的／同時多空禁止 | **通過** |
| 5 保證金／單筆風險 | **通過** |
| 6 日損 ＜／＝／＞ 2000 | **通過** |
| 7 鎖下退出、重啟保鎖、台北跨日 | **通過** |
| 8 寫入隔離、舊帳戶未動 | **通過** |
| 9 同 profile 衝突偵測 | **通過** |
| 10 部分止盈／移動停利回歸 | **通過**（既有 trail 套件全過） |
| 11 無實盤下單路徑 | **通過** |
| 12 報表空樣本 n/a | **通過** |
| 13 決策欄位不改開倉結果 | **通過** |
| 正式長時間啟動／雙程序實機並行壓測 | **未測**（本輪禁止） |
| 與正式程序並行之 API 限流實測 | **未測**（僅記錄風險） |

---

## 7. 模擬限制（必讀）

- 成交＝公開 bid/ask + 不利滑價假設，**非**交易所真實成交。  
- 資金費常 `funding_data_gap=true` → **不得**宣稱淨利已完整含資金費。  
- MFE/MAE 為輪詢報價採樣估計，**非**精確 tick 路徑。  
- 不保證獲利；日損 2000 只停開倉，既有倉與跳價仍可能超額虧損。

---

## 8. 經 CLI 核對的指令（**本輪不要執行正式初始化／長跑**）

已核對：

```text
python3 -m breakout_follow_v1.paper_main --help
  --paper（必填）
  --profile {breakout_follow_v1,paper_bulk_test_v1}
  --once | --hours
```

### 首次初始化（建立 50000 帳戶；短跑一輪即可）

```bash
cd /Users/alan/MyProjects/automax
# 需先將隔離封存安裝到可 import 的 package（另案批准）後：
python3 -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 --once
```

### 後續續跑（沿用帳戶，不重設本金）

```bash
python3 -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 --hours 0
# 日誌建議：output/paper_bulk_test_v1/paper_stdout_<TS>.log（勿覆寫）
```

### 停止（僅該 profile）

```bash
kill -TERM $(cat output/paper_bulk_test_v1/paper.pid)
# 確認：pgrep -af 'paper_main.*paper_bulk_test_v1'
```

### 驗收

```bash
# 帳戶本金／路徑
python3 - <<'PY'
import json
from pathlib import Path
p=Path('output/paper_bulk_test_v1/account_state.json')
print(json.loads(p.read_text())['initial_capital_u'], p)
PY
# launch / code_snapshot / account_at_start
cat output/paper_bulk_test_v1/current_run.json
```

**首次 vs 續跑**：首次無 `account_state.json` → 建 50000；其後一律載入既有檔，虧損／日鎖／持倉續接。

---

## 9. 查看路徑一覽

| 資料 | 路徑 |
|------|------|
| 帳戶 | `output/paper_bulk_test_v1/account_state.json` |
| fills／trades／equity／decision／review | `output/paper_bulk_test_v1/` |
| runs | `output/paper_bulk_test_v1/runs/` |
| vault | `data/paper_bulk_test_v1/kline_vault_v2_clean/` |
| 本就緒報告 | `output/paper_bulk_test_v1_setup/PAPER_BULK_TEST_READY_REPORT.md` |
| 程式封存 | `output/paper_bulk_test_v1_setup/fix_source_sealed_20261119T120802/` |

舊績效：`output/breakout_follow_v1/` — **分開統計，不可合併曲線**。

---

## 10. 下一步（待使用者批准）

1. 批准將封存安裝進正式 import 路徑（或指定 PYTHONPATH／部署流程）。  
2. 批准首次 `--once` 初始化 bulk 帳戶。  
3. 批准長時間 `--hours 0` 與舊程序並行（ cognizant API 配額）。  
4. **不**自動解除舊 `breakout_follow_v1` 日損鎖。

**結論：隔離副本已就緒，可等待批准啟動；本輪未執行正式初始化或啟動。**
